//! The brindle desktop shell. The window shows the React front end; all of
//! brindle's state and actions come from the local brindle daemon over its
//! API, so the shell itself carries no engine logic.
//!
//! The shell's own parts: the engine connection (finding or starting the
//! daemon and carrying the front end's requests to it, see `daemon`), system
//! notifications (the notification plugin, sent from the front end) and a
//! tray / menu-bar icon showing the "needs you" count, which the front end
//! sets with `set_inbox_count`. The macOS app carries its own engine, brindle
//! with its Python and tmux, and starts only that (see `engine`; its
//! `install_cli` links that brindle onto PATH). App updates come from the
//! updater plugin (see `updates`). When there is no daemon to find or start, the first run's
//! setup checks what brindle needs and installs it (see `setup`). On Windows
//! the engine runs inside WSL and the app is its thin client (see `wsl`).
//!
//! `brindle --version` prints the app's version and exits without a window;
//! `brindle --headless-check` connects to the daemon (starting one) and exits
//! 0 when it answers. The bundle smoke test (app/scripts/smoke_bundle.sh)
//! runs both.

mod about;
mod daemon;
mod engine;
/// Every pane the webview shows, through a real tmux client on a pty.
pub mod pty_view;
mod setup;
mod updates;
mod wsl;

use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager};

const TRAY_ID: &str = "inbox";

/// The tray icon's badge: the count beside the icon (macOS menu bar) and in
/// its tooltip everywhere.
#[tauri::command]
fn set_inbox_count(app: AppHandle, count: u32) -> Result<(), String> {
    let Some(tray) = app.tray_by_id(TRAY_ID) else {
        return Ok(());
    };
    let title = (count > 0).then(|| count.to_string());
    tray.set_title(title.as_deref()).map_err(|e| e.to_string())?;
    let tooltip = match count {
        0 => "brindle: nothing needs you".to_string(),
        1 => "brindle: 1 item needs you".to_string(),
        n => format!("brindle: {n} items need you"),
    };
    tray.set_tooltip(Some(tooltip)).map_err(|e| e.to_string())
}

/// Open a web page in the system browser (a brindle Pro sign-in, the
/// pricing page): never in the webview, so the person's own browser session
/// is used. Only http(s) URLs made of URL characters: anything else could
/// reach a local handler, and no shell ever sees the string (the URL comes
/// from the daemon's sign-in flow, which is the person's own engine, but an
/// argument stays an argument on every platform).
#[tauri::command]
fn open_url(url: String) -> Result<(), String> {
    if !(url.starts_with("https://") || url.starts_with("http://")) {
        return Err(format!("refusing to open {url}: not an http(s) address"));
    }
    if !url
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b"-._~:/?#[]@!$&'()*+,;=%".contains(&b))
    {
        return Err(format!("refusing to open {url}: not a plain URL"));
    }
    let mut command = if cfg!(target_os = "macos") {
        let mut c = std::process::Command::new("open");
        c.arg(&url);
        c
    } else if cfg!(target_os = "windows") {
        // Not `cmd /C start`: a shell would read `&` and the like in the URL.
        let mut c = std::process::Command::new("rundll32");
        c.args(["url.dll,FileProtocolHandler", &url]);
        c
    } else {
        let mut c = std::process::Command::new("xdg-open");
        c.arg(&url);
        c
    };
    command
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("couldn't open {url}: {e}"))
}

/// `brindle --headless-check`: no window; find or start the daemon as the
/// app does and ask it `GET /v1/meta`. Exit 0 when it answers 200.
fn headless_check() -> i32 {
    match daemon::Engine::default().get("/v1/meta") {
        Ok((200, body)) => {
            println!("brindle daemon: {body}");
            0
        }
        Ok((status, body)) => {
            eprintln!("brindle daemon answered {status}: {body}");
            1
        }
        Err(e) => {
            eprintln!("{e}");
            1
        }
    }
}

/// The most file descriptors the shell asks for: macOS refuses a soft limit
/// above OPEN_MAX, even under an unlimited hard one.
#[cfg(unix)]
const MAX_OPEN_FILES: libc::rlim_t = 10240;

