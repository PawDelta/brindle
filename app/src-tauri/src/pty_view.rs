//! A tmux pane (an agent's window, or one of the daemon's action terminals)
//! shown through a real tmux client on a pty, the way a person's own
//! terminal shows it (see app/docs/terminal-architecture.md). It is how
//! the webview shows every pane.
//!
//! Each open view gets a tmux session of its own on the agent's tmux server
//! (`brindle-view-<pid>-<n>`), holding only the agent's window (`link-window`,
//! so no other window is reachable from it), with session options that make
//! it the app's and nobody else's:
//! - no status line, no prefix key: every key reaches the provider;
//! - the mouse on: tmux passes the wheel to a program that asks for mouse
//!   reports (Claude Code), and scrolls a plain pane's history in copy mode;
//! - `key-table brindle-view`: the view's client never looks at the server's
//!   root table, so whatever a config the server loaded bound there (`bind -n`)
//!   doesn't capture keys typed in the app. That table holds tmux's stock
//!   mouse bindings for the pane, read from a tmux with no config at all,
//!   so the view behaves as an unconfigured tmux does;
//! - `destroy-unattached`: the session goes when its client does (the view
//!   closes, the app quits or dies); the agent's window stays, still linked
//!   to the agent's own session.
//!
//! Nothing else on the server is changed: no global (`-g`, `-s`) option, no
//! other key table, no option of the agent's session or window. A person's
//! terminal on the agent's session keeps its status line, prefix and keys,
//! and the window's size follows tmux's own `window-size` between the two.
//! The view focused or clicked counts as used, as typing in it does
//! (`take_window`: a `switch-client` of the view's own client to its own
//! session, nothing sent to the pane).
//!
//! `tmux -T <features> attach-session` runs on a pty of the view's size;
//! its bytes go to the webview a redraw at a time (`forward`), the webview's
//! bytes go to it as they are typed. Nothing here parses either (`forward`
//! only looks for where a synchronized update starts and ends). tmux 3.2 or
//! newer, both the client run here (the app's bundled one) and the server it
//! attaches to.
//!
//! Any of this user's tmux servers: a named one (`-L`), a socket path (`-S`),
//! or the default server (no `-L`/`-S`), where a person's own sessions, and
//! agents with no `tmux_server`, are. The view's footprint is the same on
//! each: its own `brindle-view-*` session and key table, nothing global.
//! Stale view sessions (an app that died) are cleared on whichever server a
//! view opens on, the default one too.

use std::collections::HashMap;
use std::fs::File;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{mpsc, Arc, Mutex, OnceLock};

use serde::Serialize;
use tauri::ipc::Channel;
use tauri::State;

use crate::daemon::base64;

