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
//! the engine runs inside WSL and the app is its thin client (see `wsl`; its
//! panes come over the daemon's WebSocket, see `pty_bridge`).
//!
//! `brindle --version` prints the app's version and exits without a window;
//! `brindle --headless-check` connects to the daemon (starting one) and exits
//! 0 when it answers. The bundle smoke test (app/scripts/smoke_bundle.sh)
//! runs both.

mod about;
mod daemon;
mod engine;
mod instance;
mod oauth;
mod pty_bridge;
/// Every pane the webview shows, through a real tmux client on a pty.
pub mod pty_view;
mod setup;
mod startup;
mod updates;
/// The sandbox each agent-written visual renders in: a child webview on the
/// `brindle-visual://` scheme that can reach nothing but the shell.
pub mod visual_scheme;
mod wsl;

use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, Runtime};

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

/// Open a local Knowledge file or folder with its platform application.
#[tauri::command]
fn open_local_path(path: String) -> Result<(), String> {
    let local = std::path::Path::new(&path);
    if !local.is_absolute() || !local.exists() {
        return Err("choose an existing absolute local path".into());
    }
    let mut command = if cfg!(target_os = "macos") {
        std::process::Command::new("open")
    } else if cfg!(target_os = "windows") {
        let mut c = std::process::Command::new("explorer.exe");
        c.arg(&path);
        return c.spawn().map(|_| ()).map_err(|e| e.to_string());
    } else {
        std::process::Command::new("xdg-open")
    };
    command.arg(local).spawn().map(|_| ()).map_err(|e| e.to_string())
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

/// The app's build-time context (tauri.conf.json, the embedded front end, the
/// capabilities). Generated once here: the visual probe suite builds its own
/// app from it, and a second `generate_context!` would embed the Info.plist twice.
pub fn context() -> tauri::Context {
    tauri::generate_context!()
}

/// The webview the app's page runs in: the only one that may call commands.
const MAIN_WEBVIEW: &str = "main";

/// Every command is the app's page's alone. Tauri checks its ACL on the app's
/// own commands only when the app ships a permission manifest (it doesn't),
/// and a page on a registered custom scheme counts as a local origin: so a
/// visual's child webview (visual_scheme) would otherwise reach
/// `daemon_request` and the rest through the injected IPC. The check is
/// here, before any command is dispatched, by webview label.
pub fn only_main<R: Runtime>(
    commands: impl Fn(tauri::ipc::Invoke<R>) -> bool + Send + Sync + 'static,
) -> impl Fn(tauri::ipc::Invoke<R>) -> bool + Send + Sync + 'static {
    move |invoke| {
        if invoke.message.webview().label() != MAIN_WEBVIEW {
            let command = invoke.message.command().to_string();
            invoke
                .resolver
                .reject(format!("{command}: only the app's page may call commands"));
            return true;
        }
        commands(invoke)
    }
}