/// Raise the soft limit on open files toward the hard one, as the engine does
/// for itself. An app opened from the Finder gets launchd's 256, and every
/// connection to the daemon is one of them. Returns the soft limit now.
#[cfg(unix)]
fn raise_open_files() -> Option<libc::rlim_t> {
    let mut limit = libc::rlimit { rlim_cur: 0, rlim_max: 0 };
    // SAFETY: getrlimit/setrlimit only read and fill the struct given.
    if unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) } != 0 {
        return None;
    }
    let want = limit.rlim_max.min(MAX_OPEN_FILES);
    if limit.rlim_cur < want {
        let raised = libc::rlimit { rlim_cur: want, rlim_max: limit.rlim_max };
        if unsafe { libc::setrlimit(libc::RLIMIT_NOFILE, &raised) } == 0 {
            limit.rlim_cur = want;
        }
    }
    Some(limit.rlim_cur)
}

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    #[cfg(unix)]
    raise_open_files();
    let context = tauri::generate_context!();
    match std::env::args().nth(1).as_deref() {
        Some("--version") => {
            println!("brindle {}", context.package_info().version);
            return;
        }
        Some("--headless-check") => std::process::exit(headless_check()),
        _ => {}
    }
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        // The native folder picker (Knowledge's Add folder), from the front end.
        .plugin(tauri_plugin_dialog::init())
        // The clipboard, for what a pane view copies and pastes (pty_view_copy, pty_view_paste). Rust's
        // only: the webview has no clipboard permission of its own.
        .plugin(tauri_plugin_clipboard_manager::init())
        .manage(daemon::Engine::default())
        .manage(setup::Installer::default())
        .manage(pty_view::PtyViews::default())
        .manage(updates::Installs::default())
        // The app's one webview starts a page (a reload, a web content process
        // that died and came back): the streams the last page opened go with it.
        .on_page_load(|webview, payload| {
            if payload.event() == tauri::webview::PageLoadEvent::Started {
                webview.state::<daemon::Engine>().close_all_streams();
                webview.state::<pty_view::PtyViews>().close_all();
            }
        })
        .invoke_handler(tauri::generate_handler![
            set_inbox_count,
            open_url,
            daemon::daemon_connect,
            daemon::daemon_request,
            daemon::daemon_stream_open,
            daemon::daemon_stream_send,
            daemon::daemon_stream_close,
            engine::install_cli,
            setup::setup_detect,
            setup::setup_install_start,
            setup::setup_install_attach,
            setup::setup_install_write,
            setup::setup_install_resize,
            pty_view::pty_view_status,
            pty_view::pty_view_copy,
            pty_view::pty_view_paste,
            pty_view::pty_view_open,
            pty_view::pty_view_write,
            pty_view::pty_view_resize,
            pty_view::pty_view_focus,
            pty_view::pty_view_close,
            updates::app_update_install,
            updates::app_release_build_install,
            updates::app_restart,
            about::about_read,
        ])
        .setup(|app| {
            let mut tray = TrayIconBuilder::with_id(TRAY_ID)
                .tooltip("brindle")
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main_window(tray.app_handle());
                    }
                });
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;
            updates::check_in_background(app.handle(), &daemon::brindle_home());
            Ok(())
        })
        .build(context)
        .expect("error while building the brindle app")
        .run(|app, event| {
            // The app quits: each pane view's session goes with it (a crash
            // takes them too: their clients die with their ptys).
            if let tauri::RunEvent::Exit = event {
                app.state::<pty_view::PtyViews>().close_all();
            }
        });
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    fn soft_limit() -> libc::rlimit {
        let mut limit = libc::rlimit { rlim_cur: 0, rlim_max: 0 };
        // SAFETY: getrlimit only fills the struct given.
        assert_eq!(unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) }, 0);
        limit
    }

    #[test]
    fn the_shell_raises_launchds_256_open_files_toward_the_hard_limit() {
        // In a process of its own started as the Finder starts the app (soft limit 256),
        // so the tests alongside keep theirs.
        let exe = std::env::current_exe().unwrap();
        let out = std::process::Command::new("/bin/sh")
            .arg("-c")
            .arg(r#"ulimit -S -n 256 && exec "$0" --exact tests::raise_open_files_from_256 --ignored"#)
            .arg(&exe)
            .env("BRINDLE_TEST_OPEN_FILES", "256")
            .output()
            .unwrap();
        let stdout = String::from_utf8_lossy(&out.stdout);
        assert!(out.status.success() && stdout.contains("1 passed"), "{stdout}{}", String::from_utf8_lossy(&out.stderr));
    }

    /// Run by the test above, in a process whose soft limit is 256.
    #[test]
    #[ignore]
    fn raise_open_files_from_256() {
        if std::env::var_os("BRINDLE_TEST_OPEN_FILES").is_none() {
            return;
        }
        let before = soft_limit();
        assert_eq!(before.rlim_cur, 256);
        let want = before.rlim_max.min(MAX_OPEN_FILES);
        assert_eq!(raise_open_files(), Some(want));
        assert_eq!(soft_limit().rlim_cur, want);
        assert!(want > 256, "the hard limit is {}", before.rlim_max);
        // Raised already: left as it is.
        assert_eq!(raise_open_files(), Some(want));
    }
}