/// What tmux is told xterm.js can do (`terminal-features`): it can't ask, as
/// it asks a terminal it knows by name. RGB keeps 24-bit colour 24-bit;
/// clipboard has tmux hand its copies to the app as OSC 52. The server reads
/// the list up to the first name it doesn't know: 3.2 knows them all but
/// hyperlinks (3.4), so that one goes last.
pub const FEATURES: &str = "RGB,256,mouse,title,bpaste,focus,sync,usstyle,strikethrough,overline,cstyle,clipboard,hyperlinks";
/// The oldest tmux the view works with: `-T` (client features) is 3.2's.
pub const MIN_TMUX: (u32, u32) = (3, 2);
/// The view sessions' names start so: `brindle-view-<the app's pid>-<n>`.
pub const VIEW_PREFIX: &str = "brindle-view-";
/// The view's key table, in place of the server's root table.
pub const KEY_TABLE: &str = "brindle-view";
/// tmux's stock root-table bindings the view's table takes, by the oldest
/// server each is for: the pane's own mouse (select, drag to copy, wheel,
/// middle-click paste, double and triple click, the scrollbar). Not the
/// menus, borders, status line or swaps: those reach other panes and
/// windows, or kill and split them. The 3.2 lines are tmux 3.2a's own
/// (`list-keys -T root` of a server with no config) and every later tmux
/// reads them; a later one's replaces its key's (a bind replaces a bind)
/// or adds a key that tmux only has from then. As 3.7c lists them.
const MOUSE_BINDINGS: &[((u32, u32), &str)] = &[
    ((3, 2), r##"MouseDown1Pane select-pane -t = \; send-keys -M"##),
    ((3, 2), r##"MouseDown2Pane select-pane -t = \; if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" "send -M" "paste -p""##),
    ((3, 2), r##"MouseDrag1Pane if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" "send -M" "copy-mode -M""##),
    ((3, 2), r##"WheelUpPane if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" "send -M" "copy-mode -e""##),
    ((3, 2), r##"DoubleClick1Pane select-pane -t = \; if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" "send -M" "copy-mode -H ; send -X select-word ; run -d0.3 ; send -X copy-pipe-and-cancel""##),
    ((3, 2), r##"TripleClick1Pane select-pane -t = \; if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" "send -M" "copy-mode -H ; send -X select-line ; run -d0.3 ; send -X copy-pipe-and-cancel""##),
    // A full-screen program that asks for no mouse gets the wheel too.
    ((3, 7), r##"WheelUpPane if-shell -F "#{||:#{alternate_on},#{pane_in_mode},#{mouse_any_flag}}" { send-keys -M } { copy-mode -e }"##),
    // Pane scrollbars.
    ((3, 7), r##"MouseDown1ScrollbarUp if-shell -F -t = "#{pane_in_mode}" { send-keys -X page-up } { copy-mode -u }"##),
    ((3, 7), r##"MouseDown1ScrollbarDown if-shell -F -t = "#{pane_in_mode}" { send-keys -X page-down } { copy-mode -d }"##),
    ((3, 7), r##"MouseDrag1ScrollbarSlider if-shell -F -t = "#{pane_in_mode}" { send-keys -X scroll-to-mouse } { copy-mode -S }"##),
];
/// ⌘C with nothing selected in xterm.js: the view sends this key
/// (Ctrl+Option+Shift+F12, PtyTerminal's COPY_KEY, which no shortcut
/// sends), and tmux copies its copy-mode selection, kept on screen, as a drag's
/// copy goes (OSC 52, to the app's clipboard). With none, nothing: the key
/// never reaches the pane. Every server from 3.2 has it.
pub const COPY_BINDING: &str = r##"C-M-S-F12 if-shell -F "#{selection_present}" "send-keys -X copy-selection-no-clear""##;
/// Ctrl+C on Windows and Linux with nothing selected in xterm.js
/// (Ctrl+Option+Shift+F11, PtyTerminal's COPY_OR_INTERRUPT): tmux's
/// copy-mode selection copied (OSC 52) and copy mode left, as a terminal's
/// Ctrl+C clears what it copied; with none, C-c to the pane, as typed.
pub const COPY_OR_INTERRUPT_BINDING: &str = r##"C-M-S-F11 if-shell -F "#{selection_present}" "send-keys -X copy-selection-and-cancel" "send-keys C-c""##;
/// The largest pty a view asks for.
const MAX_SIZE: u16 = 1000;

static NEXT_VIEW: AtomicU32 = AtomicU32::new(1);

/// The tmux to run: the bundled engine's, else (a dev build) the one on PATH.
pub fn tmux_binary() -> PathBuf {
    crate::engine::bundled_engine()
        .map(|engine| engine.join("bin/tmux"))
        .filter(|tmux| tmux.exists())
        .unwrap_or_else(|| PathBuf::from("tmux"))
}

/// A tmux command. `-u`: UTF-8 whatever the locale, and the app has none
/// (launchd starts it with no LANG): tmux would otherwise print every byte
/// outside printable ASCII as `_`, the tabs `crop_of` splits `list-panes` on too.
fn tmux(tmux: &Path) -> Command {
    let mut cmd = Command::new(tmux);
    cmd.arg("-u").env_remove("TMUX").stdin(Stdio::null());
    cmd
}

/// A tmux version's major and minor: `tmux 3.7c`, `3.2a`, `next-3.4`, `openbsd-7.5`
/// (OpenBSD's own, which is new). `master` is a build of the newest.
pub fn parse_version(version: &str) -> Option<(u32, u32)> {
    let v = version.trim().trim_start_matches("tmux").trim();
    if v == "master" {
        return Some((u32::MAX, 0));
    }
    if v.starts_with("openbsd-") {
        return Some((u32::MAX, 0));
    }
    let v = v.trim_start_matches("next-");
    let mut parts = v.splitn(2, '.');
    let major = parts.next()?.parse().ok()?;
    let minor: String = parts.next().unwrap_or("0").chars().take_while(char::is_ascii_digit).collect();
    Some((major, minor.parse().unwrap_or(0)))
}

/// Refuses a tmux older than 3.2, in words the app shows as they are. `what`
/// says which tmux it is (the app's own, or the agents' server).
pub fn check_version(version: &str, what: &str) -> Result<(), String> {
    let (major, minor) = MIN_TMUX;
    match parse_version(version) {
        Some(v) if v >= MIN_TMUX => Ok(()),
        Some(_) => Err(format!("{what} is {}, and brindle needs tmux {major}.{minor} or newer to show a session. Update tmux, then reopen the session.", version.trim())),
        None => Err(format!("{what} says its version is {:?}, which brindle can't read: it needs tmux {major}.{minor} or newer.", version.trim())),
    }
}

/// The version of the tmux binary the view runs.
pub fn client_version(tmux_bin: &Path) -> Result<String, String> {
    let out = tmux(tmux_bin).arg("-V").output().map_err(|e| format!("couldn't run tmux ({}): {e}", tmux_bin.display()))?;
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Where tmux puts a `-L name` server's socket: `$TMUX_TMPDIR` (else /tmp)/tmux-<uid>/<name>.
#[cfg(unix)]
fn socket_of(name: &str) -> PathBuf {
    let dir = std::env::var_os("TMUX_TMPDIR").filter(|d| !d.is_empty()).map_or_else(|| PathBuf::from("/tmp"), PathBuf::from);
    // SAFETY: getuid has no failure mode.
    dir.join(format!("tmux-{}", unsafe { libc::getuid() })).join(name)
}

/// The arguments that name `server` (an agent's `tmux_server`): `-S` for a
/// socket path, `-L` for a name, none for the default server (no server, or
/// `default` in any case: macOS's file system ignores case). Its socket path
/// is a path like any other: `-S` to it.
pub fn server_args(server: Option<&str>) -> Result<Vec<String>, String> {
    match server.map(str::trim).filter(|s| !s.is_empty()) {
        None => Ok(vec![]),
        Some(s) if s.contains('/') => Ok(vec!["-S".into(), s.into()]),
        Some(s) if !s.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b)) => Err(format!("not a tmux server name: {s}")),
        Some(s) if s.eq_ignore_ascii_case("default") => Ok(vec![]),
        Some(s) => Ok(vec!["-L".into(), s.into()]),
    }
}

/// A tmux target (`session:window`, `%pane`, `@window`), as an agent's
/// `window` is: never a command, never a flag.
pub fn valid_target(target: &str) -> bool {
    !target.is_empty() && !target.starts_with('-') && target.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_.:@%$=+".contains(&b))
}

/// The view's key table for a server of `version`, as `source-file` reads
/// it: every `MOUSE_BINDINGS` line that server has, in order, then ⌘C's
/// and Ctrl+C's (`COPY_BINDING`, `COPY_OR_INTERRUPT_BINDING`).
pub fn view_bindings(version: (u32, u32)) -> String {
    MOUSE_BINDINGS
        .iter()
        .filter(|(since, _)| version >= *since)
        .map(|(_, binding)| *binding)
        .chain([COPY_BINDING, COPY_OR_INTERRUPT_BINDING])
        .map(|binding| format!("bind-key -T {KEY_TABLE} {binding}\n"))
        .collect()
}

/// One tmux command list that makes the view session `view` of `cols` x
/// `rows` showing only `target`'s window, with the app's session options,
/// and reads the view's key table (`view_bindings`) from stdin.
pub fn prepare_args(view: &str, target: &str, cols: u16, rows: u16) -> Vec<String> {
    let placeholder = "brindle-view-placeholder";
    // set-option's -t is a pane's: `=view:` is the session's current window, so the session.
    let session = format!("={view}:");
    let cmds: Vec<Vec<String>> = vec![
        // A session's first window is a command's; `cat` waits on a pty and does nothing.
        vec!["new-session", "-d", "-s", view, "-n", placeholder, "-x", &cols.to_string(), "-y", &rows.to_string(), "cat"],
        vec!["link-window", "-s", target, "-t", &session],
        vec!["kill-window", "-t", &format!("={view}:{placeholder}")],
        vec!["set-option", "-t", &session, "status", "off"],
        vec!["set-option", "-t", &session, "prefix", "None"],
        vec!["set-option", "-t", &session, "prefix2", "None"],
        vec!["set-option", "-t", &session, "mouse", "on"],
        vec!["set-option", "-t", &session, "key-table", KEY_TABLE],
        vec!["source-file", "-"],
    ]
    .into_iter()
    .map(|c| c.into_iter().map(String::from).collect())
    .collect();
    cmds.join(&[";".to_string()][..])
}

/// The client's command line. `destroy-unattached` only once it is
/// attached: set on a session with no client, it takes the session at once.
pub fn attach_args(view: &str) -> Vec<String> {
    let session = format!("={view}:");
    // -u: a UTF-8 client whatever the app's locale (it has none), else it draws ❯, ⏺, a box's lines as `_`.
    ["-u", "-T", FEATURES, "attach-session", "-t", &session, ";", "set-option", "-t", &session, "destroy-unattached", "on"]
        .into_iter()
        .map(String::from)
        .collect()
}

/// The app's pid in a view session's name, if it is one.
pub fn view_owner(session: &str) -> Option<u32> {
    let rest = session.strip_prefix(VIEW_PREFIX)?;
    let (pid, n) = rest.split_once('-')?;
    n.bytes().all(|b| b.is_ascii_digit()).then_some(())?;
    pid.parse().ok()
}

#[cfg(unix)]
fn alive(pid: u32) -> bool {
    // SAFETY: signal 0 only checks the process exists.
    unsafe { libc::kill(pid as libc::pid_t, 0) == 0 || io::Error::last_os_error().raw_os_error() == Some(libc::EPERM) }
}

/// View sessions an app that is gone left on the server (it died before its
/// client attached): killed. Another running app's, and this one's, stay.
#[cfg(unix)]
fn clear_stale(tmux_bin: &Path, server: &[String]) {
    let Ok(out) = tmux(tmux_bin).args(server).args(["list-sessions", "-F", "#{session_name}"]).output() else { return };
    for session in String::from_utf8_lossy(&out.stdout).lines() {
        if view_owner(session).is_some_and(|pid| pid != std::process::id() && !alive(pid)) {
            let _ = tmux(tmux_bin).args(server).args(["kill-session", "-t", &format!("={session}")]).output();
        }
    }
}

/// The executable process `pid` runs.
#[cfg(target_os = "macos")]
fn exe_of(pid: u32) -> Option<PathBuf> {
    use std::os::unix::ffi::OsStrExt;
    let mut buf = vec![0u8; 4096];
    // SAFETY: proc_pidpath writes at most `buf.len()` bytes into `buf`.
    let n = unsafe { libc::proc_pidpath(pid as libc::c_int, buf.as_mut_ptr().cast(), buf.len() as u32) };
    (n > 0).then(|| PathBuf::from(std::ffi::OsStr::from_bytes(&buf[..n as usize])))
}

#[cfg(all(unix, not(target_os = "macos")))]
fn exe_of(pid: u32) -> Option<PathBuf> {
    std::fs::read_link(format!("/proc/{pid}/exe")).ok()
}

/// Who listens on the unix socket at `socket`, as the kernel says (not as
/// the server answers): its uid and pid.
#[cfg(target_os = "macos")]
fn peer_of(socket: &Path) -> Option<(u32, u32)> {
    use std::os::fd::AsRawFd;
    let stream = std::os::unix::net::UnixStream::connect(socket).ok()?;
    let fd = stream.as_raw_fd();
    let (mut uid, mut gid): (libc::uid_t, libc::gid_t) = (0, 0);
    let mut pid: libc::pid_t = 0;
    let mut len = std::mem::size_of::<libc::pid_t>() as libc::socklen_t;
    // SAFETY: each call writes only into the locals it is given, sized as said.
    unsafe {
        if libc::getpeereid(fd, &mut uid, &mut gid) != 0 {
            return None;
        }
        if libc::getsockopt(fd, libc::SOL_LOCAL, libc::LOCAL_PEERPID, (&mut pid as *mut libc::pid_t).cast(), &mut len) != 0 {
            return None;
        }
    }
    Some((uid, pid as u32))
}

#[cfg(all(unix, not(target_os = "macos")))]
fn peer_of(socket: &Path) -> Option<(u32, u32)> {
    use std::os::fd::AsRawFd;
    let stream = std::os::unix::net::UnixStream::connect(socket).ok()?;
    // SAFETY: a zeroed ucred is a valid one; getsockopt writes at most `len` bytes into it.
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    if unsafe { libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED, (&mut cred as *mut libc::ucred).cast(), &mut len) } != 0 {
        return None;
    }
    Some((cred.uid, cred.pid as u32))
}

/// Whether `exe` is safe to run as this user: it and its folder owned by
/// root or this user, and writable by no one else.
#[cfg(unix)]
fn trusted_exe(exe: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    // SAFETY: getuid has no failure mode.
    let me = unsafe { libc::getuid() };
    let safe = |p: &Path| std::fs::metadata(p).is_ok_and(|m| (m.uid() == 0 || m.uid() == me) && m.mode() & 0o022 == 0);
    exe.canonicalize().is_ok_and(|exe| safe(&exe) && exe.parent().is_some_and(safe))
}

/// The socket `server` (`server_args`) names.
#[cfg(unix)]
fn socket_path(server: &[String]) -> PathBuf {
    match server {
        [flag, path] if flag == "-S" => PathBuf::from(path),
        [_, name] => socket_of(name),
        _ => socket_of("default"),
    }
}

/// The tmux the view's client runs. A client attaches only to a server of
/// its own version (3.7's to 3.2's: "open terminal failed: not a
/// terminal"), so ours (`tmux_bin`, `ours`: its `-V`) when the server runs
/// the same (`theirs`), else the executable of the process listening on its
/// `socket`: only one of this user's, that no one else can write to, and
/// that says it is that version. Nothing the server answers picks it.
#[cfg(unix)]
fn client_for(tmux_bin: &Path, ours: &str, socket: &Path, theirs: &str) -> Result<PathBuf, String> {
    if ours == theirs {
        return Ok(tmux_bin.to_path_buf());
    }
    // SAFETY: getuid has no failure mode.
    let me = unsafe { libc::getuid() };
    let exe = peer_of(socket).filter(|(uid, _)| *uid == me).and_then(|(_, pid)| exe_of(pid)).filter(|exe| trusted_exe(exe));
    match exe {
        Some(exe) if client_version(&exe).as_deref() == Ok(theirs) => Ok(exe),
        _ => Err(format!("The agents' tmux server is {theirs} and the app's is {ours}: a tmux client only attaches to a server of its own version, and the server's own tmux couldn't be found.")),
    }
}

fn clamp(n: u16) -> u16 {
    n.clamp(1, MAX_SIZE)
}

/// The pane option brindle's CLI marks its sidebar pane with (its value, a
/// session's id, is never empty).
pub const SIDEBAR_OPTION: &str = "@brindle_sidebar";

/// Columns of the view's window the app doesn't show: brindle's CLI sidebar
/// pane and the border beside it, at the window's left or right. A person's
/// terminal on the window shows it; the app shows the agent's pane alone.
/// The view's client is `cols` columns (the sidebar's and its border's)
/// wider than the view, and the webview (PtyTerminal) shows only the view's
/// width of it, from the client's column `left` on, so the agent's pane
/// fills it. Both are the client's columns, as tmux draws the window on it:
/// from the client's first column where the window is no wider than the
/// client, panned to where its cursor is where it is wider (a wider terminal
/// sharing the window typed last), and then less of the sidebar, or none of
/// it, is on the client.
///
/// `width`: the agent's columns shown from `left` on, where fewer than the
/// view's. A narrower terminal having the window makes the agent's pane
/// narrower than the view, and a sidebar at its right then sits inside the
/// view: the webview masks every column past them. The client's size, and
/// so who has the window, is as it was.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
pub struct Crop {
    pub cols: u16,
    pub left: u16,
    pub width: Option<u16>,
}

/// What `crop_of` reads, a line a pane (`list-panes -F`).
pub const LAYOUT_FORMAT: &str = "#{@brindle_sidebar}\t#{pane_left}\t#{pane_right}\t#{pane_top}\t#{pane_bottom}\t#{window_width}\t#{window_height}\t#{window_zoomed_flag}\t#{pane_active}\t#{cursor_x}";

/// What `crop_now` reads of the view's client, a line a client (`list-clients -F`): its pid, its
/// width, and how far into a window wider than it tmux has panned.
const CLIENT_FORMAT: &str = "#{client_pid}\t#{client_width}\t#{window_offset_x}";

/// The crop for a window's panes as `LAYOUT_FORMAT` lists them, for a view
/// `visible` columns wide, on a client `client` (its width, and how far
/// tmux has panned the window in it) when it is there to say: a sidebar
/// pane the window's full height at its left or right edge, beside another
/// pane, is cropped with its border. None while a pane is zoomed (the
/// sidebar isn't drawn), for a sidebar along the bottom, or one alone.
pub fn crop_of(panes: &str, visible: u16, client: Option<(u16, u16)>) -> Crop {
    let num = |s: &str| s.parse::<u16>().ok();
    let panes: Vec<Vec<&str>> = panes.lines().map(|line| line.split('\t').collect()).filter(|fields: &Vec<&str>| fields.len() == 10).collect();
    for fields in &panes {
        let [sidebar, left, right, top, bottom, width, height, zoomed, ..] = fields[..] else { continue };
        if sidebar.is_empty() || zoomed == "1" {
            continue;
        }
        let (Some(left), Some(right), Some(top), Some(bottom), Some(width), Some(height)) = (num(left), num(right), num(top), num(bottom), num(width), num(height)) else { continue };
        if top != 0 || bottom + 1 != height {
            continue;
        }
        // The client's columns the sidebar and its border take, and the agent's columns of the window (end exclusive).
        let (cols, agent) = if left == 0 && right + 1 < width {
            (right + 2, (right + 2, width))
        } else if left > 0 && right + 1 == width {
            (width - left + 1, (0, left - 1))
        } else {
            continue;
        };
        let (client_width, panned) = client.unwrap_or((clamp(visible.saturating_add(cols)), 0));
        // The agent's columns of the client: the window drawn from the client's first column, `panned` columns into it.
        let (from, to) = (agent.0.saturating_sub(panned).min(client_width), agent.1.saturating_sub(panned).min(client_width));
        let shown = to - from;
        let mut start = from;
        if shown > visible {
            // Panned: more of the agent's columns are on the client than the view shows. Its cursor kept in view, as
            // tmux keeps it on the client.
            let cursor = panes.iter().find(|f| f[8] == "1" && f[0].is_empty()).and_then(|f| Some(num(f[1])? + num(f[9])?));
            if let Some(at) = cursor.and_then(|at| at.checked_sub(panned)).filter(|&at| at >= from + visible) {
                start = (at + 1 - visible).min(to - visible);
            }
        }
        return Crop { cols, left: start, width: (shown < visible).then_some(shown) };
    }
    Crop::default()
}

/// The crop of the view session `view`'s window for a view `visible` columns wide (its client
/// that and the crop's columns), as it is now, if tmux can say (the session is there). `client`:
/// the pid of the view's client, once it is attached.
fn crop_now(tmux_bin: &Path, server: &[String], view: &str, visible: u16, client: Option<u32>) -> Option<Crop> {
    let out = tmux(tmux_bin).args(server).args(["list-panes", "-t", &format!("={view}:"), "-F", LAYOUT_FORMAT]).output().ok().filter(|out| out.status.success())?;
    let panes = String::from_utf8_lossy(&out.stdout);
    let on = client.and_then(|pid| {
        let out = tmux(tmux_bin).args(server).args(["list-clients", "-t", &format!("={view}"), "-F", CLIENT_FORMAT]).output().ok().filter(|out| out.status.success())?;
        let clients = String::from_utf8_lossy(&out.stdout).into_owned();
        clients.lines().find_map(|line| {
            let fields: Vec<&str> = line.split('\t').collect();
            let [at, width, panned] = fields[..] else { return None };
            (at.parse::<u32>().ok()? == pid).then_some((width.parse().ok()?, panned.parse().unwrap_or(0)))
        })
    });
    Some(crop_of(&panes, visible, on))
}

/// The pty `master` set to `size` (the view's own, in cells) and `crop`'s columns besides.
#[cfg(unix)]
fn set_size(master: &File, size: (u16, u16), crop: Crop) -> Result<(), String> {
    use std::os::fd::AsRawFd;
    let winsize = libc::winsize { ws_row: clamp(size.1), ws_col: clamp(size.0.saturating_add(crop.cols)), ws_xpixel: 0, ws_ypixel: 0 };
    // SAFETY: TIOCSWINSZ reads a winsize that outlives the call.
    if unsafe { libc::ioctl(master.as_raw_fd(), libc::TIOCSWINSZ as _, &winsize) } == -1 {
        return Err(io::Error::last_os_error().to_string());
    }
    Ok(())
}

/// What the client's reader hands `forward`: bytes as one read gave them, or the client's exit code, last.
pub enum Out {
    Bytes(Vec<u8>),
    Exit(i32),
}

/// A synchronized update's start and end (DEC 2026): tmux brackets each of
/// its redraws so for a client with the "sync" feature, as the view's has.
const SYNC_START: &[u8] = b"\x1b[?2026h";
const SYNC_END: &[u8] = b"\x1b[?2026l";
/// Outside a synchronized update, what follows a read within this long goes
/// with it, up to `GATHER_MAX` from the first byte.
const QUIET: std::time::Duration = std::time::Duration::from_millis(1);
const GATHER_MAX: std::time::Duration = std::time::Duration::from_millis(8);
/// Inside one, what comes before its end goes with it (the terminal paints
/// none of it before the end), up to this long from the first byte.
const SYNC_MAX: std::time::Duration = std::time::Duration::from_millis(50);
/// The most bytes sent as one.
const GATHER_BYTES: usize = 1 << 18;

/// Whether a synchronized update is open after `bytes`, given whether one was
/// before them; `before`: the bytes just ahead of them (the last few are
/// enough), for a marker split between the two.
pub fn sync_open_after(open: bool, before: &[u8], bytes: &[u8]) -> bool {
    let keep = before.len().min(SYNC_START.len() - 1);
    let seen: Vec<u8> = before[before.len() - keep..].iter().chain(bytes).copied().collect();
    let last = |marker: &[u8]| seen.windows(marker.len()).rposition(|w| w == marker);
    match (last(SYNC_START), last(SYNC_END)) {
        (None, None) => open,
        (start, end) => start > end,
    }
}

/// The client's bytes to `on_data`, a redraw at a time, then its exit code to
/// `on_exit`. A macOS pty hands over a kilobyte a read, and tmux writes a
/// redraw in as many pieces as it takes (a few kilobytes for a Claude Code
/// keystroke's: tmux redraws the whole pane each time a program updates it
/// synchronized), or a burst of a few bytes each: sent on as they come, each
/// is a message of its own to the webview (in the app, a script the main
/// thread runs, the thread every key typed goes through), hundreds in a
/// tenth of a second while an agent works. Gathered, a redraw is one: all of
/// a synchronized update (nothing of it is painted before its end anyway),
/// and outside one, what comes within a millisecond of the last read.
pub fn forward(reads: mpsc::Receiver<Out>, on_data: impl FnMut(&[u8]), on_exit: impl FnOnce(i32)) {
    forward_from(reads, on_data, on_exit)
}

/// Where `forward` gets the client's reads and the time: a channel and the
/// clock, or in tests, a script of reads in time of its own.
trait Reads {
    fn now(&self) -> std::time::Instant;
    /// The next read, waiting for it at most `wait` (`None`: as long as it
    /// takes); `None` if none came, or none ever will.
    fn next(&mut self, wait: Option<std::time::Duration>) -> Option<Out>;
}

impl Reads for mpsc::Receiver<Out> {
    fn now(&self) -> std::time::Instant {
        std::time::Instant::now()
    }

    fn next(&mut self, wait: Option<std::time::Duration>) -> Option<Out> {
        match wait {
            Some(wait) => self.recv_timeout(wait).ok(),
            None => self.recv().ok(),
        }
    }
}

fn forward_from(mut reads: impl Reads, mut on_data: impl FnMut(&[u8]), on_exit: impl FnOnce(i32)) {
    let mut open = false;
    // The stream's last bytes, whichever message they went in: a marker split between reads, a message cut mid-marker
    // (at `SYNC_MAX` or `GATHER_BYTES`) included, is read whole however many reads it takes.
    let mut tail: Vec<u8> = Vec::new();
    let mut read = |open: bool, bytes: &[u8]| {
        let open = sync_open_after(open, &tail, bytes);
        tail.extend_from_slice(&bytes[bytes.len().saturating_sub(SYNC_START.len() - 1)..]);
        tail.drain(..tail.len().saturating_sub(SYNC_START.len() - 1));
        open
    };
    let mut exit = None;
    while exit.is_none() {
        let mut bytes = match reads.next(None) {
            Some(Out::Bytes(bytes)) => bytes,
            Some(Out::Exit(code)) => {
                exit = Some(code);
                break;
            }
            None => break,
        };
        open = read(open, &bytes);
        let first = reads.now();
        while bytes.len() < GATHER_BYTES {
            let until = first + if open { SYNC_MAX } else { GATHER_MAX };
            let wait = if open { until.saturating_duration_since(reads.now()) } else { QUIET.min(until.saturating_duration_since(reads.now())) };
            match reads.next(Some(wait)) {
                Some(Out::Bytes(more)) => {
                    open = read(open, &more);
                    bytes.extend_from_slice(&more);
                }
                Some(Out::Exit(code)) => {
                    exit = Some(code);
                    break;
                }
                None => break,
            }
            if reads.now() >= until {
                break;
            }
        }
        on_data(&bytes);
    }
    on_exit(exit.unwrap_or(-1));
}

/// How long a mouse button's release waits behind the input just before it.
/// A drag's last motion and its release read by tmux together: the release's
/// copy (copy mode's `copy-pipe-and-cancel`) lands while the pane waits to be
/// redrawn for the motion, and tmux (3.8) sends that pane's clipboard write
/// (OSC 52) to no client: the buffer is filled, the app's clipboard isn't. A
/// release that comes after tmux has read and drawn the motion copies as it
/// should. On real tmux, any separate write was enough; with none, every
/// drag's copy was lost (a_drag_released_with_its_last_motion_copies_as_osc_52).
pub const RELEASE_GAP: std::time::Duration = std::time::Duration::from_millis(15);

/// Where in `data` each SGR mouse release (`ESC [ < b ; x ; y m`) starts.
fn sgr_releases(data: &[u8]) -> Vec<usize> {
    let mut at = Vec::new();
    let mut i = 0;
    while i + 3 <= data.len() {
        if data[i..].starts_with(b"\x1b[<") {
            let end = data[i + 3..].iter().position(|b| !(b.is_ascii_digit() || *b == b';')).map(|n| i + 3 + n);
            if let Some(end) = end {
                if data[end] == b'm' && end > i + 3 {
                    at.push(i);
                }
                i = end;
                continue;
            }
            break;
        }
        i += 1;
    }
    at
}

/// What the person typed, to the view's client: as it comes, but each mouse
/// release `RELEASE_GAP` after the write before it, so tmux reads it apart.
/// `last`: when the last write went.
fn write_input(writer: &mut impl Write, data: &[u8], last: &mut Option<std::time::Instant>) -> io::Result<()> {
    // What's before each release goes, then the release (and what follows it) once the gap has passed.
    let mut from = 0;
    for at in sgr_releases(data) {
        if from < at {
            writer.write_all(&data[from..at])?;
            *last = Some(std::time::Instant::now());
        }
        if let Some(wait) = last.and_then(|t| RELEASE_GAP.checked_sub(t.elapsed())) {
            std::thread::sleep(wait);
        }
        from = at;
    }
    if from < data.len() {
        writer.write_all(&data[from..])?;
        *last = Some(std::time::Instant::now());
    }
    Ok(())
}

/// One open view: a tmux client on a pty of its own.
pub struct PtyView {
    tmux: PathBuf,
    server: Vec<String>,
    view: String,
    master: Arc<File>,
    /// The view's size in cells, and the columns cropped off it (`Crop`):
    /// the client's pty is the two together.
    size: Arc<Mutex<((u16, u16), Crop)>>,
    input: mpsc::Sender<Vec<u8>>,
    /// The client's pid, and its tty (tmux's name for it) once looked up.
    client: u32,
    client_tty: Arc<OnceLock<String>>,
}

/// What giving the view the window takes (`PtyView::take_window`), apart
/// from the view: run off the views' lock, which keys wait on.
pub struct WindowTaker {
    tmux: PathBuf,
    server: Vec<String>,
    view: String,
    client: u32,
    client_tty: Arc<OnceLock<String>>,
}

impl PtyView {
    /// Make the view session and attach a client to it. `cols` x `rows` is
    /// the view's size. `on_crop` gets the window's `Crop` before any of the
    /// client's bytes, and again whenever it changes (the client's pty is
    /// resized to the view's size and the crop as it is told); `on_data` the
    /// client's bytes, a redraw at a time (`forward`); `on_exit` its exit code, once.
    #[cfg(unix)]
    #[allow(clippy::too_many_arguments)]
    pub fn open(
        tmux_bin: &Path,
        server: Option<&str>,
        target: &str,
        cols: u16,
        rows: u16,
        on_data: impl Fn(&[u8]) + Send + 'static,
        on_crop: impl Fn(Crop) + Send + 'static,
        on_exit: impl FnOnce(i32) + Send + 'static,
    ) -> Result<PtyView, String> {
        let server = server_args(server)?;
        if !valid_target(target) {
            return Err(format!("not a tmux target: {target}"));
        }
        let ours = client_version(tmux_bin)?;
        check_version(&ours, "The app's tmux")?;
        let running = tmux(tmux_bin).args(&server).args(["display-message", "-p", "#{version}"]).output().map_err(|e| format!("couldn't run tmux: {e}"))?;
        if !running.status.success() {
            return Err(format!("the agent's tmux server isn't there: {}", String::from_utf8_lossy(&running.stderr).trim()));
        }
        let version = String::from_utf8_lossy(&running.stdout).trim().to_string();
        let theirs = format!("tmux {version}");
        check_version(&theirs, "The agents' tmux server")?;
        let client = client_for(tmux_bin, &ours, &socket_path(&server), &theirs)?;
        Self::open_on(&client, server, parse_version(&version).expect("checked"), target, cols, rows, on_data, on_crop, on_exit)
    }

    /// `open` on a server of `version` (its `#{version}`, checked).
    #[cfg(unix)]
    #[allow(clippy::too_many_arguments)]
    fn open_on(
        tmux_bin: &Path,
        server: Vec<String>,
        version: (u32, u32),
        target: &str,
        cols: u16,
        rows: u16,
        on_data: impl Fn(&[u8]) + Send + 'static,
        on_crop: impl Fn(Crop) + Send + 'static,
        on_exit: impl FnOnce(i32) + Send + 'static,
    ) -> Result<PtyView, String> {
        clear_stale(tmux_bin, &server);
        let (cols, rows) = (clamp(cols), clamp(rows));
        let view = format!("{VIEW_PREFIX}{}-{}", std::process::id(), NEXT_VIEW.fetch_add(1, Ordering::Relaxed));
        // The view's key table goes in on stdin: the bindings that server's tmux has.
        let mut prepare = tmux(tmux_bin)
            .args(&server)
            .args(prepare_args(&view, target, cols, rows))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("couldn't run tmux: {e}"))?;
        if let Some(mut stdin) = prepare.stdin.take() {
            let _ = stdin.write_all(view_bindings(version).as_bytes());
        }
        let out = prepare.wait_with_output().map_err(|e| format!("couldn't run tmux: {e}"))?;
        if !out.status.success() {
            let _ = tmux(tmux_bin).args(&server).args(["kill-session", "-t", &format!("={view}")]).output();
            return Err(format!("tmux couldn't make the view: {}", String::from_utf8_lossy(&out.stderr).trim()));
        }
        // The client attaches at the view's size and the sidebar's columns, from its first frame.
        let crop = crop_now(tmux_bin, &server, &view, cols, None).unwrap_or_default();
        let mut cmd = Command::new(tmux_bin);
        cmd.args(&server).args(attach_args(&view)).env("TERM", "xterm-256color").env_remove("TMUX");
        let crate::setup::Term { output: mut reader, input: mut writer, master, mut child } =
            crate::setup::spawn_terminal_sized(cmd, clamp(cols.saturating_add(crop.cols)), rows).map_err(|e| format!("couldn't attach: {e}"))?;
        let master = Arc::new(master.ok_or("no pty")?);
        let size = Arc::new(Mutex::new(((cols, rows), crop)));
        on_crop(crop);
        let client = child.id();
        // The crop looked at again after the client draws: a layout change (a sidebar
        // split, moved, closed, a pane zoomed) is redrawn, and so is the window panned
        // on the client, so each is followed by bytes. One look at a time, a little
        // after the bytes that asked for it; and each second besides, for a sidebar
        // marked (its pane option set) after it was drawn, which redraws nothing. Until
        // the client's output ends.
        let (redrawn, looks) = mpsc::sync_channel::<()>(1);
        {
            let (tmux_bin, server, view, master, size) = (tmux_bin.to_path_buf(), server.clone(), view.clone(), master.clone(), size.clone());
            std::thread::spawn(move || {
                while !matches!(looks.recv_timeout(std::time::Duration::from_secs(1)), Err(mpsc::RecvTimeoutError::Disconnected)) {
                    std::thread::sleep(std::time::Duration::from_millis(50));
                    while looks.try_recv().is_ok() {}
                    let visible = size.lock().unwrap_or_else(|e| e.into_inner()).0 .0;
                    let Some(crop) = crop_now(&tmux_bin, &server, &view, visible, Some(client)) else { continue };
                    let mut size = size.lock().unwrap_or_else(|e| e.into_inner());
                    if size.1 != crop {
                        let resized = size.1.cols != crop.cols;
                        size.1 = crop;
                        // Told first: the bytes drawn at the new size come after.
                        on_crop(crop);
                        if resized {
                            let _ = set_size(&master, size.0, crop);
                        }
                    }
                    drop(size);
                    std::thread::sleep(std::time::Duration::from_millis(50));
                }
            });
        }
        let (input, keys) = mpsc::channel::<Vec<u8>>();
        // Keys on a thread of their own: a write to a client that isn't reading blocks, and a mouse release waits
        // its gap behind the motion before it (`write_input`).
        std::thread::spawn(move || {
            let mut last = None;
            for data in keys {
                if write_input(&mut writer, &data, &mut last).is_err() {
                    break;
                }
            }
        });
        // The client's bytes as the pty gives them (a macOS pty a kilobyte a read), gathered (`forward`) on a thread
        // of their own, so the reader goes straight back to reading.
        let (read, gathered) = mpsc::channel::<Out>();
        std::thread::spawn(move || {
            let mut buf = vec![0u8; 1 << 16];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) => break,
                    Ok(n) => {
                        if read.send(Out::Bytes(buf[..n].to_vec())).is_err() {
                            break;
                        }
                    }
                    Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                    // EIO on Linux once the client's side has closed.
                    Err(_) => break,
                }
            }
            let _ = read.send(Out::Exit(child.wait().ok().and_then(|s| s.code()).unwrap_or(-1)));
        });
        std::thread::spawn(move || {
            forward(gathered, |bytes| {
                on_data(bytes);
                let _ = redrawn.try_send(());
            }, on_exit)
        });
        Ok(PtyView { tmux: tmux_bin.to_path_buf(), server, view, master, size, input, client, client_tty: Arc::default() })
    }

    #[cfg(not(unix))]
    #[allow(clippy::too_many_arguments)]
    pub fn open(
        _tmux: &Path,
        _server: Option<&str>,
        _target: &str,
        _cols: u16,
        _rows: u16,
        _on_data: impl Fn(&[u8]) + Send + 'static,
        _on_crop: impl Fn(Crop) + Send + 'static,
        _on_exit: impl FnOnce(i32) + Send + 'static,
    ) -> Result<PtyView, String> {
        Err("the pty view is macOS and Linux only for now (Windows: the engine's WSL bridge)".into())
    }

    /// What the person typed (keys, a paste, mouse reports), as xterm.js says it.
    pub fn write(&self, data: &[u8]) {
        let _ = self.input.send(data.to_vec());
    }

    /// The view is `cols` x `rows` cells: its client's pty is that and the crop's columns.
    pub fn resize(&self, cols: u16, rows: u16) -> Result<(), String> {
        let mut size = self.size.lock().unwrap_or_else(|e| e.into_inner());
        size.0 = (clamp(cols), clamp(rows));
        #[cfg(unix)]
        return set_size(&self.master, size.0, size.1);
        #[cfg(not(unix))]
        Ok(())
    }

    /// The columns of the window cropped off the view, as last told (`on_crop`).
    pub fn crop(&self) -> Crop {
        self.size.lock().unwrap_or_else(|e| e.into_inner()).1
    }

    /// The person is at the view (it took focus, or was clicked): the
    /// window takes the view's size, as it would had they typed in it. With
    /// tmux's `window-size latest` the window is the size of the client used
    /// last, and only a key, a mouse report or a resize counts as using one
    /// (and focus, which tmux hears only with the global `focus-events` on).
    /// Switching the view's own client to the session it is already on makes
    /// it the window's latest too (3.2's cmd-switch-client.c, later tmux's
    /// server_client_set_session), and sends the pane nothing. A person
    /// typing in their own terminal takes the window back, as ever.
    pub fn take_window(&self) -> Result<(), String> {
        self.window_taker().take()
    }

    pub fn window_taker(&self) -> WindowTaker {
        WindowTaker {
            tmux: self.tmux.clone(),
            server: self.server.clone(),
            view: self.view.clone(),
            client: self.client,
            client_tty: self.client_tty.clone(),
        }
    }

    pub fn name(&self) -> &str {
        &self.view
    }
}

impl WindowTaker {
    /// `PtyView::take_window`.
    pub fn take(&self) -> Result<(), String> {
        let tty = match self.client_tty.get() {
            Some(tty) => tty,
            None => {
                // A view just opened (the webview takes the window as it opens) may not have its
                // client attached yet: looked for a little while.
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
                let tty = loop {
                    let out = tmux(&self.tmux)
                        .args(&self.server)
                        .args(["list-clients", "-t", &format!("={}", self.view), "-F", "#{client_pid} #{client_tty}"])
                        .output()
                        .map_err(|e| format!("couldn't run tmux: {e}"))?;
                    let listed = String::from_utf8_lossy(&out.stdout);
                    // Only the app's own client: the one it started, on the view's own session.
                    let pid = self.client.to_string();
                    if let Some(tty) = listed.lines().find_map(|l| l.split_once(' ').filter(|(p, _)| *p == pid).map(|(_, tty)| tty.to_string())) {
                        break tty;
                    }
                    if std::time::Instant::now() > deadline {
                        return Err(format!("the view's client isn't attached: {}", String::from_utf8_lossy(&out.stderr).trim()));
                    }
                    std::thread::sleep(std::time::Duration::from_millis(25));
                };
                self.client_tty.get_or_init(|| tty)
            }
        };
        // -E: the view session's environment stays as it is.
        let out = tmux(&self.tmux)
            .args(&self.server)
            .args(["switch-client", "-E", "-c", tty, "-t", &format!("={}", self.view)])
            .output()
            .map_err(|e| format!("couldn't run tmux: {e}"))?;
        if !out.status.success() {
            return Err(format!("tmux couldn't give the view the window: {}", String::from_utf8_lossy(&out.stderr).trim()));
        }
        Ok(())
    }
}

impl Drop for PtyView {
    /// The view session goes, which detaches its client (it exits, and the
    /// reader with it). The agent's window stays in the agent's session.
    fn drop(&mut self) {
        let _ = tmux(&self.tmux)
            .args(&self.server)
            .args(["kill-session", "-t", &format!("={}", self.view)])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
}

/// The open views, by the id the webview holds, and the ones being opened.
#[derive(Default)]
pub struct PtyViews {
    inner: Mutex<Views>,
}

#[derive(Default)]
struct Views {
    /// The page's: one more each time the webview loads a page (`close_all`).
    generation: u64,
    /// Asked for and still opening, claimed for the page that asked, until
    /// registered or closed.
    pending: std::collections::HashSet<u32>,
    open: HashMap<u32, PtyView>,
}

/// A view being opened: its id and the page generation it was asked in.
#[derive(Clone, Copy, Debug)]
pub struct Claim {
    pub id: u32,
    generation: u64,
}

impl PtyViews {
    fn lock(&self) -> std::sync::MutexGuard<'_, Views> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// A view asked for: its id, claimed for the page now loaded. Run as the
    /// page's ask is dispatched, before another page can load.
    pub fn claim(&self) -> Claim {
        let mut views = self.lock();
        let id = NEXT_VIEW.fetch_add(1, Ordering::Relaxed);
        views.pending.insert(id);
        Claim { id, generation: views.generation }
    }

    /// The view `claim` opened, kept if its page is still the one loaded and
    /// hasn't closed it meanwhile; else handed back, for the caller to drop
    /// (which takes its session, and its client with it).
    pub fn register(&self, claim: Claim, view: PtyView) -> Result<(), PtyView> {
        let mut views = self.lock();
        if views.generation != claim.generation || !views.pending.remove(&claim.id) {
            return Err(view);
        }
        views.open.insert(claim.id, view);
        Ok(())
    }

    /// A claim whose view couldn't be opened.
    pub fn abandon(&self, claim: Claim) {
        self.lock().pending.remove(&claim.id);
    }

    /// Closed by its page: the view, if open (or None while it is still
    /// opening: it won't be registered).
    pub fn close(&self, id: u32) -> Option<PtyView> {
        let mut views = self.lock();
        views.pending.remove(&id);
        views.open.remove(&id)
    }

    pub fn with<T>(&self, id: u32, f: impl FnOnce(&PtyView) -> T) -> Option<T> {
        self.lock().open.get(&id).map(f)
    }

    /// The webview (re)loads its page: every view the last page opened goes,
    /// and any it asked for that is still opening is closed once it opens.
    pub fn close_all(&self) {
        let open = {
            let mut views = self.lock();
            views.generation += 1;
            views.pending.clear();
            std::mem::take(&mut views.open)
        };
        drop(open);
    }
}

/// What an open view sends the webview: it opened (writes and resizes may
/// follow), the window's crop (before the client's first bytes, and on each
/// change), the client's bytes (base64), its exit, or why it couldn't open.
#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum PtyViewEvent {
    Open,
    Crop { cols: u16, left: u16, width: Option<u16> },
    Data { data: String },
    Exit { code: i32 },
    Error { message: String },
}

/// Whether the webview can show panes through the pty view, and if it can't, why.
#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum PtyStatus {
    Ready { tmux: String },
    /// The pty view can't work here (tmux too old, or missing; Windows, until its bridge): the message says so.
    Unusable { message: String },
}

/// What a Windows build says until the engine's WSL bridge carries its views.
pub const NOT_ON_WINDOWS: &str = "Sessions can't be shown in the app on Windows yet.";

pub fn status(tmux_bin: &Path) -> PtyStatus {
    if cfg!(not(unix)) {
        return PtyStatus::Unusable { message: NOT_ON_WINDOWS.into() };
    }
    match client_version(tmux_bin).and_then(|v| check_version(&v, "The app's tmux").map(|()| v)) {
        Ok(tmux) => PtyStatus::Ready { tmux },
        Err(message) => PtyStatus::Unusable { message },
    }
}

#[tauri::command]
pub async fn pty_view_status() -> Result<PtyStatus, String> {
    tauri::async_runtime::spawn_blocking(|| status(&tmux_binary())).await.map_err(|e| e.to_string())
}

/// Attach a view to a pane (`server`, `target`: an agent's `tmux_server` and
/// `window`, or an action terminal's `server` and `target`) and answer its id at once. On `channel`: `open` once it is
/// (then its bytes, then its exit), or `error`.
///
/// Not async, as `daemon_stream_open` isn't: an async command's body runs
/// only once the runtime polls it, by when the page that asked may have gone
/// (a page load ran between), and a view opened then would belong to no
/// page. This one claims the id for the page as its ask is dispatched, where
/// page loads are told too (lib.rs on_page_load); the opening is a thread's,
/// and a view whose page has gone, or closed it, by the time it opens is
/// dropped there, its session and client with it.
#[tauri::command]
pub fn pty_view_open(
    app: tauri::AppHandle,
    views: State<'_, PtyViews>,
    server: Option<String>,
    target: String,
    cols: u16,
    rows: u16,
    channel: Channel<PtyViewEvent>,
) -> Result<u32, String> {
    use tauri::Manager;
    let claim = views.claim();
    std::thread::spawn(move || {
        let (data, cropped, exit) = (channel.clone(), channel.clone(), channel.clone());
        let opened = PtyView::open(
            &tmux_binary(),
            server.as_deref(),
            &target,
            cols,
            rows,
            move |bytes| {
                let _ = data.send(PtyViewEvent::Data { data: base64(bytes) });
            },
            move |Crop { cols, left, width }| {
                let _ = cropped.send(PtyViewEvent::Crop { cols, left, width });
            },
            move |code| {
                let _ = exit.send(PtyViewEvent::Exit { code });
            },
        );
        let views = app.state::<PtyViews>();
        match opened {
            Ok(view) => match views.register(claim, view) {
                Ok(()) => {
                    let _ = channel.send(PtyViewEvent::Open);
                }
                Err(stale) => drop(stale),
            },
            Err(message) => {
                views.abandon(claim);
                let _ = channel.send(PtyViewEvent::Error { message });
            }
        }
    });
    Ok(claim.id)
}

#[tauri::command]
pub fn pty_view_write(views: State<'_, PtyViews>, id: u32, data: String) -> Result<(), String> {
    views.with(id, |view| view.write(data.as_bytes()));
    Ok(())
}

/// The view is `cols` x `rows` cells (the agent's pane, without the crop's columns).
#[tauri::command]
pub fn pty_view_resize(views: State<'_, PtyViews>, id: u32, cols: u16, rows: u16) -> Result<(), String> {
    views.with(id, |view| view.resize(cols, rows)).unwrap_or(Ok(()))
}

/// The view took focus, or was clicked: the window takes its size
/// (`PtyView::take_window`).
#[tauri::command]
pub async fn pty_view_focus(views: State<'_, PtyViews>, id: u32) -> Result<(), String> {
    let Some(taker) = views.with(id, PtyView::window_taker) else { return Ok(()) };
    // tmux runs as a process: not on the main thread, nor under the views' lock.
    tauri::async_runtime::spawn_blocking(move || taker.take()).await.map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn pty_view_close(views: State<'_, PtyViews>, id: u32) -> Result<(), String> {
    let view = views.close(id);
    // kill-session runs a tmux process: not on the main thread.
    tauri::async_runtime::spawn_blocking(move || drop(view)).await.map_err(|e| e.to_string())
}

/// The most a view copies at once (an OSC 52 tmux sent on).
pub const MAX_COPY: usize = 1 << 20;

/// What tmux copied (OSC 52, xterm's clipboard addon) onto the clipboard,
/// through the app: the webview itself may neither write nor read it.
#[tauri::command]
pub fn pty_view_copy(app: tauri::AppHandle, text: String) -> Result<(), String> {
    use tauri_plugin_clipboard_manager::ClipboardExt;
    if text.len() > MAX_COPY {
        return Err(format!("not copied: {} bytes is more than a view copies ({MAX_COPY})", text.len()));
    }
    app.clipboard().write_text(text).map_err(|e| e.to_string())
}

/// What the clipboard holds, for a view's ⌘V (the person pasting): the
/// webview can't read it. Never asked for by a program in the pane (OSC 52
/// reads are refused in the view).
#[tauri::command]
pub fn pty_view_paste(app: tauri::AppHandle) -> Result<String, String> {
    use tauri_plugin_clipboard_manager::ClipboardExt;
    app.clipboard().read_text().map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    #[test]
    fn a_server_is_named_by_its_socket_its_name_or_not_at_all_for_the_default_one() {
        // The default server: no server, or `default` in any case.
        let none: [&str; 0] = [];
        for default in [None, Some(""), Some("  "), Some("default"), Some("DEFAULT"), Some("Default")] {
            assert_eq!(server_args(default).unwrap(), none, "{default:?}");
        }
        // Its socket path is a path like any other.
        assert_eq!(server_args(Some("/private/tmp/tmux-501/default")).unwrap(), ["-S", "/private/tmp/tmux-501/default"]);
        assert_eq!(server_args(Some("/tmp/tmux-501/brindle")).unwrap(), ["-S", "/tmp/tmux-501/brindle"]);
        assert_eq!(server_args(Some("/tmp/brindle/agents.sock")).unwrap(), ["-S", "/tmp/brindle/agents.sock"]);
        assert_eq!(server_args(Some("brindle")).unwrap(), ["-L", "brindle"]);
        assert_eq!(server_args(Some("ptyspike-42")).unwrap(), ["-L", "ptyspike-42"]);
        // A name that isn't one is still refused: never a flag, a command or a space.
        for bad in ["a b", "x;kill-server", "a\tb", "$(id)"] {
            assert!(server_args(Some(bad)).is_err(), "{bad}");
        }
        // The default server's socket is where tmux puts it.
        #[cfg(unix)]
        assert_eq!(socket_path(&server_args(None).unwrap()), socket_of("default"));
    }

    #[test]
    fn a_target_is_never_a_flag_or_a_command() {
        assert!(valid_target("brindle-x:@3"));
        assert!(valid_target("%12"));
        assert!(!valid_target("-t"));
        assert!(!valid_target("a;kill-server"));
        assert!(!valid_target("a b"));
    }

    #[test]
    fn a_synchronized_update_is_open_from_its_start_to_its_end_a_marker_split_between_reads_too() {
        assert!(!sync_open_after(false, b"", b"plain"));
        assert!(sync_open_after(false, b"", b"\x1b[?2026hhalf a redraw"));
        assert!(!sync_open_after(true, b"", b"the rest\x1b[?2026l"));
        assert!(sync_open_after(true, b"", b"more of it"));
        assert!(sync_open_after(false, b"", b"\x1b[?2026lone\x1b[?2026l\x1b[?2026htwo"));
        assert!(!sync_open_after(false, b"", b"\x1b[?2026hone\x1b[?2026l"));
        // The end's first bytes in one read, the rest in the next.
        assert!(!sync_open_after(true, b"redraw\x1b[?20", b"26l"));
        assert!(sync_open_after(false, b"\x1b[", b"?2026hredraw"));

        // Split across two messages `forward` sends: an update held to `SYNC_MAX` is cut with its end's first bytes;
        // the rest closes it all the same, so what comes 10 ms later goes on by itself, not held with it.
        let (sent, _) = forwarded(vec![(0, b"\x1b[?2026hredraw\x1b[?20"), (70, b"26l"), (10, b"next")]);
        assert_eq!(sent, vec![b"\x1b[?2026hredraw\x1b[?20".to_vec(), b"26l".to_vec(), b"next".to_vec()]);
        // A start's first bytes sent on alone (a quiet gap): the rest opens it, and the update is held to its end.
        let (sent, _) = forwarded(vec![(0, b"\x1b["), (10, b"?2026hrow 1 "), (10, b"row 2\x1b[?2026l")]);
        assert_eq!(sent, vec![b"\x1b[".to_vec(), b"?2026hrow 1 row 2\x1b[?2026l".to_vec()]);
        // Cut at `GATHER_BYTES` mid-marker: the same (paused longer: a debug build takes a while over a quarter megabyte).
        let big: &'static [u8] = Box::leak([b"\x1b[?2026h".to_vec(), vec![b'x'; GATHER_BYTES - 8 - 3], b"\x1b[?".to_vec()].concat().into_boxed_slice());
        let (sent, _) = forwarded(vec![(0, big), (150, b"2026l"), (60, b"next")]);
        assert_eq!(sent.len(), 3, "{:?}", sent.iter().map(|s| s.len()).collect::<Vec<_>>());
        assert_eq!(sent[1], b"2026l");
        // Cut at `SYNC_MAX` after `ESC[?`, the rest in two reads of the next message: they still close it.
        let (sent, _) = forwarded(vec![(0, b"\x1b[?2026hredraw\x1b[?"), (70, b"20"), (0, b"26l"), (10, b"next")]);
        assert_eq!(sent, vec![b"\x1b[?2026hredraw\x1b[?".to_vec(), b"2026l".to_vec(), b"next".to_vec()]);
        // An end in three reads of one message.
        let (sent, _) = forwarded(vec![(0, b"\x1b[?2026hredraw\x1b"), (5, b"[?2"), (5, b"02"), (5, b"6l"), (20, b"next")]);
        assert_eq!(sent, vec![b"\x1b[?2026hredraw\x1b[?2026l".to_vec(), b"next".to_vec()]);
        // A start's `ESC` sent on alone, the rest in three reads of the next message: it opens, and holds what follows.
        let (sent, _) = forwarded(vec![(0, b"\x1b"), (10, b"[?"), (0, b"20"), (0, b"26hrow 1 "), (10, b"row 2\x1b[?2026l")]);
        assert_eq!(sent, vec![b"\x1b".to_vec(), b"[?2026hrow 1 row 2\x1b[?2026l".to_vec()]);
    }

    /// Reads, each at its time, in time that passes only as `forward` waits: however busy the machine, each pause is exact.
    struct Script {
        start: Instant,
        now: Duration,
        reads: std::collections::VecDeque<(Duration, Out)>,
    }

    impl Reads for Script {
        fn now(&self) -> Instant {
            self.start + self.now
        }

        fn next(&mut self, wait: Option<Duration>) -> Option<Out> {
            let at = self.reads.front()?.0;
            match wait {
                Some(wait) if at > self.now + wait => {
                    self.now += wait;
                    None
                }
                _ => {
                    self.now = self.now.max(at);
                    self.reads.pop_front().map(|(_, out)| out)
                }
            }
        }
    }

    /// `forward` fed `reads` (bytes, each after the pause before it), and what it sent on: each `on_data`, and the exit.
    fn forwarded(reads: Vec<(u64, &'static [u8])>) -> (Vec<Vec<u8>>, i32) {
        let mut at = Duration::ZERO;
        let mut script = Script { start: Instant::now(), now: Duration::ZERO, reads: Default::default() };
        for (pause, bytes) in reads {
            at += Duration::from_millis(pause);
            script.reads.push_back((at, Out::Bytes(bytes.to_vec())));
        }
        script.reads.push_back((at, Out::Exit(3)));
        let mut sent = Vec::new();
        let mut code = None;
        forward_from(script, |bytes| sent.push(bytes.to_vec()), |c| code = Some(c));
        (sent, code.expect("exited"))
    }

    #[test]
    fn a_redraw_read_a_kilobyte_at_a_time_goes_on_whole() {
        // A synchronized update in three reads, tmux busy between them: one message, when it ends.
        let (sent, code) = forwarded(vec![(0, b"\x1b[?2026hrow 1 "), (6, b"row 2 "), (6, b"row 3\x1b[?2026l"), (30, b"\x1b[?2026hnext\x1b[?2026l")]);
        assert_eq!(sent, vec![b"\x1b[?2026hrow 1 row 2 row 3\x1b[?2026l".to_vec(), b"\x1b[?2026hnext\x1b[?2026l".to_vec()]);
        assert_eq!(code, 3);
        // A burst of a few bytes a read, outside one: gathered too.
        let burst: Vec<(u64, &'static [u8])> = (0..200).map(|_| (0, &b"\x1b[C"[..])).collect();
        let (sent, _) = forwarded(burst);
        assert!(sent.len() <= 3, "{} messages for one burst", sent.len());
        assert_eq!(sent.concat(), b"\x1b[C".repeat(200));
    }

    #[test]
    fn bytes_apart_go_on_apart_and_none_is_held_long() {
        // Outside a synchronized update, reads a few milliseconds apart (a key's echo, the next one's): one message each.
        let (sent, _) = forwarded(vec![(0, b"a"), (20, b"b"), (20, b"c")]);
        assert_eq!(sent, vec![b"a".to_vec(), b"b".to_vec(), b"c".to_vec()]);
        // An update whose end never comes is sent on all the same, as the terminal's own wait for it would end.
        let started = Instant::now();
        let (tx, rx) = mpsc::channel();
        tx.send(Out::Bytes(b"\x1b[?2026hno end".to_vec())).unwrap();
        let (got, gone) = mpsc::channel();
        let forwarder = std::thread::spawn(move || forward(rx, |bytes| got.send((bytes.to_vec(), started.elapsed())).unwrap(), |_| {}));
        let (bytes, after) = gone.recv_timeout(Duration::from_secs(1)).expect("sent on");
        assert_eq!(bytes, b"\x1b[?2026hno end");
        assert!(after < Duration::from_millis(200), "held {after:?}");
        drop(tx);
        forwarder.join().unwrap();
    }

    #[test]
    fn tmux_older_than_3_2_is_refused_with_words_the_app_shows() {
        assert_eq!(parse_version("tmux 3.7c"), Some((3, 7)));
        assert_eq!(parse_version("tmux 3.2a"), Some((3, 2)));
        assert_eq!(parse_version("3.10"), Some((3, 10)));
        assert_eq!(parse_version("tmux next-3.4"), Some((3, 4)));
        assert_eq!(parse_version("tmux 3.0a"), Some((3, 0)));
        assert_eq!(parse_version("tmux 2.9"), Some((2, 9)));
        assert!(parse_version("tmux master").unwrap() > MIN_TMUX);
        assert!(parse_version("tmux openbsd-7.5").unwrap() > MIN_TMUX);
        assert_eq!(parse_version("nonsense"), None);
        for ok in ["tmux 3.2", "tmux 3.2a", "tmux 3.8", "tmux 4.0", "tmux master"] {
            assert!(check_version(ok, "The app's tmux").is_ok(), "{ok}");
        }
        let old = check_version("tmux 3.0a", "The agents' tmux server").unwrap_err();
        assert_eq!(old, "The agents' tmux server is tmux 3.0a, and brindle needs tmux 3.2 or newer to show a session. Update tmux, then reopen the session.");
        assert!(check_version("tmux 3.1c", "x").is_err());
        assert!(check_version("", "The app's tmux").unwrap_err().contains("3.2 or newer"));
    }

    #[test]
    fn the_status_is_ready_with_a_tmux_and_unusable_with_why_without_one() {
        if Command::new("tmux").arg("-V").output().is_ok_and(|o| o.status.success()) {
            assert!(matches!(status(Path::new("tmux")), PtyStatus::Ready { .. }));
        }
        match status(Path::new("/nonexistent/tmux")) {
            PtyStatus::Unusable { message } => assert!(message.contains("couldn't run tmux"), "{message}"),
            other => panic!("a missing tmux: {other:?}"),
        }
    }

    #[test]
    fn the_view_session_holds_only_the_agents_window_with_the_apps_options() {
        let args = prepare_args("v1", "agents:@3", 120, 40).join(" ");
        assert!(args.starts_with("new-session -d -s v1 -n brindle-view-placeholder -x 120 -y 40 cat ;"));
        for part in [
            "link-window -s agents:@3 -t =v1:",
            "kill-window -t =v1:brindle-view-placeholder",
            "set-option -t =v1: status off",
            "set-option -t =v1: prefix None",
            "set-option -t =v1: mouse on",
            "set-option -t =v1: key-table brindle-view",
        ] {
            assert!(args.contains(part), "{part} in {args}");
        }
        assert!(args.ends_with("; source-file -"));
        // Nothing but the view session's own options: no global, server or window option.
        assert!(!args.contains("destroy-unattached"));
        for set in args.split(" ; ").filter(|c| c.starts_with("set-option")) {
            assert!(set.starts_with("set-option -t =v1: "), "{set}");
        }
        assert!(!args.contains("window-size") && !args.contains("bind-key") && !args.contains("set-hook"));
        assert!(attach_args("v1").join(" ").ends_with("attach-session -t =v1: ; set-option -t =v1: destroy-unattached on"));
        assert!(FEATURES.split(',').any(|f| f == "clipboard"));
        // UTF-8 whatever the app's locale (none, from launchd): the client, and every command whose output is read.
        assert_eq!(attach_args("v1")[0], "-u");
        assert_eq!(tmux(Path::new("tmux")).get_args().next(), Some(std::ffi::OsStr::new("-u")));
    }

    #[test]
    fn the_views_key_table_is_the_stock_pane_mouse_of_the_servers_tmux_version() {
        let keys = |version| view_bindings(version).lines().map(|l| l.split_whitespace().nth(3).unwrap().to_string()).collect::<Vec<_>>();
        let baseline = ["MouseDown1Pane", "MouseDown2Pane", "MouseDrag1Pane", "WheelUpPane", "DoubleClick1Pane", "TripleClick1Pane"];
        // A 3.2 server: tmux 3.2a's own six, no key it doesn't have (the scrollbar's), and ⌘C's.
        for old in [(3, 2), (3, 4), (3, 6)] {
            assert_eq!(keys(old), [&baseline[..], &["C-M-S-F12", "C-M-S-F11"]].concat(), "{old:?}");
            assert!(!view_bindings(old).contains("alternate_on") && !view_bindings(old).contains("Scrollbar"));
        }
        // 3.7 on: the wheel for a full-screen program without the mouse, and the scrollbar.
        for new in [(3, 7), (3, 8), (4, 0)] {
            let all = keys(new);
            assert_eq!(&all[..6], baseline);
            assert_eq!(&all[6..], ["WheelUpPane", "MouseDown1ScrollbarUp", "MouseDown1ScrollbarDown", "MouseDrag1ScrollbarSlider", "C-M-S-F12", "C-M-S-F11"]);
        }
        // All in the view's table, none reaching beyond the pane.
        for line in view_bindings((4, 0)).lines() {
            assert!(line.starts_with("bind-key -T brindle-view "), "{line}");
            for beyond in ["kill-", "split-window", "swap-", "display-menu", "new-window", "respawn"] {
                assert!(!line.contains(beyond), "{line}");
            }
        }
        // The server reads the features it knows up to the first it doesn't: 3.2's all come before 3.4's.
        assert!(FEATURES.ends_with(",hyperlinks"));
    }

    #[test]
    fn a_view_sessions_owner_is_the_pid_in_its_name() {
        assert_eq!(view_owner("brindle-view-4242-7"), Some(4242));
        assert_eq!(view_owner("brindle-view-placeholder"), None);
        assert_eq!(view_owner("brindle-view-stock-12"), None);
        assert_eq!(view_owner("agents"), None);
        assert_eq!(view_owner("brindle-view-12-x"), None);
    }

    /// A tmux server of the test's own, `-L pty-<what>-<pid>`, killed when it goes.
    #[cfg(unix)]
    struct Server {
        name: String,
        /// The tmux the server runs: a person's terminal on it runs the same.
        bin: PathBuf,
    }

    #[cfg(unix)]
    impl Server {
        /// None (the test skips) when there's no tmux.
        fn start(what: &str, config: &str, cmd: &str, size: (u16, u16)) -> Option<Server> {
            Self::start_with(Path::new("tmux"), what, config, cmd, size)
        }

        /// The server run by the tmux at `bin` (the view's client is still PATH's).
        fn start_with(bin: &Path, what: &str, config: &str, cmd: &str, size: (u16, u16)) -> Option<Server> {
            if Command::new("tmux").arg("-V").output().is_err() || Command::new(bin).arg("-V").output().is_err() {
                eprintln!("no tmux: skipped");
                return None;
            }
            let server = Server { name: format!("pty-{what}-{}", std::process::id()), bin: bin.to_path_buf() };
            let (x, y) = (size.0.to_string(), size.1.to_string());
            let out = tmux(bin).args(["-L", &server.name, "-f", config, "new-session", "-d", "-s", "agents", "-n", "a1", "-x", &x, "-y", &y, cmd]).output().unwrap();
            assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
            Some(server)
        }

        fn run(&self, args: &[&str]) -> String {
            let out = tmux(Path::new("tmux")).args(["-L", &self.name]).args(args).output().unwrap();
            String::from_utf8_lossy(&out.stdout).trim().to_string()
        }

        fn size(&self) -> String {
            self.run(&["display-message", "-p", "-t", "agents:a1", "#{window_width}x#{window_height}"])
        }

        fn wait(&self, what: &str, done: impl Fn(&Server) -> bool) {
            let deadline = Instant::now() + Duration::from_secs(5);
            while !done(self) {
                assert!(Instant::now() < deadline, "{what}");
                std::thread::sleep(Duration::from_millis(20));
            }
        }

        fn wait_size(&self, want: &str, what: &str) {
            self.wait(&format!("{what}: the window is {}, not {want}", self.size()), |s| s.size() == want);
        }

        fn open(&self, cols: u16, rows: u16) -> PtyView {
            PtyView::open(Path::new("tmux"), Some(&self.name), "agents:a1", cols, rows, |_| {}, |_| {}, |_| {}).unwrap()
        }

        /// A person's terminal: a plain `tmux attach` to the agent's own session, on a pty.
        fn native(&self, cols: u16, rows: u16) -> (Box<dyn Write + Send>, std::process::Child, Option<File>) {
            let mut cmd = Command::new(&self.bin);
            cmd.args(["-L", &self.name, "attach-session", "-t", "=agents"]).env("TERM", "xterm-256color").env_remove("TMUX");
            let crate::setup::Term { output: mut out, input, master, child } = crate::setup::spawn_terminal_sized(cmd, cols, rows).unwrap();
            std::thread::spawn(move || {
                let mut buf = [0u8; 8192];
                while matches!(out.read(&mut buf), Ok(n) if n > 0) {}
            });
            (input, child, master)
        }
    }

    #[cfg(unix)]
    impl Drop for Server {
        fn drop(&mut self) {
            self.run(&["kill-server"]);
            let _ = std::fs::remove_file(socket_of(&self.name));
        }
    }

    /// Against a tmux server of the test's own: the view shows the agent's
    /// pane, without a status line, at the view's size; closing it leaves
    /// the agent's window where it was, and no view session behind.
    #[cfg(unix)]
    #[test]
    fn a_view_attaches_a_real_client_and_leaves_the_agents_window_when_closed() {
        let Some(server) = Server::start("view", "/dev/null", "printf 'hello from the agent\\n'; cat", (100, 30)) else { return };
        let seen = std::sync::Arc::new(Mutex::new(Vec::<u8>::new()));
        let sink = seen.clone();
        let (exited, exit) = mpsc::channel();
        let view = PtyView::open(Path::new("tmux"), Some(&server.name), "agents:a1", 90, 25, move |b| sink.lock().unwrap().extend_from_slice(b), |_| {}, move |code| {
            let _ = exited.send(code);
        })
        .unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        while !String::from_utf8_lossy(&seen.lock().unwrap()).contains("hello from the agent") {
            assert!(Instant::now() < deadline, "the client drew nothing: {:?}", String::from_utf8_lossy(&seen.lock().unwrap()));
            std::thread::sleep(Duration::from_millis(20));
        }
        let session = format!("={}:", view.name());
        assert!(view.name().starts_with(&format!("brindle-view-{}-", std::process::id())));
        assert_eq!(server.run(&["show-options", "-v", "-t", &session, "status"]), "off");
        assert_eq!(server.run(&["show-options", "-v", "-t", &session, "prefix"]), "None");
        assert_eq!(server.run(&["show-options", "-v", "-t", &session, "mouse"]), "on");
        assert_eq!(server.run(&["show-options", "-v", "-t", &session, "key-table"]), "brindle-view");
        assert_eq!(server.run(&["show-options", "-v", "-t", &session, "destroy-unattached"]), "on");
        // The agent's own session (a person's terminal on it) keeps tmux's defaults, and so does the server.
        for option in ["status", "prefix", "mouse", "key-table"] {
            assert_eq!(server.run(&["show-options", "-v", "-t", "=agents:", option]), "", "{option}");
        }
        assert_eq!(server.run(&["show-options", "-gv", "status"]), "on");
        assert_eq!(server.run(&["show-options", "-gv", "key-table"]), "root");
        assert_eq!(server.run(&["show-options", "-wv", "-t", "agents:a1", "window-size"]), "");
        assert_eq!(server.size(), "90x25");
        // The view's table holds tmux's stock pane mouse.
        let keys = server.run(&["list-keys", "-T", "brindle-view"]);
        assert!(keys.contains("WheelUpPane") && keys.contains("MouseDrag1Pane"), "{keys}");
        assert!(!keys.contains("kill-pane") && !keys.contains("split-window"), "{keys}");
        view.write(b"typed\r");
        server.wait("the key didn't reach the pane", |s| s.run(&["capture-pane", "-p", "-t", "agents:a1"]).contains("typed"));
        view.resize(70, 20).unwrap();
        server.wait_size("70x20", "the resize reached the window");
        let name = view.name().to_string();
        drop(view);
        assert!(exit.recv_timeout(Duration::from_secs(5)).is_ok(), "the client didn't exit");
        let sessions = server.run(&["list-sessions", "-F", "#{session_name}"]);
        assert!(!sessions.contains(&name), "{sessions}");
        assert!(!sessions.contains(VIEW_PREFIX), "{sessions}");
        assert_eq!(server.run(&["list-windows", "-t", "=agents", "-F", "#{window_name}"]), "a1");
    }

    /// ⌘C's key (COPY_BINDING): tmux's copy-mode selection goes to the view as
    /// OSC 52 and stays on screen; with none, nothing is copied and nothing
    /// reaches the pane.
    #[cfg(unix)]
    #[test]
    fn the_views_copy_key_copies_tmuxs_selection_as_osc_52() {
        let Some(server) = Server::start("copy", "/dev/null", "printf 'copy me please\\n'; cat", (100, 30)) else { return };
        let seen = std::sync::Arc::new(Mutex::new(Vec::<u8>::new()));
        let sink = seen.clone();
        let view = PtyView::open(Path::new("tmux"), Some(&server.name), "agents:a1", 90, 25, move |b| sink.lock().unwrap().extend_from_slice(b), |_| {}, |_| {}).unwrap();
        let shown = || String::from_utf8_lossy(&seen.lock().unwrap()).to_string();
        let deadline = Instant::now() + Duration::from_secs(5);
        while !shown().contains("copy me please") {
            assert!(Instant::now() < deadline, "the client drew nothing: {:?}", shown());
            std::thread::sleep(Duration::from_millis(20));
        }
        // Nothing selected, out of copy mode: nothing copied, the key not typed into `cat`.
        view.write(b"\x1b[24;8~");
        std::thread::sleep(Duration::from_millis(300));
        assert!(!shown().contains("\x1b]52;"), "{:?}", shown());
        assert_eq!(server.run(&["capture-pane", "-p", "-t", "agents:a1"]), "copy me please");
        // "copy" selected in copy mode, as a drag leaves it where the drag's end doesn't copy.
        for keys in [&["copy-mode", "-t", "agents:a1"][..], &["send-keys", "-t", "agents:a1", "-X", "top-line"], &["send-keys", "-t", "agents:a1", "-X", "begin-selection"], &["send-keys", "-t", "agents:a1", "-X", "-N", "4", "cursor-right"]] {
            server.run(keys);
        }
        view.write(b"\x1b[24;8~");
        let copied = format!("\x1b]52;;{}\x07", base64(b"copy"));
        server.wait("the selection wasn't copied as OSC 52", |_| shown().contains(&copied));
        assert_eq!(server.run(&["display-message", "-p", "-t", "agents:a1", "#{pane_in_mode} #{selection_present}"]), "1 1");
        server.run(&["send-keys", "-t", "agents:a1", "-X", "cancel"]);
    }

    /// A plain drag over a plain pane (tmux's copy mode), its last motion and
    /// its release in one write, as the webview's input can come: the drag's
    /// copy still reaches the view as OSC 52, and so does a double-click's
    /// word, both in one write. Without the release's gap (`RELEASE_GAP`)
    /// tmux fills its buffer and sends the client nothing, every time.
    #[cfg(unix)]
    #[test]
    fn a_drag_released_with_its_last_motion_copies_as_osc_52() {
        let Some(server) = Server::start("drag", "/dev/null", "printf 'drag me please\\n'; cat", (100, 30)) else { return };
        let seen = std::sync::Arc::new(Mutex::new(Vec::<u8>::new()));
        let sink = seen.clone();
        let view = PtyView::open(Path::new("tmux"), Some(&server.name), "agents:a1", 90, 25, move |b| sink.lock().unwrap().extend_from_slice(b), |_| {}, |_| {}).unwrap();
        let shown = || String::from_utf8_lossy(&seen.lock().unwrap()).to_string();
        server.wait("the client drew nothing", |_| shown().contains("drag me please"));
        // The client told to report the mouse before the drag.
        std::thread::sleep(Duration::from_millis(200));
        for round in 0..3 {
            view.write(b"\x1b[<0;1;1M\x1b[<32;3;1M\x1b[<32;8;1M\x1b[<0;8;1m");
            let copied = format!("\x1b]52;;{}\x07", base64(b"drag me"));
            server.wait(&format!("drag {round}: the copy didn't come back as OSC 52 (buffer {:?})", server.run(&["show-buffer"])), |_| shown().matches(&copied).count() == round + 1);
            assert_eq!(server.run(&["display-message", "-p", "-t", "agents:a1", "#{pane_in_mode}"]), "0");
            // Past tmux's double-click time: the next press is a press, not a second click.
            std::thread::sleep(Duration::from_millis(400));
        }
        // A double-click on "please", in one write: tmux's stock binding selects the word and copies it.
        view.write(b"\x1b[<0;10;1M\x1b[<0;10;1m\x1b[<0;10;1M\x1b[<0;10;1m");
        let word = format!("\x1b]52;;{}\x07", base64(b"please"));
        server.wait("the double-click's word didn't come back as OSC 52", |_| shown().contains(&word));
    }

    /// A program's copy, written as Claude Code (2.1.308) writes it inside
    /// tmux: an OSC 52 of its own, the same wrapped for tmux's passthrough,
    /// and `tmux load-buffer -w -`. Only the last reaches the view (as OSC
    /// 52, the clipboard addon's to `copy`): the server's own
    /// `set-clipboard external` takes no program's OSC 52, and
    /// `allow-passthrough` is off. It still does with a person's terminal
    /// on the agent's own session, the view used last.
    #[cfg(unix)]
    #[test]
    fn a_programs_copy_through_tmux_reaches_the_view_and_its_own_osc_52_doesnt() {
        let (raw, wrapped) = (base64(b"own osc 52"), base64(b"passthrough"));
        let cmd = format!(
            "printf 'ready\\n'; read x; printf '\\033]52;c;{raw}\\007'; printf '\\033Ptmux;\\033\\033]52;c;{wrapped}\\007\\033\\\\'; \
             printf 'through tmux' | tmux load-buffer -w -; echo one; read y; printf 'again' | tmux load-buffer -w -; echo two; cat"
        );
        let Some(server) = Server::start("progcopy", "/dev/null", &cmd, (100, 30)) else { return };
        let seen = std::sync::Arc::new(Mutex::new(Vec::<u8>::new()));
        let sink = seen.clone();
        let view = PtyView::open(Path::new("tmux"), Some(&server.name), "agents:a1", 90, 25, move |b| sink.lock().unwrap().extend_from_slice(b), |_| {}, |_| {}).unwrap();
        let shown = || String::from_utf8_lossy(&seen.lock().unwrap()).to_string();
        let pane = || server.run(&["capture-pane", "-p", "-t", "agents:a1"]);
        server.wait("the client drew nothing", |_| shown().contains("ready"));
        assert_eq!(server.run(&["show-options", "-gv", "set-clipboard"]), "external");
        assert_eq!(server.run(&["show-options", "-gv", "allow-passthrough"]), "off");
        view.write(b"go\r");
        server.wait("the program didn't copy", |_| pane().contains("one"));
        let copied = format!("\x1b]52;;{}\x07", base64(b"through tmux"));
        server.wait("load-buffer -w didn't reach the view as OSC 52", |_| shown().contains(&copied));
        assert!(!shown().contains(&raw) && !shown().contains(&wrapped), "{:?}", shown());
        // A person's terminal on the agent's session: the view, used last, still gets it.
        let (_input, mut terminal, _master) = server.native(80, 24);
        server.wait("the person's terminal didn't attach", |s| s.run(&["list-clients", "-t", "=agents", "-F", "#{client_tty}"]).lines().count() == 1);
        view.write(b"go\r");
        server.wait("the program didn't copy again", |_| pane().contains("two"));
        let again = format!("\x1b]52;;{}\x07", base64(b"again"));
        server.wait("load-buffer -w didn't reach the view with a terminal on the agent's session", |_| shown().contains(&again));
        terminal.kill().unwrap();
        let _ = terminal.wait();
    }

    #[test]
    fn a_mouse_release_waits_behind_what_came_just_before_it() {
        assert_eq!(sgr_releases(b"\x1b[<0;1;1M\x1b[<32;8;1M\x1b[<0;8;1mx\x1b[<64;5;5M\x1b[<2;10;12m"), vec![19, 39]);
        // Not a release: a press, an unfinished one, a key that looks like one.
        assert!(sgr_releases(b"\x1b[<0;1;1M\x1b[<0;1;\x1b[m[<0;1;1m").is_empty());
        let mut out = Vec::new();
        let mut last = None;
        let started = Instant::now();
        write_input(&mut out, b"\x1b[<32;8;1M\x1b[<0;8;1m", &mut last).unwrap();
        assert!(started.elapsed() >= RELEASE_GAP, "the release went at once");
        assert_eq!(out, b"\x1b[<32;8;1M\x1b[<0;8;1m");
        // Long after the last write: the release goes at once.
        last = Some(Instant::now() - RELEASE_GAP * 4);
        let started = Instant::now();
        write_input(&mut out, b"\x1b[<0;8;1m", &mut last).unwrap();
        assert!(started.elapsed() < RELEASE_GAP);
        // Keys don't wait.
        let started = Instant::now();
        write_input(&mut out, b"typed", &mut last).unwrap();
        assert!(started.elapsed() < RELEASE_GAP);
    }

    /// Ctrl+C's key on Windows and Linux (COPY_OR_INTERRUPT_BINDING): tmux's
    /// copy-mode selection copied as OSC 52 and copy mode left; with none,
    /// C-c reaches the pane (its program interrupted).
    #[cfg(unix)]
    #[test]
    fn the_views_ctrl_c_key_copies_tmuxs_selection_or_interrupts() {
        let cmd = "trap 'echo interrupted' INT; printf 'copy me please\\n'; while :; do sleep 0.1; done";
        let Some(server) = Server::start("ctrlc", "/dev/null", cmd, (100, 30)) else { return };
        let seen = std::sync::Arc::new(Mutex::new(Vec::<u8>::new()));
        let sink = seen.clone();
        let view = PtyView::open(Path::new("tmux"), Some(&server.name), "agents:a1", 90, 25, move |b| sink.lock().unwrap().extend_from_slice(b), |_| {}, |_| {}).unwrap();
        let shown = || String::from_utf8_lossy(&seen.lock().unwrap()).to_string();
        server.wait("the client drew nothing", |_| shown().contains("copy me please"));
        let pane = || server.run(&["capture-pane", "-p", "-t", "agents:a1"]);
        // "copy" selected in copy mode: copied, copy mode left, the program not interrupted.
        for keys in [&["copy-mode", "-t", "agents:a1"][..], &["send-keys", "-t", "agents:a1", "-X", "top-line"], &["send-keys", "-t", "agents:a1", "-X", "begin-selection"], &["send-keys", "-t", "agents:a1", "-X", "-N", "4", "cursor-right"]] {
            server.run(keys);
        }
        view.write(b"\x1b[23;8~");
        let copied = format!("\x1b]52;;{}\x07", base64(b"copy"));
        server.wait("the selection wasn't copied as OSC 52", |_| shown().contains(&copied));
        server.wait("copy mode wasn't left", |s| s.run(&["display-message", "-p", "-t", "agents:a1", "#{pane_in_mode}"]) == "0");
        std::thread::sleep(Duration::from_millis(300));
        assert!(!pane().contains("interrupted"), "{}", pane());
        // Nothing selected: C-c to the pane.
        view.write(b"\x1b[23;8~");
        server.wait("C-c didn't reach the pane", |_| pane().contains("interrupted"));
        assert_eq!(shown().matches("\x1b]52;").count(), 1);
    }

    #[test]
    fn brindles_sidebar_pane_and_its_border_are_cropped_at_the_left_or_right() {
        let line = |sidebar: &str, left, right, top, bottom, zoomed| format!("{sidebar}\t{left}\t{right}\t{top}\t{bottom}\t121\t25\t{zoomed}\t0\t0");
        // A view 90 wide; its client 121 (not attached yet, or the window's own size).
        let window = |lines: &[String]| crop_of(&lines.join("\n"), 90, None);
        // A left sidebar 30 wide, its border at 30, the agent's pane from 31: 31 columns more on the client, shown from 31.
        let at_left = Crop { cols: 31, left: 31, width: None };
        assert_eq!(window(&[line("s1", 0, 29, 0, 24, 0), line("", 31, 120, 0, 24, 0)]), at_left);
        assert_eq!(crop_of(&[line("s1", 0, 29, 0, 24, 0), line("", 31, 120, 0, 24, 0)].join("\n"), 90, Some((121, 0))), at_left);
        // On the right: its border at 90, shown from 0.
        assert_eq!(window(&[line("", 0, 89, 0, 24, 0), line("s1", 91, 120, 0, 24, 0)]), Crop { cols: 31, left: 0, width: None });
        // None: no sidebar, a pane zoomed, a sidebar along the bottom, or the sidebar alone.
        assert_eq!(window(&[line("", 0, 120, 0, 24, 0)]), Crop::default());
        assert_eq!(window(&[line("s1", 0, 29, 0, 24, 1), line("", 31, 120, 0, 24, 1)]), Crop::default());
        assert_eq!(window(&[line("", 0, 120, 0, 11, 0), line("s1", 0, 120, 13, 24, 0)]), Crop::default());
        assert_eq!(window(&[line("s1", 0, 120, 0, 24, 0)]), Crop::default());
        assert_eq!(crop_of("garbage", 90, None), Crop::default());
        // A narrower terminal has the window (100 wide, on the client of 121): a right sidebar at 70-99 is inside
        // the view; only the agent's 69 columns are shown. At the left: the agent's 69 from the crop on.
        let pane = |sidebar: &str, left, right, width, active, cursor| format!("{sidebar}\t{left}\t{right}\t0\t24\t{width}\t25\t0\t{active}\t{cursor}");
        let narrow = |sidebar: &str, left, right| pane(sidebar, left, right, 100, 0, 0);
        assert_eq!(crop_of(&[narrow("", 0, 68), narrow("s1", 70, 99)].join("\n"), 90, Some((121, 0))), Crop { cols: 31, left: 0, width: Some(69) });
        assert_eq!(crop_of(&[narrow("s1", 0, 29), narrow("", 31, 99)].join("\n"), 90, Some((121, 0))), Crop { cols: 31, left: 31, width: Some(69) });
        // A wider terminal has the window (160 wide): tmux pans it on the client to keep the cursor on it. Panned
        // 10 columns in, 20 of the sidebar's and the border are on the client: those are cropped, no more.
        let wide = |cursor| [pane("s1", 0, 29, 160, 0, 0), pane("", 31, 159, 160, 1, cursor)].join("\n");
        assert_eq!(crop_of(&wide(5), 90, Some((121, 10))), Crop { cols: 31, left: 21, width: None });
        // Panned 40 in, none of it: from the client's first column, as far on as keeps the cursor in the view.
        assert_eq!(crop_of(&wide(5), 90, Some((121, 40))), Crop { cols: 31, left: 0, width: None });
        assert_eq!(crop_of(&wide(119), 90, Some((121, 39))), Crop { cols: 31, left: 22, width: None });
        assert_eq!(crop_of(&wide(128), 90, Some((121, 39))), Crop { cols: 31, left: 31, width: None });
        // The sidebar's own cursor (it is the active pane) is no matter.
        assert_eq!(crop_of(&[pane("s1", 0, 29, 160, 1, 29), pane("", 31, 159, 160, 0, 128)].join("\n"), 90, Some((121, 0))), at_left);
        assert!(LAYOUT_FORMAT.starts_with(&format!("#{{{SIDEBAR_OPTION}}}\t")));
    }

    /// A window with brindle's CLI sidebar pane (a pane with `@brindle_sidebar`,
    /// kept 30 wide by a hook as brindle keeps it): the view's client is the
    /// view's width and the sidebar's columns, so the agent's pane is the
    /// view's width, and the webview is told what to crop, before the
    /// client's first bytes. Told again, and the client resized, as the
    /// layout changes: zoomed, unzoomed, the sidebar closed, one on the right.
    /// Nothing of tmux's is set by the view: the window's hook is the test's.
    #[cfg(unix)]
    #[test]
    fn a_view_of_a_window_with_brindles_sidebar_crops_it_and_follows_its_layout() {
        let Some(server) = Server::start("crop", "/dev/null", "cat", (100, 30)) else { return };
        let agent = server.run(&["display-message", "-p", "-t", "agents:a1", "#{pane_id}"]);
        let sidebar = |flags: &[&str]| {
            let mut args = vec!["split-window", "-d", "-h", "-l", "30", "-P", "-F", "#{pane_id}", "-t", &agent];
            args.splice(3..3, flags.iter().copied());
            let pane = server.run(&args);
            server.run(&["set-option", "-p", "-t", &pane, SIDEBAR_OPTION, "e6af0dc0"]);
            server.run(&["set-hook", "-w", "-t", "agents:a1", "window-resized", &format!("resize-pane -t {pane} -x 30")]);
            pane
        };
        let left = sidebar(&["-b"]);
        let told = Arc::new(Mutex::new(Vec::<(Crop, bool)>::new()));
        let drawn = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let (crops, seen) = (told.clone(), drawn.clone());
        let view = PtyView::open(
            Path::new("tmux"),
            Some(&server.name),
            "agents:a1",
            90,
            25,
            move |_| seen.store(true, Ordering::SeqCst),
            move |crop| crops.lock().unwrap().push((crop, drawn.load(Ordering::SeqCst))),
            |_| {},
        )
        .unwrap();
        let client = || server.run(&["list-clients", "-F", "#{session_name} #{client_width}x#{client_height}"]).lines().find(|l| l.starts_with(view.name())).map(|l| l.split(' ').nth(1).unwrap().to_string());
        let agent_width = || server.run(&["display-message", "-p", "-t", &agent, "#{pane_width}"]);
        let last = || told.lock().unwrap().last().map(|(c, _)| *c);
        let settled = |crop: Crop, what: &str| {
            let want = format!("{}x25", 90 + crop.cols);
            server.wait(&format!("{what}: told {:?}, the client {:?}, the agent's pane {}", last(), client(), agent_width()), |_| last() == Some(crop) && client().as_deref() == Some(want.as_str()) && agent_width() == "90");
        };

        let at_left = Crop { cols: 31, left: 31, width: None };
        let at_right = Crop { cols: 31, left: 0, width: None };
        // Told before any of the client's bytes, and the client attached that wide from the start
        // (the window still the 100 it was started at: 69 of the agent's columns, until the client has it).
        let first = told.lock().unwrap().first().copied().unwrap();
        assert_eq!((first.0.cols, first.0.left, first.1), (31, 31, false));
        settled(at_left, "a sidebar at the left");
        assert_eq!(view.crop(), at_left);
        assert_eq!(server.run(&["display-message", "-p", "-t", &left, "#{pane_width}"]), "30");

        // Zoomed: the sidebar isn't drawn, nothing is cropped, and the client is the view's width.
        server.run(&["resize-pane", "-Z", "-t", &agent]);
        server.wait("zoomed", |_| last() == Some(Crop::default()) && client().as_deref() == Some("90x25"));
        // (The hook's resize-pane, run as the client narrowed, may have unzoomed it already.)
        if server.run(&["display-message", "-p", "-t", "agents:a1", "#{window_zoomed_flag}"]) == "1" {
            server.run(&["resize-pane", "-Z", "-t", &agent]);
        }
        settled(at_left, "unzoomed");

        // The view resized: the client follows, the crop kept.
        view.resize(80, 20).unwrap();
        server.wait("resized", |_| client().as_deref() == Some("111x20"));
        view.resize(90, 25).unwrap();
        settled(at_left, "resized back");

        // A wider terminal (180x30) types last and has the window, 180 wide: tmux pans it on the view's client
        // (121 wide) to keep the agent's cursor on it. Only what of the sidebar is on the client is cropped, none
        // once it is panned past it, and the view goes as far on as keeps the cursor in it.
        let (mut terminal_in, mut terminal, _pty) = server.native(180, 30);
        terminal_in.write_all(b"t").unwrap();
        server.wait_size("180x29", "the wider terminal typed in");
        let panned = || server.run(&["list-clients", "-t", &format!("={}", view.name()), "-F", "#{window_offset_x}"]).parse::<u16>().unwrap_or(0);
        let cursor = || server.run(&["display-message", "-p", "-t", &agent, "#{cursor_x}"]).parse::<u16>().unwrap() + 31;
        // The cursor on the client (tmux pans once it is past it): not panned, the sidebar cropped as it was.
        server.run(&["send-keys", "-t", &agent, "-l", &"x".repeat(78)]);
        server.wait(&format!("not panned: told {:?}, panned {}, the cursor at {}", last(), panned(), cursor()), |_| cursor() == 110 && panned() == 0 && last() == Some(at_left));
        // At the window's 130th column: panned past the sidebar (to the window's end), the agent's pane from the
        // client's first column, the cursor in the view.
        server.run(&["send-keys", "-t", &agent, "-l", &"x".repeat(20)]);
        server.wait(&format!("panned past the sidebar: told {:?}, panned {}, the cursor at {}", last(), panned(), cursor()), |_| cursor() == 130 && panned() > 31 && last() == Some(Crop { cols: 31, left: 0, width: None }));
        assert!(cursor() - panned() < 90, "the cursor in the view");
        assert_eq!(client().as_deref(), Some("121x25"));
        // At the 170th: the cursor past the view's 90 columns of the client: the view goes on to it.
        server.run(&["send-keys", "-t", &agent, "-l", &"y".repeat(40)]);
        server.wait(&format!("the cursor at the right: told {:?}, panned {}, the cursor at {}", last(), panned(), cursor()), |_| {
            let at = cursor().saturating_sub(panned());
            cursor() == 170 && at >= 90 && last() == Some(Crop { cols: 31, left: at + 1 - 90, width: None })
        });
        view.take_window().unwrap();
        settled(at_left, "the view took the wider window back");
        terminal.kill().unwrap();
        let _ = terminal.wait();

        // The sidebar closed: nothing cropped. One on the right: cropped there.
        server.run(&["kill-pane", "-t", &left]);
        server.wait("the sidebar closed", |_| last() == Some(Crop::default()) && client().as_deref() == Some("90x25") && agent_width() == "90");
        sidebar(&[]);
        settled(at_right, "a sidebar at the right");

        // A narrower terminal (100x30, its status line on) types last and has the window: the right
        // sidebar is at 70-99, inside the view, and only the agent's 69 columns are shown. The view's
        // client keeps its size; the view taking the window back shows 90 again.
        let (mut terminal_in, mut terminal, _pty) = server.native(100, 30);
        terminal_in.write_all(b"t").unwrap();
        server.wait_size("100x29", "the terminal typed in");
        server.wait(&format!("the narrower window: told {:?}", last()), |_| last() == Some(Crop { width: Some(69), ..at_right }));
        assert_eq!(client().as_deref(), Some("121x25"));
        view.take_window().unwrap();
        settled(at_right, "the view took the window back");
        terminal.kill().unwrap();
        let _ = terminal.wait();

        // The view set nothing of the window's or the server's: the hook is the test's own, as brindle's is.
        assert_eq!(server.run(&["show-options", "-gv", "status"]), "on");
        assert_eq!(server.run(&["show-options", "-wv", "-t", "agents:a1", "window-size"]), "");
        let told = told.lock().unwrap();
        assert!(told.windows(2).all(|w| w[0].0 != w[1].0), "told only of changes: {told:?}");
    }

    /// A config the agents' server loaded binds keys in the root table
    /// (`bind -n`), as a person's ~/.tmux.conf might: a person's terminal on
    /// the agent's session gets them, the view doesn't, and nothing on the
    /// server is changed by the view.
    #[cfg(unix)]
    #[test]
    fn a_view_doesnt_take_root_table_bindings_from_the_servers_config() {
        let dir = std::env::temp_dir().join(format!("pty-view-conf-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let conf = dir.join("tmux.conf");
        std::fs::write(&conf, "bind-key -n z send-keys -l CAPTURED\nbind-key -n WheelUpPane send-keys -l WHEELED\n").unwrap();
        let started = Server::start("conf", conf.to_str().unwrap(), "cat", (100, 30));
        std::fs::remove_dir_all(&dir).unwrap();
        let Some(server) = started else { return };
        let root = server.run(&["list-keys", "-T", "root"]);
        assert!(root.contains("CAPTURED"), "{root}");
        let view = server.open(90, 25);
        let pane = || server.run(&["capture-pane", "-p", "-t", "agents:a1"]).replace('\n', "");
        view.write(b"z");
        server.wait("z typed in the view reached the pane as z", |_| pane() == "z");
        // A wheel over the plain pane: tmux's stock copy-mode scroll, not the config's binding.
        view.write(b"\x1b[<64;5;5M");
        server.wait("the wheel put the pane in copy mode", |s| s.run(&["display-message", "-p", "-t", "agents:a1", "#{pane_in_mode}"]) == "1");
        assert_eq!(pane().replace(char::is_whitespace, "").trim_end_matches(|c: char| !c.is_alphabetic()), "z");
        server.run(&["send-keys", "-t", "agents:a1", "-X", "cancel"]);
        // A person's terminal on the agent's own session still has the config's binding.
        let (mut terminal, mut child, _pty) = server.native(120, 40);
        std::thread::sleep(Duration::from_millis(300));
        terminal.write_all(b"z").unwrap();
        server.wait("z typed in the terminal went through the config's binding", |_| pane().contains("zCAPTURED"));
        assert_eq!(server.run(&["list-keys", "-T", "root"]), root, "the server's root table is as it was");
        child.kill().unwrap();
        let _ = child.wait();
    }

    /// A view still opening when its page goes (the webview loads another:
    /// `close_all`), or that its page closed meanwhile, isn't registered: it
    /// is handed back and dropped, and its session and client go. One that
    /// opens in time is registered, and the next page load closes it.
    #[cfg(unix)]
    #[test]
    fn a_view_opened_for_a_page_that_has_gone_is_closed_not_registered() {
        let Some(server) = Server::start("page", "/dev/null", "cat", (100, 30)) else { return };
        let views = PtyViews::default();
        let shown = || server.run(&["list-sessions", "-F", "#{session_name}"]).lines().filter(|s| s.starts_with(VIEW_PREFIX)).count();

        // Asked for, then the page reloads before it opens.
        let claim = views.claim();
        views.close_all();
        let stale = views.register(claim, server.open(90, 25)).unwrap_err();
        assert_eq!(shown(), 1);
        drop(stale);
        assert_eq!(shown(), 0, "the stale view's session went with it");
        assert!(views.with(claim.id, |_| ()).is_none());

        // Asked for, then closed by its page (switched away from) before it opens.
        let claim = views.claim();
        assert!(views.close(claim.id).is_none());
        drop(views.register(claim, server.open(90, 25)).unwrap_err());
        assert_eq!(shown(), 0);

        // One that couldn't open is let go; its id is never registered later.
        let claim = views.claim();
        views.abandon(claim);
        drop(views.register(claim, server.open(90, 25)).unwrap_err());
        assert_eq!(shown(), 0);

        // Opened in time: registered, and shown; the next page load closes it.
        let claim = views.claim();
        views.register(claim, server.open(90, 25)).map_err(|_| "registered").unwrap();
        assert!(views.with(claim.id, |_| ()).is_some());
        assert_eq!(shown(), 1);
        views.close_all();
        assert_eq!(shown(), 0);
        assert_eq!(server.run(&["list-windows", "-t", "=agents", "-F", "#{window_name}"]), "a1");
    }

    /// A view session an app that died left behind (its pid is gone) is
    /// cleared when the next view opens on that server.
    #[cfg(unix)]
    #[test]
    fn a_view_left_by_an_app_that_died_is_cleared() {
        let Some(server) = Server::start("stale", "/dev/null", "cat", (100, 30)) else { return };
        // A pid that isn't running: a child that has exited and been reaped.
        let mut gone = Command::new("true").spawn().unwrap();
        let dead = gone.id();
        gone.wait().unwrap();
        let stale = format!("brindle-view-{dead}-1");
        let other = "brindle-view-1-1"; // launchd's: alive, another app's as far as this one knows.
        server.run(&["new-session", "-d", "-s", &stale, "cat"]);
        server.run(&["new-session", "-d", "-s", other, "cat"]);
        let view = server.open(90, 25);
        let sessions = server.run(&["list-sessions", "-F", "#{session_name}"]);
        assert!(!sessions.contains(&stale), "{sessions}");
        assert!(sessions.contains(other) && sessions.contains(view.name()) && sessions.contains("agents"), "{sessions}");
    }

    /// A default tmux server of the test's own: a `tmux` that runs PATH's
    /// with `TMUX_TMPDIR` a folder of the test's, so no `-L`/`-S` reaches a
    /// server there and never the person's own default one. Killed, and its
    /// folder removed, when it goes.
    #[cfg(unix)]
    struct DefaultServer {
        dir: PathBuf,
        bin: PathBuf,
    }

    #[cfg(unix)]
    impl DefaultServer {
        fn start(cmd: &str) -> Option<DefaultServer> {
            use std::os::unix::fs::PermissionsExt;
            if Command::new("tmux").arg("-V").output().is_err() {
                eprintln!("no tmux: skipped");
                return None;
            }
            let dir = std::env::temp_dir().join(format!("pty-view-default-{}", std::process::id()));
            std::fs::create_dir_all(&dir).unwrap();
            let bin = dir.join("tmux");
            std::fs::write(&bin, format!("#!/bin/sh\nunset TMUX\nTMUX_TMPDIR='{}' exec tmux \"$@\"\n", dir.display())).unwrap();
            std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o700)).unwrap();
            let server = DefaultServer { dir, bin };
            let out = tmux(&server.bin).args(["-f", "/dev/null", "new-session", "-d", "-s", "agents", "-n", "a1", "-x", "100", "-y", "30", cmd]).output().unwrap();
            assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
            Some(server)
        }

        /// A command with no `-L`/`-S`: the default server, as the wrapper's `TMUX_TMPDIR` has it.
        fn run(&self, args: &[&str]) -> String {
            let out = tmux(&self.bin).args(args).output().unwrap();
            String::from_utf8_lossy(&out.stdout).trim().to_string()
        }
    }

    #[cfg(unix)]
    impl Drop for DefaultServer {
        fn drop(&mut self) {
            self.run(&["kill-server"]);
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    /// An agent with no `tmux_server` is on the default server: the view
    /// reaches it with no `-L`/`-S`, shows the pane, keeps to its own session
    /// and key table, and clears a view session an app that died left there.
    #[cfg(unix)]
    #[test]
    fn a_view_attaches_to_a_pane_on_the_default_server() {
        let Some(server) = DefaultServer::start("printf 'hello from the default server\\n'; cat") else { return };
        let socket = server.dir.join(format!("tmux-{}", unsafe { libc::getuid() })).join("default");
        assert!(socket.exists(), "the test's own default server is at {}", socket.display());
        let mut gone = Command::new("true").spawn().unwrap();
        let dead = gone.id();
        gone.wait().unwrap();
        let stale = format!("brindle-view-{dead}-1");
        server.run(&["new-session", "-d", "-s", &stale, "cat"]);

        let seen = std::sync::Arc::new(Mutex::new(Vec::<u8>::new()));
        let sink = seen.clone();
        let view = PtyView::open(&server.bin, None, "agents:a1", 90, 25, move |b| sink.lock().unwrap().extend_from_slice(b), |_| {}, |_| {}).unwrap();
        assert!(view.server.is_empty(), "{:?}", view.server);
        let deadline = Instant::now() + Duration::from_secs(5);
        while !String::from_utf8_lossy(&seen.lock().unwrap()).contains("hello from the default server") {
            assert!(Instant::now() < deadline, "the client drew nothing: {:?}", String::from_utf8_lossy(&seen.lock().unwrap()));
            std::thread::sleep(Duration::from_millis(20));
        }
        let sessions = server.run(&["list-sessions", "-F", "#{session_name}"]);
        assert!(!sessions.contains(&stale), "the stale view went: {sessions}");
        assert!(sessions.contains(view.name()) && sessions.contains("agents"), "{sessions}");
        let session = format!("={}:", view.name());
        assert_eq!(server.run(&["show-options", "-v", "-t", &session, "key-table"]), "brindle-view");
        let deadline = Instant::now() + Duration::from_secs(5);
        while server.run(&["show-options", "-v", "-t", &session, "destroy-unattached"]) != "on" {
            assert!(Instant::now() < deadline, "destroy-unattached never set");
            std::thread::sleep(Duration::from_millis(20));
        }
        // Nothing global, nothing of the agent's session.
        assert_eq!(server.run(&["show-options", "-gv", "key-table"]), "root");
        assert_eq!(server.run(&["show-options", "-gv", "status"]), "on");
        assert_eq!(server.run(&["show-options", "-v", "-t", "=agents:", "key-table"]), "");
        view.write(b"typed\r");
        let deadline = Instant::now() + Duration::from_secs(5);
        while !server.run(&["capture-pane", "-p", "-t", "agents:a1"]).contains("typed") {
            assert!(Instant::now() < deadline, "the key didn't reach the pane");
            std::thread::sleep(Duration::from_millis(20));
        }
        let name = view.name().to_string();
        drop(view);
        let sessions = server.run(&["list-sessions", "-F", "#{session_name}"]);
        assert!(!sessions.contains(&name), "{sessions}");
        assert_eq!(server.run(&["list-windows", "-t", "=agents", "-F", "#{window_name}"]), "a1");
    }

    /// The keys every tmux from 3.2 has in the view's table, and ones it only has later.
    const BASELINE_KEYS: [&str; 6] = ["MouseDown1Pane", "MouseDown2Pane", "MouseDrag1Pane", "WheelUpPane", "DoubleClick1Pane", "TripleClick1Pane"];

    /// The view on `server`, whose tmux is older than the client's: it opens,
    /// its table has the baseline and nothing that tmux lacks, and tmux's own
    /// wheel scrolls the plain pane's history.
    #[cfg(unix)]
    fn assert_view_works_on_an_older_server(server: &Server, view: &PtyView) {
        let keys = server.run(&["list-keys", "-T", "brindle-view"]);
        for key in BASELINE_KEYS {
            assert!(keys.contains(key), "{key} in {keys}");
        }
        assert!(!keys.contains("Scrollbar") && !keys.contains("alternate_on"), "{keys}");
        // The client attached (and its terminal told to report the mouse) before the wheel.
        server.wait("the view's client attached", |s| s.run(&["list-clients", "-F", "#{session_name}"]).contains(view.name()));
        std::thread::sleep(Duration::from_millis(200));
        view.write(b"\x1b[<64;5;5M");
        server.wait("the wheel put the plain pane in copy mode", |s| s.run(&["display-message", "-p", "-t", "agents:a1", "#{pane_in_mode}"]) == "1");
        server.run(&["send-keys", "-t", "agents:a1", "-X", "cancel"]);
        view.write(b"typed");
        server.wait("a key reached the pane", |s| s.run(&["capture-pane", "-p", "-t", "agents:a1"]).contains("typed"));
    }

    /// A newer client (the app's bundled tmux) on an older supported server
    /// gives it only the bindings its tmux has (scrollbar keys, which 3.2
    /// lacks, would make the whole view fail). The server's version is faked
    /// as 3.2 here; the next test runs a real 3.2 server when one is given.
    #[cfg(unix)]
    #[test]
    fn a_newer_client_gives_an_older_server_only_the_bindings_its_tmux_has() {
        let Some(server) = Server::start("older", "/dev/null", "seq 1 100; cat", (100, 30)) else { return };
        let view = PtyView::open_on(Path::new("tmux"), server_args(Some(&server.name)).unwrap(), (3, 2), "agents:a1", 90, 25, |_| {}, |_| {}, |_| {}).unwrap();
        assert_view_works_on_an_older_server(&server, &view);
    }

    /// The same against a real tmux 3.2 server (`BRINDLE_TEST_OLD_TMUX`: the
    /// path of a tmux 3.2a binary), the view's client being PATH's newer one
    /// and finding the server's version itself. Skipped without one.
    #[cfg(unix)]
    #[test]
    fn a_view_attaches_to_a_real_tmux_3_2_server() {
        let Some(old) = std::env::var_os("BRINDLE_TEST_OLD_TMUX") else {
            eprintln!("BRINDLE_TEST_OLD_TMUX not set: skipped");
            return;
        };
        let Some(server) = Server::start_with(Path::new(&old), "real-old", "/dev/null", "seq 1 100; cat", (100, 30)) else { return };
        assert!(server.run(&["display-message", "-p", "#{version}"]).starts_with("3.2"));
        // The app's newer tmux opens it: the view's client is the server's own.
        let view = server.open(90, 25);
        assert_view_works_on_an_older_server(&server, &view);
        let client = server.run(&["list-clients", "-F", "#{session_name} #{client_pid}"]);
        let pid: u32 = client.lines().find(|l| l.starts_with(view.name())).unwrap().split(' ').nth(1).unwrap().parse().unwrap();
        assert_eq!(exe_of(pid).unwrap().canonicalize().unwrap(), Path::new(&old).canonicalize().unwrap());
    }

    /// The view's client runs the app's tmux when the server runs the same
    /// version, else the executable of the process listening on the
    /// server's socket (the kernel's word, not the server's), if it is this
    /// user's and no one else can write it; else it says so.
    #[cfg(unix)]
    #[test]
    fn the_views_client_is_a_tmux_of_the_servers_version() {
        let Some(server) = Server::start("client", "/dev/null", "cat", (100, 30)) else { return };
        let socket = socket_of(&server.name);
        assert_eq!(socket_path(&server_args(Some(&server.name)).unwrap()), socket);
        let (uid, pid) = peer_of(&socket).unwrap();
        assert_eq!(pid.to_string(), server.run(&["display-message", "-p", "#{pid}"]));
        assert_eq!(uid, unsafe { libc::getuid() });
        let theirs = format!("tmux {}", server.run(&["display-message", "-p", "#{version}"]));
        let ours = Path::new("/the/apps/tmux");
        assert_eq!(client_for(ours, &theirs, &socket, &theirs).unwrap(), ours);
        // The app's tmux said another version (faked): the server's own executable, which says the server's.
        let exe = client_for(ours, "tmux 9.9", &socket, &theirs).unwrap();
        assert_eq!(client_version(&exe).unwrap(), theirs);
        // No server on the socket: refused, saying both versions.
        let refused = client_for(ours, "tmux 9.9", Path::new("/nonexistent/socket"), &theirs).unwrap_err();
        assert!(refused.contains("tmux 9.9") && refused.contains(&theirs), "{refused}");
    }

    /// An executable anyone else could have written is never run: it, or its folder, writable by group or others.
    #[cfg(unix)]
    #[test]
    fn only_an_executable_no_one_else_can_write_is_trusted() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("pty-view-exe-{}", std::process::id()));
        let open_dir = dir.join("open");
        std::fs::create_dir_all(&open_dir).unwrap();
        let exe = dir.join("tmux");
        std::fs::write(&exe, "").unwrap();
        let set = |p: &Path, mode| std::fs::set_permissions(p, std::fs::Permissions::from_mode(mode)).unwrap();
        set(&dir, 0o755);
        set(&exe, 0o755);
        assert!(trusted_exe(&exe));
        set(&exe, 0o775);
        assert!(!trusted_exe(&exe), "group-writable");
        set(&exe, 0o757);
        assert!(!trusted_exe(&exe), "world-writable");
        let inside = open_dir.join("tmux");
        std::fs::write(&inside, "").unwrap();
        set(&inside, 0o755);
        set(&open_dir, 0o777);
        assert!(!trusted_exe(&inside), "in a folder anyone can write");
        assert!(!trusted_exe(Path::new("/nonexistent/tmux")));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// The view and a person's terminal of another size on the same pane at
    /// once, on a server with tmux's defaults: tmux's `window-size latest`
    /// gives the window to the client typed into last; each keeps its own
    /// status line setting; the window follows whichever stays when the
    /// other detaches.
    #[cfg(unix)]
    #[test]
    fn a_view_shares_the_pane_with_a_terminal_of_another_size() {
        let Some(server) = Server::start("share", "/dev/null", "cat", (100, 30)) else { return };
        let view = server.open(90, 25);
        server.wait_size("90x25", "the view alone");

        // A person's terminal, 120x40, attached to the agent's own session (status line on: 39 rows for the window).
        let (mut terminal_in, mut terminal, _pty) = server.native(120, 40);
        server.wait_size("120x39", "the terminal attached last");
        let clients = server.run(&["list-clients", "-F", "#{session_name} #{client_width}x#{client_height}"]);
        assert!(clients.contains(&format!("{} 90x25", view.name())) && clients.contains("agents 120x40"), "{clients}");
        assert_eq!(server.run(&["show-options", "-v", "-t", &format!("={}:", view.name()), "status"]), "off");
        assert_eq!(server.run(&["show-options", "-gv", "status"]), "on");

        // Typing in a client gives it the window.
        view.write(b"v");
        server.wait_size("90x25", "typed in the view");
        terminal_in.write_all(b"t").unwrap();
        server.wait_size("120x39", "typed in the terminal");

        // A client's resize counts as using it, as resizing a terminal's window does: it takes the window.
        view.resize(70, 20).unwrap();
        server.wait_size("70x20", "the view resized");
        terminal_in.write_all(b"t").unwrap();
        server.wait_size("120x39", "typed in the terminal again");
        view.write(b"v");
        server.wait_size("70x20", "typed in the view at its new size");
        terminal_in.write_all(b"t").unwrap();
        server.wait_size("120x39", "and in the terminal");

        // The terminal detaches: the window is the view's. The view closes: the window keeps its last size.
        terminal.kill().unwrap();
        let _ = terminal.wait();
        server.wait_size("70x20", "the terminal gone");
        drop(view);
        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(server.run(&["list-windows", "-t", "=agents", "-F", "#{window_name}"]), "a1");
        assert_eq!(server.run(&["capture-pane", "-p", "-t", "agents:a1"]).replace('\n', ""), "vttvt");
    }

    /// The view and a person's terminal of another size on one pane, the
    /// terminal used last (attached, or typed in): the view taking focus, or
    /// being clicked, gives it the window at once, with nothing sent to the
    /// pane; typing in the terminal takes the window back. On a server the
    /// tmux at `bin` runs, the view's client being the server's own.
    #[cfg(unix)]
    fn assert_focus_gives_the_view_the_window(bin: &Path, what: &str) {
        let dir = std::env::temp_dir().join(format!("pty-view-{what}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let input = dir.join("input");
        // Every byte the pane is sent, as it comes: no line discipline, no echo.
        let cmd = format!("stty raw -echo; exec cat > '{}'", input.display());
        let Some(server) = Server::start_with(bin, what, "/dev/null", &cmd, (100, 30)) else {
            std::fs::remove_dir_all(&dir).unwrap();
            return;
        };
        let received = || std::fs::read(&input).unwrap_or_default();
        let view = server.open(90, 25);
        server.wait_size("90x25", "the view alone");
        server.wait("the view's client attached", |s| s.run(&["list-clients", "-F", "#{session_name}"]).contains(view.name()));
        let (mut terminal_in, mut terminal, _pty) = server.native(120, 40);
        server.wait_size("120x39", "the terminal attached last");

        // Focused: the view's.
        view.take_window().unwrap();
        server.wait_size("90x25", "the view focused");
        // Focused again, already the view's: as it is.
        view.take_window().unwrap();
        assert_eq!(server.size(), "90x25");

        // Typed in the terminal: the terminal's, as tmux has it.
        terminal_in.write_all(b"t").unwrap();
        server.wait_size("120x39", "typed in the terminal");
        server.wait("the terminal's key reached the pane", |_| received() == b"t");

        // Clicked (the webview takes focus first): the view's again; then the terminal's again.
        view.take_window().unwrap();
        server.wait_size("90x25", "the view clicked");
        terminal_in.write_all(b"u").unwrap();
        server.wait_size("120x39", "typed in the terminal again");

        // The view never sent the pane a byte: only what was typed in the terminal reached it.
        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(String::from_utf8_lossy(&received()), "tu");
        // Only the view's client was touched: still on its own session and table, and nothing
        // global, of the agent's session or of its window was set.
        let clients = server.run(&["list-clients", "-F", "#{session_name} #{client_key_table}"]);
        assert!(clients.contains(&format!("{} brindle-view", view.name())) && clients.contains("agents root"), "{clients}");
        assert_eq!(server.run(&["show-options", "-wv", "-t", "agents:a1", "window-size"]), "");
        assert_eq!(server.run(&["show-options", "-gwv", "window-size"]), "latest");
        assert_eq!(server.run(&["show-options", "-gv", "focus-events"]), "off");
        assert_eq!(server.run(&["show-options", "-v", "-t", "=agents:", "key-table"]), "");
        // Taken as a view opens (the webview does), before its client has attached: it waits for it.
        let opened = server.open(80, 20);
        opened.take_window().unwrap();
        server.wait_size("80x20", "a view taking the window as it opens");
        drop(opened);
        terminal.kill().unwrap();
        let _ = terminal.wait();
        drop(view);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn focusing_the_view_gives_it_the_window_and_typing_in_a_terminal_takes_it_back() {
        assert_focus_gives_the_view_the_window(Path::new("tmux"), "focus");
    }

    /// The same on the app's bundled tmux (`BRINDLE_TEST_BUNDLED_TMUX`, else
    /// the engine build_engine.sh staged) and a real 3.2
    /// (`BRINDLE_TEST_OLD_TMUX`), each skipped without one.
    #[cfg(unix)]
    #[test]
    fn focusing_the_view_gives_it_the_window_on_the_bundled_tmux_and_on_3_2() {
        let staged = Path::new(env!("CARGO_MANIFEST_DIR")).join("engine/bin/tmux");
        let bundled = std::env::var_os("BRINDLE_TEST_BUNDLED_TMUX").map(PathBuf::from).or_else(|| staged.exists().then_some(staged));
        for (what, bin) in [("focus-bundled", bundled), ("focus-old", std::env::var_os("BRINDLE_TEST_OLD_TMUX").map(PathBuf::from))] {
            match bin {
                Some(bin) => assert_focus_gives_the_view_the_window(&bin, what),
                None => eprintln!("{what}: no tmux given: skipped"),
            }
        }
    }
}