/// The app's commands, guarded: what the app runs, and what the visual probe
/// suite (tests/visual_probes.rs) puts its hostile pages against.
pub fn invoke_handler() -> impl Fn(tauri::ipc::Invoke<tauri::Wry>) -> bool + Send + Sync + 'static {
    only_main(tauri::generate_handler![
        set_inbox_count,
        open_url,
        oauth::oauth_listen,
        oauth::oauth_wait,
        oauth::oauth_cancel,
        oauth::provider_http,
        open_local_path,
        daemon::daemon_connect,
        startup::app_startup_step,
        daemon::daemon_start_step,
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
        pty_view::pty_view_files,
        pty_view::pty_view_attachment_path,
        pty_view::pty_view_open,
        pty_view::pty_view_write,
        pty_view::pty_view_resize,
        pty_view::pty_view_focus,
        pty_view::pty_view_close,
        updates::app_update_install,
        updates::app_release_build_install,
        updates::app_return_to_stable,
        updates::app_stable_check,
        updates::app_restart,
        updates::app_update_status,
        updates::app_update_check,
        updates::app_update_dismiss,
        updates::app_update_later,
        updates::app_update_now,
        about::about_read,
        visual_scheme::visual_open,
        visual_scheme::visual_close,
        visual_scheme::visual_set_bounds,
        visual_scheme::visual_post,
        visual_scheme::visual_snapshot,
    ])
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    startup::begin();
    #[cfg(unix)]
    raise_open_files();
    let context = context();
    match std::env::args().nth(1).as_deref() {
        Some("--version") => {
            println!("brindle {}", context.package_info().version);
            return;
        }
        Some("--headless-check") => std::process::exit(headless_check()),
        // Relaunched into an update (instance::relaunch): the instance that was runs out
        // first. A surviving daemon is reused unless it is already pausing.
        #[cfg(target_os = "macos")]
        Some(instance::AFTER_EXIT_FLAG) => {
            if let Some(pid) = std::env::args().nth(2).and_then(|pid| pid.parse().ok()) {
                instance::wait_for_exit(pid);
                daemon::wait_for_predecessor(pid);
            }
        }
        _ => {}
    }
    // One brindle at a time: a second launch brings the running one to the front and ends here.
    let exe = std::env::current_exe().ok();
    let _place = match instance::take_place(&daemon::brindle_home(), exe.as_deref()) {
        instance::Place::Ours(claim) => claim,
        instance::Place::Taken => return,
    };
    // The visual sandbox tells the main webview what visuals say and do; its
    // app handle exists only once the app is built (setup fills it).
    let visuals_app: std::sync::Arc<std::sync::OnceLock<AppHandle>> = Default::default();
    let visuals_sink = visual_scheme::deliver_to_main(visuals_app.clone());
    let builder = tauri::Builder::default()
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
        .manage(updates::Seen::default())
        // The app's one webview starts a page (a reload, a web content process
        // that died and came back): the streams the last page opened go with it.
        .on_page_load(|webview, payload| {
            // Only the app's page: a visual's child webview loads its own.
            if webview.label() == "main" && payload.event() == tauri::webview::PageLoadEvent::Started {
                webview.state::<daemon::Engine>().close_all_streams();
                webview.state::<pty_view::PtyViews>().close_all();
            }
        });
    // A visual's page comes from the daemon's revision route, with the shell's token.
    let builder = visual_scheme::register(builder, visual_scheme::Sandbox::new(visuals_sink), |app, visual| {
        let (status, body) = app.state::<daemon::Engine>().get(&visual.daemon_route())?;
        Ok(visual_scheme::Upstream { status, body: body.into_bytes() })
    });
    builder
        .invoke_handler(invoke_handler())
        .setup(move |app| {
            startup::open(app.handle());
            let _ = visuals_app.set(app.handle().clone());
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
                // A daemon the app runs as its own child (Allow in the Background
                // switched off) stops with it, pausing its sessions.
                #[cfg(target_os = "macos")]
                daemon::stop_app_daemon();
            }
        });
}

#[cfg(test)]
mod ipc_tests {
    use super::*;
    use tauri::ipc::{CallbackFn, InvokeBody};
    use tauri::test::{get_ipc_response, mock_builder, mock_context, noop_assets, MockRuntime, INVOKE_KEY};
    use tauri::webview::{InvokeRequest, WebviewBuilder};
    use tauri::{LogicalPosition, LogicalSize, WebviewUrl, WebviewWindowBuilder};

    /// The same guard the app runs, around a real command, dispatched as the
    /// IPC does: the app's page reaches the command, a visual's webview in the
    /// same window is refused before it.
    #[test]
    fn only_the_main_webview_reaches_a_command() {
        let app = mock_builder()
            .manage(visual_scheme::Sandbox::new(|_| {}))
            .invoke_handler(only_main(tauri::generate_handler![visual_scheme::visual_set_bounds]))
            .build(mock_context(noop_assets()))
            .unwrap();
        let main = WebviewWindowBuilder::new(&app, "main", WebviewUrl::default()).build().unwrap();
        let visual = app
            .get_window("main")
            .unwrap()
            .add_child(
                WebviewBuilder::new("visual:v_1:r1", WebviewUrl::default()),
                LogicalPosition::new(0.0, 0.0),
                LogicalSize::new(10.0, 10.0),
            )
            .unwrap();
        struct Asking(tauri::Webview<MockRuntime>);
        impl AsRef<tauri::Webview<MockRuntime>> for Asking {
            fn as_ref(&self) -> &tauri::Webview<MockRuntime> {
                &self.0
            }
        }
        let ask = |webview: &tauri::Webview<MockRuntime>| {
            get_ipc_response(
                &Asking(webview.clone()),
                InvokeRequest {
                    cmd: "visual_set_bounds".into(),
                    callback: CallbackFn(0),
                    error: CallbackFn(1),
                    url: "tauri://localhost".parse().unwrap(),
                    body: InvokeBody::Json(serde_json::json!({
                        "label": "visual:nobody:r1",
                        "bounds": { "x": 0, "y": 0, "width": 10, "height": 10 },
                    })),
                    headers: Default::default(),
                    invoke_key: INVOKE_KEY.into(),
                },
            )
            .map_err(|e| e.to_string())
        };
        // The app's page: the command ran (and answered for itself).
        let from_main = ask(&main.as_ref().clone()).unwrap_err();
        assert!(from_main.contains("is not an open visual"), "{from_main}");
        // The visual's webview, same window, same local origin: stopped at the door.
        let from_visual = ask(&visual).unwrap_err();
        assert!(from_visual.contains("only the app's page may call commands"), "{from_visual}");
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn local_open_rejects_urls_and_relative_paths() {
        for path in ["https://example.com", "src/cache", "../notes"] {
            assert!(open_local_path(path.into()).is_err());
        }
    }

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
