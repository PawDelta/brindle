//! The engine connection. The app finds the local brindle daemon through
//! `~/.brindle/daemon.json` (written 0600 by `brindle daemon`: socket, port,
//! token, version, pid; see brindle/daemon/lifecycle.py), starts one when none
//! runs (an app that carries its engine uses only that engine's daemon, see
//! `reuse`), and carries the webview's requests and event stream to it over its
//! Unix socket (or 127.0.0.1 when it listens on TCP only). The token stays
//! here: the webview never reads daemon.json, it asks through the `daemon_*`
//! commands and gets back status codes and JSON.
//!
//! On the Unix socket the daemon refuses a caller that is, or descends from,
//! a brindle agent's process (brindle/daemon/peer.py): an app started from an
//! agent's terminal gets a 403 `agent_caller` for every request.
//!
//! On Windows the daemon runs inside WSL and is found, checked and started
//! through `wsl` instead; everything after finding it is the same, over TCP.

use std::collections::HashMap;
use std::ffi::{OsStr, OsString};
#[cfg(not(windows))]
use std::fs::File;
use std::io::{self, Read, Write};
use std::net::{Shutdown, TcpStream};
#[cfg(unix)]
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
#[cfg(not(windows))]
use std::time::Instant;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::ipc::Channel;
use tauri::State;
use tungstenite::{HandshakeError, Message};

/// How long one start waits for the daemon to listen before answering that it
/// is still starting (`ENGINE_STARTING`); the webview asks again, and the next
/// start waits on the same process. However long that takes, it isn't a
/// failure: an engine's first start is slow (Gatekeeper's first look at a new
/// bundle, the first imports, a Keychain prompt waiting on the person), and
/// only a daemon that exits has failed.
pub(crate) const START_TIMEOUT: Duration = Duration::from_secs(20);
/// How a start's failure begins while the daemon it started still runs but
/// doesn't listen yet: not a failure. The webview (api/engine.ts) keeps
/// "Starting brindle…" up and asks again.
pub(crate) const ENGINE_STARTING: &str = "engine starting";
/// The close code for a WebSocket whose first frame wasn't the token (auth.WS_UNAUTHORIZED).
const WS_UNAUTHORIZED: u16 = 4401;
/// A request's answer; long actions answer at once with a job id.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(60);
/// What a request says when the daemon doesn't answer within `REQUEST_TIMEOUT`.
const BUSY_MESSAGE: &str = "The brindle daemon is busy and didn't answer within 60 seconds.";
/// How many requests may have a connection to the daemon open at once; more
/// wait for one to finish. Every connection is a file descriptor, and an app
/// opened from the Finder gets only 256 of them: a burst from the webview
/// (a reload per event the stream replays at launch) once took them all.
const MAX_CONNECTIONS: usize = 16;
/// How long a stream's read waits before letting a frame from the webview
/// (a terminal's key, paste or resize) have the socket.
const STREAM_POLL: Duration = Duration::from_millis(40);
/// Where a started daemon's stderr goes, in brindle's home, for when it fails.
pub(crate) const START_LOG: &str = "app-daemon.log";
/// The launchd job that runs the daemon on macOS (`start_daemon`), in brindle's home.
#[cfg(target_os = "macos")]
const LAUNCHD_PLIST: &str = "app-daemon.plist";
/// How long a liveness check's connect and answer may take.
const CHECK_TIMEOUT: Duration = Duration::from_secs(3);
/// How a start's failure begins when the installed brindle predates `brindle
/// daemon` (0.4.x): the webview reads it (api/engine.ts) and shows the engine
/// banner's upgrade instead of the bare reason.
pub(crate) const ENGINE_TOO_OLD: &str = "engine too old";
/// How a start's failure begins when an app that carries its engine finds
/// another install's `brindle daemon`, one a person started, using brindle's home.
pub(crate) const ENGINE_CONFLICT: &str = "engine conflict";
/// How long a daemon being replaced has to shut down after SIGTERM before it
/// is killed, unless it says it is pausing (PAUSING_FILE). What it still
/// ran gets lifecycle.SHUTDOWN_GRACE of that.
#[cfg(not(windows))]
const STOP_GRACE: Duration = Duration::from_secs(2);
/// How long in all a daemon being replaced has while it says it is pausing:
/// the app's daemon pauses every session it runs on its way out
/// (lifecycle.pause_for_exit: their windows closed, their work kept, for the
/// next daemon to resume; it gives up after lifecycle.PAUSE_BUDGET). This
/// covers recovery winding down, the pause, and cleanup during replacement;
/// it is not a wait for the app's exit to stop a healthy launchd daemon.
#[cfg(not(windows))]
const PAUSE_GRACE: Duration = Duration::from_secs(20);
/// The file in brindle's home that names the daemon (its pid) while it
/// pauses on its way out (lifecycle.PAUSING_FILE).
#[cfg(not(windows))]
const PAUSING_FILE: &str = "daemon-pausing";
/// How long a killed daemon has to be gone.
#[cfg(not(windows))]
const KILL_GRACE: Duration = Duration::from_secs(2);

/// `~/.brindle/daemon.json`, as `brindle daemon` writes it.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct DaemonInfo {
    pub socket: Option<PathBuf>,
    pub port: Option<u16>,
    pub token: String,
    #[serde(default)]
    pub version: String,
    pub pid: u32,
}

pub fn parse_info(text: &str) -> Result<DaemonInfo, String> {
    let info: DaemonInfo =
        serde_json::from_str(text).map_err(|e| format!("daemon.json is unreadable: {e}"))?;
    if info.socket.is_none() && info.port.is_none() {
        return Err("daemon.json names neither a socket nor a port".into());
    }
    if info.token.is_empty() {
        return Err("daemon.json has no token".into());
    }
    Ok(info)
}

pub fn home_dir() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default()
}

/// brindle's home, as the engine's config.brindle_home finds it.
pub fn brindle_home() -> PathBuf {
    std::env::var_os("BRINDLE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home_dir().join(".brindle"))
}

/// Whether `pid` is alive and still a brindle daemon, not a process that got
/// the pid of one that died without cleaning up (lifecycle._is_daemon).
#[cfg(not(windows))]
fn is_daemon(pid: u32) -> bool {
    ps(pid, "command=").is_some_and(|cmd| cmd.contains("brindle") && cmd.contains("daemon"))
}

/// What `ps -o <field> -p <pid>` says, or None when the process is gone.
#[cfg(not(windows))]
fn ps(pid: u32, field: &str) -> Option<String> {
    let out = Command::new("ps").args(["-o", field, "-p", &pid.to_string()]).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// What an app that carries its engine does with the running daemon.
#[derive(Debug, PartialEq)]
pub(crate) enum Reuse {
    /// Its own engine's, of this build: use it.
    Use,
    /// Stop it, then start the engine's own: one of this engine's from
    /// another build (from before an update, or an app put in this one's
    /// place), or another install's that an app started (`--idle-exit`: an
    /// older app's uv brindle, which would exit once idle anyway).
    Replace,
    /// One that can't be told to be this app's, nor safely stopped: another
    /// install's that a person started (`brindle daemon` in a terminal), or
    /// any when this engine's build is unknown. Left running, and the app
    /// says what to do.
    Conflict(String),
}

/// What to do with the daemon whose command line is `command`, in an app
/// whose engine is `engine` at build `build` (an Err: why its build is
/// unknown, `engine::require_build`'s). Its own
/// is told by the build id the launcher puts on every brindle's command line,
/// which lives exactly as long as the process: not by its path alone (an
/// update leaves the path as it was) nor by times. All of these share
/// brindle's home, so its sign-in and refresh lock stay shared.
#[cfg(not(windows))]
pub(crate) fn reuse(engine: &Path, build: Result<&str, &str>, command: &str) -> Reuse {
    let build = match build {
        Ok(build) => build,
        Err(why) => return Reuse::Conflict(why.to_string()),
    };
    let words: Vec<&str> = command.split_whitespace().collect();
    if command.starts_with(&crate::engine::python_command(engine)) {
        let option = crate::engine::build_option(build);
        let this_build = words.windows(2).any(|w| w[0] == "-X" && w[1] == option);
        return if this_build { Reuse::Use } else { Reuse::Replace };
    }
    if words.contains(&"--idle-exit") {
        return Reuse::Replace;
    }
    let program = words.first().copied().unwrap_or("(unknown)");
    Reuse::Conflict(format!(
        "{ENGINE_CONFLICT}: a brindle daemon from another install ({program}) is running, not this \
         app's own engine. Stop it (`brindle daemon stop`, with that brindle) and reopen the app"
    ))
}

/// `reuse` for the daemon `info` names, from its command line as `ps` shows it now.
#[cfg(not(windows))]
fn reuse_running(engine: &Path, info: &DaemonInfo) -> Reuse {
    let command = ps(info.pid, "command=").unwrap_or_default();
    let build = crate::engine::require_build(engine);
    reuse(engine, build.as_deref().map_err(String::as_str), &command)
}

/// Stop the daemon `pid` of brindle's home `home`, sending SIGTERM unless it
/// is already pausing, and wait until it's gone. Still there after `grace`,
/// SIGKILL it (the next daemon takes over the files it left). While
/// it says it is pausing its sessions (`pausing`), the wait goes on, up to
/// PAUSE_GRACE in all, and ends `grace` after it stops saying so.
#[cfg(not(windows))]
fn stop_daemon(home: &Path, pid: u32, grace: Duration) -> Result<(), String> {
    stop_daemon_while(pid, grace, || is_daemon(pid), || pausing(home, pid))
}

/// `stop_daemon`, told by `running` whether `pid` is still the daemon and by
/// `says_pausing` whether it still says it is pausing.
#[cfg(not(windows))]
fn stop_daemon_while(
    pid: u32,
    grace: Duration,
    mut running: impl FnMut() -> bool,
    mut says_pausing: impl FnMut() -> bool,
) -> Result<(), String> {
    let pid_t = libc::pid_t::try_from(pid).map_err(|_| format!("bad daemon pid {pid}"))?;
    let already_pausing = says_pausing();
    if !already_pausing {
        // SAFETY: kill(2) only sends a signal; is_daemon has just said pid is a daemon's.
        unsafe { libc::kill(pid_t, libc::SIGTERM) };
    }
    // A second SIGTERM during the daemon's pause cleanup can interrupt it.
    // One already pausing gets the same budget without another signal.
    let stopped = Instant::now();
    let mut deadline = stopped + grace;
    let (mut killed, mut said_pausing) = (false, already_pausing);
    while running() {
        if !killed {
            let says = says_pausing();
            if says {
                deadline = stopped + PAUSE_GRACE;
            } else if said_pausing {
                // Its pause is over: what it still does on its way out gets the short wait.
                deadline = (Instant::now() + grace).min(stopped + PAUSE_GRACE);
            }
            said_pausing = says;
        }
        if Instant::now() >= deadline {
            if killed {
                return Err(format!("the previous brindle daemon (pid {pid}) didn't stop, even killed"));
            }
            // SAFETY: as above; is_daemon has just said it's still the daemon.
            unsafe { libc::kill(pid_t, libc::SIGKILL) };
            killed = true;
            deadline = Instant::now() + KILL_GRACE;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    Ok(())
}

/// Whether the daemon `pid` says it is pausing the sessions it runs on its
/// way out: PAUSING_FILE in brindle's home `home` names it.
#[cfg(not(windows))]
fn pausing(home: &Path, pid: u32) -> bool {
    std::fs::read_to_string(home.join(PAUSING_FILE)).is_ok_and(|text| text.trim() == pid.to_string())
}

/// Whether a daemon answers an authenticated `GET /v1/meta` on 127.0.0.1:`port`
/// with `token`: how a daemon whose pid can't be checked here (one inside
/// WSL, seen from Windows) counts as running. A stale token's 401 doesn't.
pub(crate) fn answers_meta(port: u16, token: &str) -> bool {
    let check = || -> io::Result<u16> {
        let stream = TcpStream::connect_timeout(&(std::net::Ipv4Addr::LOCALHOST, port).into(), CHECK_TIMEOUT)?;
        let mut conn = Conn::Tcp(stream);
        conn.set_timeout(Some(CHECK_TIMEOUT))?;
        Ok(send_request(&mut conn, token, "GET", "/v1/meta", None)?.status)
    };
    check().is_ok_and(|status| status == 200)
}

/// daemon.json in brindle's home `home` of the daemon that runs now, or None.
#[cfg(not(windows))]
fn running_in(home: &Path) -> Option<DaemonInfo> {
    let text = std::fs::read_to_string(home.join("daemon.json")).ok()?;
    parse_info(&text).ok().filter(|info| is_daemon(info.pid))
}

/// The PATH for finding brindle and for the daemon it starts. An app opened
/// from the Finder inherits a bare PATH (/usr/bin:/bin:/usr/sbin:/sbin), so
/// where `uv tool install` and Homebrew put brindle, tmux and the agents'
/// CLIs are added after the inherited entries, which keep their precedence.
pub fn child_path(inherited: Option<&OsStr>, home: &Path) -> Result<OsString, String> {
    let mut dirs: Vec<PathBuf> = inherited
        .map(|p| std::env::split_paths(p).collect())
        .unwrap_or_default();
    for dir in [
        home.join(".local/bin"),
        "/opt/homebrew/bin".into(),
        "/usr/local/bin".into(),
    ] {
        if !dirs.contains(&dir) {
            dirs.push(dir);
        }
    }
    std::env::join_paths(dirs).map_err(|e| format!("PATH: {e}"))
}

/// The `brindle` to start: $BRINDLE_BIN, else the first on `path`.
#[cfg(not(windows))]
fn find_brindle(path: &OsStr) -> Option<PathBuf> {
    if let Some(bin) = std::env::var_os("BRINDLE_BIN") {
        return Some(bin.into());
    }
    std::env::split_paths(path)
        .map(|d| d.join("brindle"))
        .find(|p| p.is_file())
}

/// `brindle daemon --idle-exit` with `path` for its PATH, in its own process
/// group (it outlives the app, and exits once idle). How it is started is
/// `start_daemon`'s: on macOS as launchd's job, outside the app's coalition.
#[cfg_attr(windows, allow(dead_code))]
fn daemon_command(bin: &Path, path: &OsStr) -> Command {
    let mut cmd = Command::new(bin);
    cmd.args(["daemon", "--idle-exit"])
        .env("PATH", path)
        .stdin(Stdio::null())
        .stdout(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    cmd
}

/// A daemon this app started, before it listens: what started it says
/// whether it still runs.
#[cfg(not(windows))]
enum Daemon {
    /// A child of the app, in a process group of its own: Linux, and macOS
    /// when the person has switched brindle off under Allow in the Background.
    Child(std::process::Child),
    /// A job of the person's launchd (macOS), see `start_daemon`: `pid` once
    /// launchd has spawned it. `adopted`: another start's job (an earlier app
    /// process's), found loaded and waited on rather than started here.
    #[cfg(target_os = "macos")]
    Launchd { label: String, pid: Option<u32>, adopted: bool },
}

#[cfg(not(windows))]
impl Daemon {
    /// Its pid: 0 for a launchd job not spawned yet.
    fn id(&self) -> u32 {
        match self {
            Daemon::Child(child) => child.id(),
            #[cfg(target_os = "macos")]
            Daemon::Launchd { pid, .. } => pid.unwrap_or(0),
        }
    }

    /// Whether the daemon `pid` is the one this start made. An adopted job's
    /// isn't: its daemon.json is judged as any daemon found running is.
    fn owns(&self, pid: u32) -> bool {
        match self {
            Daemon::Child(child) => child.id() == pid,
            #[cfg(target_os = "macos")]
            Daemon::Launchd { pid: own, adopted, .. } => !adopted && *own == Some(pid),
        }
    }

    /// Whether this is another start's job, adopted (see `Launchd`).
    fn adopted(&self) -> bool {
        match self {
            Daemon::Child(_) => false,
            #[cfg(target_os = "macos")]
            Daemon::Launchd { adopted, .. } => *adopted,
        }
    }

    /// None while it runs; how it ended once it has (an exit status's words,
    /// or launchd's `last exit code = 1`). A launchd job that has ended is
    /// unloaded, so the next start loads its job afresh.
    fn try_wait(&mut self) -> Result<Option<String>, String> {
        match self {
            Daemon::Child(child) => Ok(child.try_wait().map_err(|e| e.to_string())?.map(|status| status.to_string())),
            #[cfg(target_os = "macos")]
            Daemon::Launchd { label, pid, .. } => match launchd_job(label)? {
                Some(job) if job.pid.is_some() => {
                    *pid = job.pid;
                    Ok(None)
                }
                // Loaded, and launchd hasn't spawned it yet.
                Some(job) if job.runs == 0 => Ok(None),
                Some(job) => {
                    let _ = launchctl(&["bootout", &launchd_service(label)]);
                    Ok(Some(job.exit))
                }
                None => Ok(Some("unloaded from launchd".into())),
            },
        }
    }

    /// Done waiting on it: a child is reaped once it exits; launchd reaps its own jobs.
    fn release(self) {
        match self {
            Daemon::Child(mut child) => {
                std::thread::spawn(move || child.wait());
            }
            #[cfg(target_os = "macos")]
            Daemon::Launchd { .. } => {}
        }
    }
}

/// Start the daemon `cmd` (`daemon_command`) as a child of the app, in a
/// process group of its own, its stderr appended to `log`.
#[cfg(unix)]
fn start_child(mut cmd: Command, log: &Path) -> Result<Daemon, String> {
    let log = File::options().append(true).open(log).map_err(|e| format!("{}: {e}", log.display()))?;
    let child = cmd
        .stderr(log)
        .spawn()
        .map_err(|e| format!("couldn't start {} daemon: {e}", Path::new(cmd.get_program()).display()))?;
    Ok(Daemon::Child(child))
}

/// What the app's child daemon is told the app's pid is, so it shuts down
/// (gracefully, pausing what it runs) once the app has gone without stopping
/// it: a force quit (brindle.daemon.lifecycle's APP_PID_ENV).
#[cfg(target_os = "macos")]
const APP_PID_ENV: &str = "BRINDLE_APP_PID";

/// The daemons this app process started as its own children in place of
/// launchd's job (`start_app_child`), by pid: the app stops the one serving
/// its home when it exits (`stop_app_daemon`). A launchd daemon is not owned
/// by this list and survives the app's exit.
#[cfg(target_os = "macos")]
static APP_CHILDREN: Mutex<Vec<u32>> = Mutex::new(Vec::new());

/// Start the daemon `cmd` as the app's own child, when launchd won't run its
/// job: told the app's pid (APP_PID_ENV) to watch for a force quit, and
/// recorded as this app's (APP_CHILDREN) for a normal exit to stop.
#[cfg(target_os = "macos")]
fn start_app_child(mut cmd: Command, log: &Path) -> Result<Daemon, String> {
    cmd.env(APP_PID_ENV, std::process::id().to_string());
    let daemon = start_child(cmd, log)?;
    if let Ok(mut own) = APP_CHILDREN.lock() {
        own.push(daemon.id());
    }
    Ok(daemon)
}

/// The app exits normally: the daemon serving brindle's home is stopped as a
/// replaced one is (`stop_daemon`: SIGTERM, so it shuts down gracefully and
/// pauses the sessions it runs, for the next app's daemon to resume), if this
/// app started it as its own child (`start_app_child`). launchd's job, and a
/// daemon some other app or person started, are left alone.
#[cfg(target_os = "macos")]
pub(crate) fn stop_app_daemon() {
    stop_app_daemon_in(&brindle_home());
}

#[cfg(target_os = "macos")]
fn stop_app_daemon_in(home: &Path) {
    let own = APP_CHILDREN.lock().map(|own| own.clone()).unwrap_or_default();
    if let Some(info) = running_in(home).filter(|info| own.contains(&info.pid)) {
        let _ = stop_daemon(home, info.pid, STOP_GRACE);
    }
}

/// Start the daemon `cmd` (`daemon_command`) with brindle's home `home`, its
/// stderr appended to `log`: a child of the app.
#[cfg(all(unix, not(target_os = "macos")))]
fn start_daemon(cmd: Command, _home: &Path, log: &Path) -> Result<Daemon, String> {
    start_child(cmd, log)
}

/// Start the daemon `cmd` (`daemon_command`) with brindle's home `home`, its
/// stderr appended to `log`: as a job of the person's launchd that the app
/// loads (its plist in brindle's home, written afresh at every start: this
/// engine's path and PATH, as the bundle is now) and kickstarts, not as a
/// child. launchd gives the job a coalition of its own; a child of the app is
/// in the app's, and so is everything it starts (the tmux server, every
/// agent's pane), whatever its process group. LaunchServices counts the
/// coalition's members as the app's subordinates: an updated app's old Dock
/// icon stayed as "exited-with-subordinates" while they ran, and quitting
/// that icon killed them all. A launchd daemon can outlive the app and be
/// reused by the next instance. When replaced, it pauses its sessions for
/// the next daemon to resume (brindle.daemon.lifecycle).
///
/// One job per home and app process (`launchd_label`): a job still running
/// under it is a daemon an earlier start of this process made, and is
/// adopted and waited on instead of starting a second, which would find its
/// lock and exit; `start_in` judges the daemon it turns out to be
/// (`launchd_plan`). An earlier app process's job keeps its own label, so
/// unloading it never takes this process's job. A relaunch briefly waits
/// for a predecessor that is already pausing (`wait_for_predecessor`).
///
/// Switched off under Login Items → Allow in the Background
/// (`launchd_disabled`), launchd won't run it: the daemon is then the app's
/// own child (`start_app_child`), which the app stops when it exits
/// (`stop_app_daemon`) and which stops itself once the app is gone after a
/// force quit, pausing what it runs either way.
#[cfg(target_os = "macos")]
fn start_daemon(cmd: Command, home: &Path, log: &Path) -> Result<Daemon, String> {
    let label = launchd_label(home, std::process::id());
    if launchd_disabled(&label)? {
        return start_app_child(cmd, log);
    }
    match launchd_plan(launchd_job(&label)?.as_ref()) {
        Plan::Adopt(pid) => return Ok(Daemon::Launchd { label, pid: Some(pid), adopted: true }),
        Plan::Reload => {
            launchctl(&["bootout", &launchd_service(&label)])?;
        }
        Plan::Load => {}
    }
    let plist = home.join(LAUNCHD_PLIST);
    let staged = home.join(format!("{LAUNCHD_PLIST}.{}", std::process::id()));
    // The person's own, like daemon.json: it carries the app's environment.
    use std::os::unix::fs::OpenOptionsExt;
    File::options()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&staged)
        .and_then(|mut file| file.write_all(daemon_plist(&label, &cmd, log).as_bytes()))
        .and_then(|()| std::fs::rename(&staged, &plist))
        .map_err(|e| {
            let _ = std::fs::remove_file(&staged);
            format!("{}: {e}", plist.display())
        })?;
    launchctl(&["bootstrap", &launchd_domain(), &plist.to_string_lossy()])?;
    launchctl(&["kickstart", &launchd_service(&label)])?;
    let pid = launchd_job(&label)?.and_then(|job| job.pid);
    Ok(Daemon::Launchd { label, pid, adopted: false })
}

/// What `start_daemon` does about the job launchd has under the daemon's label.
#[cfg(target_os = "macos")]
#[derive(Debug, PartialEq)]
enum Plan {
    /// None is loaded: load this start's.
    Load,
    /// It runs (its pid): another start's, adopted.
    Adopt(u32),
    /// Loaded and idle (at login, or its last run over): unloaded, then this
    /// start's plist is loaded and run.
    Reload,
}

#[cfg(target_os = "macos")]
fn launchd_plan(job: Option<&LaunchdJob>) -> Plan {
    match job {
        None => Plan::Load,
        Some(LaunchdJob { pid: Some(pid), .. }) => Plan::Adopt(*pid),
        Some(_) => Plan::Reload,
    }
}

/// Whether the person has switched the daemon's job `label` off (System
/// Settings → Login Items → Allow in the Background): launchd then refuses
/// to load it, with no more than an I/O error to say so.
#[cfg(target_os = "macos")]
fn launchd_disabled(label: &str) -> Result<bool, String> {
    Ok(disabled_in(&launchctl(&["print-disabled", &launchd_domain()])?, label))
}

/// Whether `launchctl print-disabled`'s `text` lists `label` as disabled (`"label" => disabled`).
#[cfg(target_os = "macos")]
fn disabled_in(text: &str, label: &str) -> bool {
    let entry = format!("\"{label}\" => ");
    text.lines().any(|line| line.trim().strip_prefix(&entry).is_some_and(|state| state.trim() == "disabled"))
}

/// The launchd label of the daemon the app process `app` runs on brindle's
/// home `home`: one per home (an app on another home, BRINDLE_HOME, has a
/// job of its own) and per app process (see `start_daemon`).
#[cfg(target_os = "macos")]
fn launchd_label(home: &Path, app: u32) -> String {
    use std::os::unix::ffi::OsStrExt;
    // FNV-1a over the path: short, stable, and only a label's characters.
    let hash = home
        .as_os_str()
        .as_bytes()
        .iter()
        .fold(0xcbf2_9ce4_8422_2325_u64, |h, b| (h ^ u64::from(*b)).wrapping_mul(0x0100_0000_01b3));
    format!("com.pawdelta.brindle.daemon.{hash:016x}.{app}")
}

/// Relaunched into an update (instance::relaunch), after the app process
/// `predecessor` has exited: a healthy daemon survives and is judged by the
/// normal start path (`reuse_running`). Only one already pausing needs a
/// brief exit wait here. If still pausing afterwards, `ensure_in` finishes
/// its shutdown before starting a replacement. Unload an idle job left behind.
#[cfg(target_os = "macos")]
pub(crate) fn wait_for_predecessor(predecessor: u32) {
    let home = brindle_home();
    let label = launchd_label(&home, predecessor);
    wait_for_predecessor_job(&home, &label, launchd_job);
}

#[cfg(target_os = "macos")]
fn wait_for_predecessor_job(home: &Path, label: &str, job: impl Fn(&str) -> Result<Option<LaunchdJob>, String>) {
    let Ok(Some(found)) = job(label) else { return };
    if let Some(pid) = found.pid.filter(|pid| pausing(home, *pid)) {
        let limit = libc::timespec { tv_sec: STOP_GRACE.as_secs() as _, tv_nsec: 0 };
        crate::instance::wait_for_exit_with_limit(pid, &limit);
    }
    if matches!(job(label), Ok(Some(job)) if job.pid.is_none()) {
        let _ = launchctl(&["bootout", &launchd_service(label)]);
    }
}

/// The person's launchd domain: their GUI session's.
#[cfg(target_os = "macos")]
fn launchd_domain() -> String {
    // SAFETY: getuid has no preconditions and only reads.
    format!("gui/{}", unsafe { libc::getuid() })
}

/// The job `label` as launchctl names it, in the person's domain.
#[cfg(target_os = "macos")]
fn launchd_service(label: &str) -> String {
    format!("{}/{label}", launchd_domain())
}

/// `launchctl args`: its output, or what it said when it failed.
#[cfg(target_os = "macos")]
fn launchctl(args: &[&str]) -> Result<String, String> {
    let out = Command::new("/bin/launchctl")
        .args(args)
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("couldn't run launchctl: {e}"))?;
    if !out.status.success() {
        return Err(format!("launchctl {}: {}", args.join(" "), String::from_utf8_lossy(&out.stderr).trim()));
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

/// What launchd says of a job: its pid while it runs, how many times it ran,
/// and its `last exit code = …` line.
#[cfg(target_os = "macos")]
struct LaunchdJob {
    pid: Option<u32>,
    runs: u32,
    exit: String,
}

/// The job `label` as launchd has it (`launchctl print`), or None when none is loaded.
#[cfg(target_os = "macos")]
fn launchd_job(label: &str) -> Result<Option<LaunchdJob>, String> {
    match launchctl(&["print", &launchd_service(label)]) {
        Ok(text) => Ok(Some(parse_launchd_job(&text))),
        Err(why) if why.contains("Could not find service") => Ok(None),
        Err(why) => Err(why),
    }
}

/// A job as `launchctl print` describes it: its `pid = N` (only while it
/// runs), `runs = N` and `last exit code = …` lines.
#[cfg(target_os = "macos")]
fn parse_launchd_job(text: &str) -> LaunchdJob {
    let field = |name: &str| text.lines().map(str::trim).find_map(|line| line.strip_prefix(name)?.strip_prefix(" = "));
    LaunchdJob {
        pid: field("pid").and_then(|v| v.parse().ok()),
        runs: field("runs").and_then(|v| v.parse().ok()).unwrap_or(0),
        exit: format!("last exit code = {}", field("last exit code").unwrap_or("unknown")),
    }
}

/// The launchd job `label` that runs `cmd` (its program and arguments, with
/// its environment over the app's own, as a child would get), with stderr
/// appended to `log`. It runs only when the app kickstarts it, never when
/// loaded, and is never restarted: a second daemon would only find the first
/// one's lock. What it starts (the tmux server, a hook) is left running when
/// it exits, as a child's would be. SIGTERM on replacement makes the daemon
/// pause the sessions it runs.
#[cfg(target_os = "macos")]
fn daemon_plist(label: &str, cmd: &Command, log: &Path) -> String {
    let escape = |s: &str| s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;");
    let mut env: std::collections::BTreeMap<String, String> = std::env::vars().collect();
    for (key, value) in cmd.get_envs() {
        let key = key.to_string_lossy().into_owned();
        match value {
            Some(value) => env.insert(key, value.to_string_lossy().into_owned()),
            None => env.remove(&key),
        };
    }
    let mut plist = String::from(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
         <!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n\
         <plist version=\"1.0\">\n<dict>\n",
    );
    plist += &format!("\t<key>Label</key>\n\t<string>{}</string>\n", escape(label));
    plist += "\t<key>ProgramArguments</key>\n\t<array>\n";
    for arg in std::iter::once(cmd.get_program()).chain(cmd.get_args()) {
        plist += &format!("\t\t<string>{}</string>\n", escape(&arg.to_string_lossy()));
    }
    plist += "\t</array>\n\t<key>EnvironmentVariables</key>\n\t<dict>\n";
    for (key, value) in &env {
        plist += &format!("\t\t<key>{}</key>\n\t\t<string>{}</string>\n", escape(key), escape(value));
    }
    plist += "\t</dict>\n";
    if let Some(cwd) = cmd.get_current_dir().map(Path::to_path_buf).or_else(|| std::env::current_dir().ok()) {
        plist += &format!("\t<key>WorkingDirectory</key>\n\t<string>{}</string>\n", escape(&cwd.to_string_lossy()));
    }
    plist += &format!("\t<key>StandardErrorPath</key>\n\t<string>{}</string>\n", escape(&log.to_string_lossy()));
    plist += "\t<key>RunAtLoad</key>\n\t<false/>\n\
              \t<key>KeepAlive</key>\n\t<false/>\n\
              \t<key>AbandonProcessGroup</key>\n\t<true/>\n\
              \t<key>ProcessType</key>\n\t<string>Interactive</string>\n\
              </dict>\n</plist>\n";
    plist
}

pub(crate) fn log_tail(path: &Path) -> String {
    let text = std::fs::read_to_string(path).unwrap_or_default();
    let text = text.trim();
    let start = text
        .char_indices()
        .rev()
        .nth(500)
        .map_or(0, |(i, _)| i);
    text[start..].to_string()
}

/// Whether what an exited `brindle daemon` left on stderr is click's refusal
/// of a command it doesn't have: a brindle from before the daemon (0.4.x).
pub(crate) fn lacks_daemon(log: &str) -> bool {
    log.contains("No such command 'daemon'") || log.contains("No such command \"daemon\"")
}

/// The failure for a brindle too old to run the daemon, with its version when known.
pub(crate) fn too_old(version: Option<&str>) -> String {
    let which = version.map_or_else(|| "the installed brindle".to_string(), |v| format!("brindle {v}"));
    format!("{ENGINE_TOO_OLD}: {which} has no `daemon` command. Upgrade it: uv tool upgrade brindle")
}

/// What `brindle --version` says (click's "brindle, version 0.4.1"): its last word.
#[cfg(not(windows))]
fn version_of(bin: &Path, path: &OsStr) -> Option<String> {
    let out = Command::new(bin)
        .arg("--version")
        .env("PATH", path)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    text.split_whitespace().last().map(str::to_string)
}

/// Start `brindle daemon --idle-exit` and wait until it listens: the engine
/// the app carries (see `engine`); one found on PATH only in a dev build.
#[cfg(not(windows))]
fn start(engine: Option<&Path>) -> Result<DaemonInfo, String> {
    // uv's tool bin directory too, wherever UV_TOOL_BIN_DIR put it (setup::tool_path):
    // the agents' CLIs are found there.
    let path = crate::setup::tool_path()?;
    let bin = crate::engine::brindle_to_start(engine, crate::engine::PATH_FALLBACK, || find_brindle(&path))?;
    start_in(&bin, &path, &brindle_home(), engine, START_TIMEOUT)
}

/// A daemon this app started that didn't listen yet when its start stopped
/// waiting, and when it was started.
#[cfg(not(windows))]
struct Starting {
    child: Daemon,
    since: Instant,
}

/// The starts still under way, by brindle's home. The next start waits on its
/// home's instead of starting another daemon, which would find the first one's
/// lock and exit: a failure, when nothing failed.
#[cfg(not(windows))]
static STARTING: Mutex<Option<HashMap<PathBuf, Starting>>> = Mutex::new(None);

/// Whether a start in brindle's home `home` is still under way.
#[cfg(not(windows))]
fn starting_in(home: &Path) -> bool {
    STARTING.lock().is_ok_and(|s| s.as_ref().is_some_and(|s| s.contains_key(home)))
}

/// What a start is doing, for the webview's "Starting brindle…" to say
/// (`daemon_start_step`) while it waits.
#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "step", rename_all = "lowercase")]
pub enum StartStep {
    /// Stopping daemon `pid`: another build's (from before an update), or another install's.
    Stopping { pid: u32 },
    /// The engine's daemon `pid` runs and doesn't answer yet.
    Starting { pid: u32 },
}

/// Each brindle home's start under way: its step, and since when.
#[cfg(not(windows))]
static STEPS: Mutex<Option<HashMap<PathBuf, (StartStep, Instant)>>> = Mutex::new(None);

/// Record `home`'s start as at `step` since `since`; None: no start is under way.
#[cfg(not(windows))]
fn set_step(home: &Path, step: Option<(StartStep, Instant)>) {
    if let Ok(mut steps) = STEPS.lock() {
        let steps = steps.get_or_insert_with(HashMap::new);
        match step {
            Some(step) => steps.insert(home.to_path_buf(), step),
            None => steps.remove(home),
        };
    }
}

/// `home`'s start under way: its step, and how long it has been at it.
#[cfg(not(windows))]
fn step_in(home: &Path) -> Option<(StartStep, Duration)> {
    let steps = STEPS.lock().ok()?;
    let (step, since) = steps.as_ref()?.get(home)?;
    Some((step.clone(), since.elapsed()))
}

/// Start `bin daemon --idle-exit` with brindle's home `home` (or go on waiting
/// for the one started there before) and wait until it listens, for up to
/// `patience`: past it, a daemon still starting answers `ENGINE_STARTING`.
/// With an `engine`, a daemon someone else started meanwhile is used only if
/// `reuse` would.
#[cfg(not(windows))]
fn start_in(
    bin: &Path,
    path: &OsStr,
    home: &Path,
    engine: Option<&Path>,
    patience: Duration,
) -> Result<DaemonInfo, String> {
    let usable = |info: &DaemonInfo| engine.map_or(true, |engine| reuse_running(engine, info) == Reuse::Use);
    let log_path = home.join(START_LOG);
    let pending = STARTING.lock().ok().and_then(|mut s| s.as_mut()?.remove(home));
    let Starting { mut child, since } = match pending {
        Some(starting) => starting,
        None => {
            std::fs::create_dir_all(home).map_err(|e| format!("{}: {e}", home.display()))?;
            // Each start's log begins afresh.
            File::create(&log_path).map_err(|e| format!("{}: {e}", log_path.display()))?;
            let child = start_daemon(daemon_command(bin, path), home, &log_path)?;
            Starting { child, since: Instant::now() }
        }
    };
    set_step(home, Some((StartStep::Starting { pid: child.id() }, since)));
    let waiting = Instant::now();
    loop {
        // daemon.json is written once the sockets are bound, a moment before they listen.
        let listening = running_in(home).filter(|info| Conn::open(info).is_ok());
        if let Some(info) = listening {
            if child.owns(info.pid) || usable(&info) {
                child.release();
                return ready_in(home, info);
            }
            if child.adopted() {
                // An adopted job's daemon, and not one to use: judged as ensure_in judges
                // one found running. Another build's is stopped, and this start's own
                // takes its place.
                match engine.map(|engine| reuse_running(engine, &info)) {
                    Some(Reuse::Conflict(why)) => return Err(why),
                    _ => {
                        set_step(home, Some((StartStep::Stopping { pid: info.pid }, since)));
                        stop_daemon(home, info.pid, STOP_GRACE)?;
                        child = start_daemon(daemon_command(bin, path), home, &log_path)?;
                        set_step(home, Some((StartStep::Starting { pid: child.id() }, since)));
                        continue;
                    }
                }
            }
        }
        if let Some(status) = child.try_wait()? {
            // One started by someone else in the meantime makes this one exit: use that, if it may be.
            if let Some(info) = running_in(home) {
                return match engine.map(|engine| reuse_running(engine, &info)) {
                    None | Some(Reuse::Use) => ready_in(home, info),
                    Some(Reuse::Conflict(why)) => Err(why),
                    Some(Reuse::Replace) => Err(format!(
                        "another brindle daemon (pid {}) started while this app started its own",
                        info.pid
                    )),
                };
            }
            let tail = log_tail(&log_path);
            if lacks_daemon(&tail) {
                return Err(too_old(version_of(bin, path).as_deref()));
            }
            return Err(format!("`brindle daemon` exited ({status}): {tail}"));
        }
        if waiting.elapsed() > patience {
            let pid = child.id();
            if let Ok(mut starting) = STARTING.lock() {
                starting.get_or_insert_with(HashMap::new).insert(home.to_path_buf(), Starting { child, since });
            }
            return Err(format!(
                "{ENGINE_STARTING}: `brindle daemon` (pid {pid}) has been starting for {}s",
                since.elapsed().as_secs()
            ));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// A daemon found during startup may already be pausing even though it still
/// listens and its build matches. Hand control back to normal discovery so
/// `ensure_in` finishes that shutdown before starting its replacement.
#[cfg(not(windows))]
fn ready_in(home: &Path, info: DaemonInfo) -> Result<DaemonInfo, String> {
    if pausing(home, info.pid) {
        set_step(home, Some((StartStep::Stopping { pid: info.pid }, Instant::now())));
        return Err(format!("{ENGINE_STARTING}: the previous daemon (pid {}) is pausing", info.pid));
    }
    Ok(info)
}

/// The running daemon, started first if none runs. An app that carries its
/// engine uses only that engine's, as it is now (see `reuse`).
#[cfg(not(windows))]
fn ensure() -> Result<DaemonInfo, String> {
    let engine = crate::engine::bundled_engine();
    let home = brindle_home();
    let got = ensure_in(&home, engine.as_deref(), crate::engine::PATH_FALLBACK, reuse_running, || {
        start(engine.as_deref())
    });
    // A daemon still starting is waited on again (ENGINE_STARTING): its step stands till then.
    if !matches!(&got, Err(why) if why.starts_with(ENGINE_STARTING)) {
        set_step(&home, None);
    }
    got
}

/// `ensure` with brindle's home `home`: the daemon running there, judged by
/// `judge` when the app carries an `engine`, else one `start` started. A
/// build without an engine that may not fall back to PATH (`path_fallback`:
/// a macOS release) uses no daemon at all, nor does an engine without its
/// build id: both are checked before any daemon is looked for.
#[cfg(not(windows))]
fn ensure_in(
    home: &Path,
    engine: Option<&Path>,
    path_fallback: bool,
    judge: impl FnOnce(&Path, &DaemonInfo) -> Reuse,
    start: impl FnOnce() -> Result<DaemonInfo, String>,
) -> Result<DaemonInfo, String> {
    match engine {
        Some(engine) => {
            crate::engine::require_build(engine)?;
        }
        None if !path_fallback => return Err(crate::engine::MISSING_ENGINE.into()),
        None => {}
    }
    // A start still under way goes on: its daemon may have written daemon.json
    // but not listen yet, and start waits for that (and judges a daemon found
    // in its place).
    if starting_in(home) {
        return start();
    }
    let Some(info) = running_in(home) else { return start() };
    let reuse = engine.map_or(Reuse::Use, |engine| judge(engine, &info));
    match reuse {
        Reuse::Use if !pausing(home, info.pid) => Ok(info),
        // A daemon already pausing cannot serve this instance, even when its
        // build matches. Preserve its pause budget and wait for it to release
        // the home before starting another daemon, just as replacement does.
        Reuse::Use | Reuse::Replace => {
            set_step(home, Some((StartStep::Stopping { pid: info.pid }, Instant::now())));
            stop_daemon(home, info.pid, STOP_GRACE)?;
            start()
        }
        Reuse::Conflict(why) => Err(why),
    }
}

/// The daemon running in WSL, started there first if none answers.
#[cfg(windows)]
fn ensure() -> Result<DaemonInfo, String> {
    crate::wsl::ensure()
}

/// Whether the TCP-only daemon `info` names still runs: its pid, still a daemon's.
#[cfg(not(windows))]
fn still_running(info: &DaemonInfo) -> bool {
    is_daemon(info.pid)
}

/// On Windows the pid is a Linux one inside WSL, which nothing here can
/// check: the daemon found last (`wsl::running_at` checked it answered) is
/// used while its daemon.json still names its port and token. One that exits
/// removes the file, so the port it gave up isn't connected to again.
#[cfg(windows)]
fn still_running(info: &DaemonInfo) -> bool {
    crate::wsl::still_named(info)
}

/// A connection to the daemon, on whichever transport daemon.json names.
enum Conn {
    #[cfg(unix)]
    Unix(UnixStream),
    Tcp(TcpStream),
}

impl Conn {
    /// The Unix socket whenever daemon.json names one; TCP only without.
    fn open(info: &DaemonInfo) -> io::Result<Conn> {
        #[cfg(unix)]
        if let Some(path) = &info.socket {
            return UnixStream::connect(path).map(Conn::Unix);
        }
        match info.port {
            Some(port) => TcpStream::connect(("127.0.0.1", port)).map(Conn::Tcp),
            None => Err(io::Error::new(
                io::ErrorKind::NotFound,
                "the daemon listens on no transport this app can use",
            )),
        }
    }

    fn try_clone(&self) -> io::Result<Conn> {
        match self {
            #[cfg(unix)]
            Conn::Unix(s) => s.try_clone().map(Conn::Unix),
            Conn::Tcp(s) => s.try_clone().map(Conn::Tcp),
        }
    }

    fn set_timeout(&self, timeout: Option<Duration>) -> io::Result<()> {
        match self {
            #[cfg(unix)]
            Conn::Unix(s) => s.set_read_timeout(timeout).and(s.set_write_timeout(timeout)),
            Conn::Tcp(s) => s.set_read_timeout(timeout).and(s.set_write_timeout(timeout)),
        }
    }

    fn set_read_timeout(&self, timeout: Option<Duration>) -> io::Result<()> {
        match self {
            #[cfg(unix)]
            Conn::Unix(s) => s.set_read_timeout(timeout),
            Conn::Tcp(s) => s.set_read_timeout(timeout),
        }
    }

    fn shutdown(&self) {
        let _ = match self {
            #[cfg(unix)]
            Conn::Unix(s) => s.shutdown(Shutdown::Both),
            Conn::Tcp(s) => s.shutdown(Shutdown::Both),
        };
    }
}

impl Read for Conn {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            #[cfg(unix)]
            Conn::Unix(s) => s.read(buf),
            Conn::Tcp(s) => s.read(buf),
        }
    }
}

impl Write for Conn {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self {
            #[cfg(unix)]
            Conn::Unix(s) => s.write(buf),
            Conn::Tcp(s) => s.write(buf),
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        match self {
            #[cfg(unix)]
            Conn::Unix(s) => s.flush(),
            Conn::Tcp(s) => s.flush(),
        }
    }
}

/// An HTTP answer as the webview gets it: the status and the body's text.
#[derive(Debug, PartialEq, Serialize)]
pub struct Reply {
    pub status: u16,
    pub body: String,
}

/// Parse a whole HTTP/1.1 response (the connection was closed after it).
pub fn parse_response(raw: &[u8]) -> Result<Reply, String> {
    let mut headers = [httparse::EMPTY_HEADER; 64];
    let mut res = httparse::Response::new(&mut headers);
    let start = match res.parse(raw).map_err(|e| format!("bad answer from the daemon: {e}"))? {
        httparse::Status::Complete(n) => n,
        httparse::Status::Partial => return Err("the daemon's answer ended early".into()),
    };
    let header = |name: &str| {
        res.headers
            .iter()
            .find(|h| h.name.eq_ignore_ascii_case(name))
            .map(|h| String::from_utf8_lossy(h.value).to_ascii_lowercase())
    };
    let rest = &raw[start..];
    let body = if header("transfer-encoding").is_some_and(|v| v.contains("chunked")) {
        dechunk(rest)?
    } else if let Some(n) = header("content-length").and_then(|v| v.trim().parse().ok()) {
        rest.get(..n).ok_or("the daemon's answer ended early")?.to_vec()
    } else {
        rest.to_vec()
    };
    Ok(Reply {
        status: res.code.unwrap_or(0),
        body: String::from_utf8_lossy(&body).into_owned(),
    })
}

/// Whether `raw` holds a whole answer: its head and all of the body its
/// Content-Length or last chunk says. One with neither ends with the connection.
fn answered(raw: &[u8]) -> bool {
    let mut headers = [httparse::EMPTY_HEADER; 64];
    let mut res = httparse::Response::new(&mut headers);
    let Ok(httparse::Status::Complete(start)) = res.parse(raw) else { return false };
    let header = |name: &str| {
        res.headers
            .iter()
            .find(|h| h.name.eq_ignore_ascii_case(name))
            .map(|h| String::from_utf8_lossy(h.value).to_ascii_lowercase())
    };
    if header("transfer-encoding").is_some_and(|v| v.contains("chunked")) {
        return dechunk(&raw[start..]).is_ok();
    }
    match header("content-length").and_then(|v| v.trim().parse::<usize>().ok()) {
        Some(n) => raw.len() >= start + n,
        None => false,
    }
}

fn dechunk(mut data: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    loop {
        let (start, size) = match httparse::parse_chunk_size(data) {
            Ok(httparse::Status::Complete(found)) => found,
            _ => return Err("bad chunked answer from the daemon".into()),
        };
        if size == 0 {
            return Ok(out);
        }
        let end = start + usize::try_from(size).map_err(|e| e.to_string())?;
        out.extend_from_slice(data.get(start..end).ok_or("the daemon's answer ended early")?);
        data = data.get(end + 2..).ok_or("the daemon's answer ended early")?;
    }
}

fn send_request(
    conn: &mut Conn,
    token: &str,
    method: &str,
    path: &str,
    body: Option<&str>,
) -> io::Result<Reply> {
    let body = body.unwrap_or("");
    let mut head = format!(
        "{method} {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n\
         Accept: application/json\r\nConnection: close\r\n"
    );
    if method != "GET" {
        head += &format!(
            "Content-Type: application/json\r\nContent-Length: {}\r\n",
            body.len()
        );
    }
    head += "\r\n";
    conn.write_all(head.as_bytes())?;
    conn.write_all(body.as_bytes())?;
    conn.flush()?;
    // Up to the answer's end, not the connection's: a daemon slow to close it
    // after answering would hold the connection (and the request) meanwhile.
    let mut raw = Vec::new();
    let mut buf = [0u8; 16 * 1024];
    while !answered(&raw) {
        match conn.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => raw.extend_from_slice(&buf[..n]),
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    conn.shutdown();
    if raw.is_empty() {
        // Accepted, then closed without a byte: the daemon went away under the request.
        return Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "the daemon closed the connection without answering",
        ));
    }
    parse_response(&raw).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
}

/// Whether a request's failure means the daemon died under it (the connection
/// was reset, broke, or closed with no answer), rather than a bad answer or a
/// slow one.
fn died(e: &io::Error) -> bool {
    matches!(
        e.kind(),
        io::ErrorKind::ConnectionReset
            | io::ErrorKind::ConnectionAborted
            | io::ErrorKind::BrokenPipe
            | io::ErrorKind::UnexpectedEof
            | io::ErrorKind::NotConnected
    )
}

/// Whether a request's socket timed out waiting on the daemon: `WouldBlock`
/// (EAGAIN, os error 35 on macOS) is what a socket read timeout reports there.
fn timed_out(e: &io::Error) -> bool {
    matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut)
}

/// Only the daemon's v1 routes, and nothing that could split the request line.
fn check_route(path: &str) -> Result<(), String> {
    if !path.starts_with("/v1/") || path.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return Err(format!("not a daemon route: {path:?}"));
    }
    Ok(())
}

/// What a stream sends the webview: the socket opened (the token is sent), a
/// text frame, a binary frame (a terminal's bytes, base64 so it crosses the
/// IPC as JSON), or the close code (1006 when the connection dropped, 1008
/// when the daemon refused the handshake).
#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Frame {
    Open,
    Text { data: String },
    Binary { data: String },
    Close { code: u16 },
}

const BASE64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Standard base64 with padding (what `atob` in the webview decodes).
pub fn base64(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = chunk
            .iter()
            .enumerate()
            .fold(0u32, |n, (i, &b)| n | (u32::from(b) << (16 - 8 * i)));
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(BASE64[((n >> (18 - 6 * i)) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// A stream's socket, shared between the thread reading it and the command
/// that writes the webview's frames to it.
type Socket = Arc<Mutex<tungstenite::WebSocket<Conn>>>;

/// An open stream: the page's claim on its id, then its connection (shut down
/// to end it) once made and, once the handshake is done, its socket.
struct Stream {
    conn: Option<Conn>,
    socket: Option<Socket>,
}

impl Stream {
    fn shutdown(&self) {
        if let Some(conn) = &self.conn {
            conn.shutdown();
        }
    }
}

fn run_stream(
    conn: Conn,
    path: &str,
    token: &str,
    send: &dyn Fn(Frame),
    attach: &dyn Fn(Socket),
) -> u16 {
    let mut ws = match tungstenite::client::client(format!("ws://localhost{path}"), conn) {
        Ok((ws, _)) => ws,
        Err(HandshakeError::Failure(tungstenite::Error::Http(res))) if res.status() == 403 => {
            return 1008
        }
        Err(_) => return 1006,
    };
    // The token goes as the first frame, never in the URL (brindle/daemon/auth.py).
    if ws.send(Message::Text(token.into())).is_err() {
        return 1006;
    }
    // Reads wait only a moment at a time, so a frame the webview sends gets
    // the socket between them (tungstenite resumes a frame cut by a timeout).
    if ws.get_ref().set_read_timeout(Some(STREAM_POLL)).is_err() {
        return 1006;
    }
    let ws = Arc::new(Mutex::new(ws));
    attach(ws.clone());
    send(Frame::Open);
    loop {
        let read = match ws.lock() {
            Ok(mut ws) => ws.read(),
            Err(_) => return 1006,
        };
        match read {
            Ok(Message::Text(text)) => send(Frame::Text {
                data: text.to_string(),
            }),
            Ok(Message::Binary(bytes)) => send(Frame::Binary {
                data: base64(&bytes),
            }),
            Ok(Message::Close(frame)) => return frame.map_or(1005, |f| f.code.into()),
            // Pings are answered by tungstenite; the daemon sends nothing else.
            Ok(_) => {}
            // Nothing arrived within the poll: let a waiting writer in.
            Err(tungstenite::Error::Io(e))
                if matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut) =>
            {
                std::thread::sleep(Duration::from_millis(1));
            }
            Err(_) => return 1006,
        }
    }
}

/// The connections requests may have open at once (`MAX_CONNECTIONS`).
#[derive(Default)]
struct Slots {
    taken: Mutex<usize>,
    freed: std::sync::Condvar,
}

/// A request's hold on one of the `Slots`, given back when dropped.
struct Slot<'a>(&'a Slots);

impl Slots {
    /// Wait for a free slot and take it.
    fn take(&self) -> Result<Slot<'_>, String> {
        let mut taken = self.taken.lock().map_err(|e| e.to_string())?;
        while *taken >= MAX_CONNECTIONS {
            taken = self.freed.wait(taken).map_err(|e| e.to_string())?;
        }
        *taken += 1;
        Ok(Slot(self))
    }
}

impl Drop for Slot<'_> {
    fn drop(&mut self) {
        if let Ok(mut taken) = self.0.taken.lock() {
            *taken -= 1;
        }
        self.0.freed.notify_one();
    }
}

/// The app's one connection to the daemon: its daemon.json, read once and
/// again whenever the daemon it named is gone, and the open streams (the
/// event stream, and a terminal's socket per open pane).
///
/// The engine's self-repair lives here. When the daemon dies (a crash; an
/// idle exit never happens while the app's event stream is open), nothing
/// polls for it: the next thing the webview asks finds it gone and
/// `refresh` runs `ensure`, which sees no live daemon behind daemon.json and
/// starts one, holding the lock so every caller that found it gone waits on
/// that one start. A request whose connection is refused, or which the
/// daemon died under (a GET), is sent again to the new daemon; a stream that
/// drops is reopened by the webview itself (api/events.ts reconnects with
/// backoff), and each reopen comes through `open_stream`, which finds or
/// starts the daemon first. So the app reconnects without anyone's click,
/// and the start is at most a few seconds behind the crash.
#[derive(Clone)]
pub struct Engine {
    info: Arc<Mutex<Option<DaemonInfo>>>,
    streams: Arc<Mutex<HashMap<u32, Stream>>>,
    next_stream: Arc<AtomicU32>,
    /// What bounds the requests' connections open at once.
    slots: Arc<Slots>,
    /// How long a request waits for the daemon's answer (`REQUEST_TIMEOUT`), but for tests.
    request_timeout: Duration,
    /// How the daemon is found: `ensure`, but for tests.
    discover: Arc<dyn Fn() -> Result<DaemonInfo, String> + Send + Sync>,
    /// Run as a stream's connection, made, is about to be kept for it:
    /// nothing, but for tests.
    registering: Arc<dyn Fn() + Send + Sync>,
}

impl Default for Engine {
    fn default() -> Self {
        Engine {
            info: Default::default(),
            streams: Default::default(),
            next_stream: Default::default(),
            slots: Default::default(),
            request_timeout: REQUEST_TIMEOUT,
            discover: Arc::new(ensure),
            registering: Arc::new(|| {}),
        }
    }
}

impl Engine {
    /// Find (or start) the daemon afresh. The lock is held throughout, so
    /// requests that all find the daemon gone start only one.
    fn refresh(&self) -> Result<DaemonInfo, String> {
        let mut info = self.info.lock().map_err(|e| e.to_string())?;
        let found = (self.discover)()?;
        *info = Some(found.clone());
        Ok(found)
    }

    /// The daemon last found. Its Unix socket lives in brindle's 0700 home,
    /// but a TCP port it has let go of can be taken by any local process: on
    /// TCP only, check it's still the daemon before connecting (and sending
    /// the token).
    pub(crate) fn current(&self) -> Result<DaemonInfo, String> {
        let known = self.info.lock().map_err(|e| e.to_string())?.clone();
        match known {
            Some(info) if info.socket.is_some() || still_running(&info) => Ok(info),
            _ => self.refresh(),
        }
    }

    fn request(&self, method: &str, path: &str, body: Option<&str>) -> Result<Reply, String> {
        if !matches!(method, "GET" | "POST" | "PATCH" | "PUT" | "DELETE") {
            return Err(format!("not an HTTP method: {method:?}"));
        }
        check_route(path)?;
        // Held until the answer is in, so the request's connection (closed with it) is counted.
        let _slot = self.slots.take()?;
        let mut info = self.current()?;
        let mut retried = false;
        loop {
            let mut conn = match Conn::open(&info) {
                Ok(conn) => conn,
                // The daemon is gone (idle exit, an engine upgrade): find or start the next, once.
                Err(_) if !retried => {
                    retried = true;
                    info = self.refresh()?;
                    continue;
                }
                Err(e) => return Err(format!("the brindle daemon is unreachable: {e}")),
            };
            conn.set_timeout(Some(self.request_timeout))
                .map_err(|e| e.to_string())?;
            let reply = match send_request(&mut conn, &info.token, method, path, body) {
                Ok(reply) => reply,
                // The daemon didn't answer in time: busy, not broken. Nothing is retried.
                Err(e) if timed_out(&e) => return Err(BUSY_MESSAGE.into()),
                // The daemon died under the request (a crash mid-answer). A GET changed nothing:
                // find or start the next daemon and ask it, once. Anything else may have been
                // acted on before the daemon went, so it is reported, not sent again.
                Err(e) if method == "GET" && !retried && died(&e) => {
                    retried = true;
                    info = self.refresh()?;
                    continue;
                }
                Err(e) => return Err(format!("the brindle daemon: {e}")),
            };
            // A daemon restarted since daemon.json was read has a new token. A 401 ran nothing: retry.
            if reply.status == 401 && !retried {
                retried = true;
                let fresh = self.refresh()?;
                if fresh.token != info.token {
                    info = fresh;
                    continue;
                }
            }
            return Ok(reply);
        }
    }

    /// A GET from the shell itself (the update check, `--headless-check`):
    /// its status and body.
    pub fn get(&self, path: &str) -> Result<(u16, String), String> {
        let reply = self.request("GET", path, None)?;
        Ok((reply.status, reply.body))
    }

    /// Open stream `path` and answer its id at once. It runs as the page's ask
    /// is dispatched (`daemon_stream_open` is a synchronous command, on the
    /// thread page loads are told on), so the id is claimed for the page that
    /// asked before another page can load. The connection is made on the
    /// stream's own thread and kept only while that claim stands: one the page
    /// closed, or left behind (`close_all_streams`), meanwhile is closed there,
    /// as no one is left to close it. A stream that can't connect closes 1006.
    fn open_stream(&self, path: &str, send: impl Fn(Frame) + Send + 'static) -> Result<u32, String> {
        check_route(path)?;
        let id = self.next_stream.fetch_add(1, Ordering::Relaxed);
        self.streams
            .lock()
            .map_err(|e| e.to_string())?
            .insert(id, Stream { conn: None, socket: None });
        let engine = self.clone();
        let path = path.to_string();
        std::thread::spawn(move || {
            let Some((info, conn)) = engine.connect_stream(id) else {
                if let Ok(mut streams) = engine.streams.lock() {
                    streams.remove(&id);
                }
                send(Frame::Close { code: 1006 });
                return;
            };
            // The webview hears of one open, however many connections it takes.
            let opened = AtomicBool::new(false);
            let forward = |frame: Frame| {
                if !(matches!(frame, Frame::Open) && opened.swap(true, Ordering::Relaxed)) {
                    send(frame);
                }
            };
            let attach = |socket: Socket| {
                if let Some(stream) = engine.streams.lock().ok().as_mut().and_then(|s| s.get_mut(&id)) {
                    stream.socket = Some(socket);
                }
            };
            let (mut info, mut conn, mut retried) = (info, conn, false);
            let code = loop {
                let code = run_stream(conn, &path, &info.token, &forward, &attach);
                // 4401 for a token: a daemon restarted on the same socket since daemon.json
                // was read. The webview's stream stops for good on it, so find the new one, once.
                if code != WS_UNAUTHORIZED || retried {
                    break code;
                }
                retried = true;
                match engine.reopen(id, &info) {
                    Some((fresh, fresh_conn)) => (info, conn) = (fresh, fresh_conn),
                    None => break code,
                }
            };
            if let Ok(mut streams) = engine.streams.lock() {
                streams.remove(&id);
            }
            send(Frame::Close { code });
        });
        Ok(id)
    }

    /// Stream `id`'s connection, to the daemon found afresh (a stream (re)opens
    /// when the daemon may have restarted: its token), kept for the stream only
    /// if its page still claims it; else closed, and None, as when no daemon
    /// could be reached.
    fn connect_stream(&self, id: u32) -> Option<(DaemonInfo, Conn)> {
        let info = self.refresh().ok()?;
        let conn = Conn::open(&info).ok()?;
        let handle = conn.try_clone().ok()?;
        (self.registering)();
        let mut streams = self.streams.lock().ok()?;
        match streams.get_mut(&id) {
            Some(stream) => {
                stream.conn = Some(handle);
                Some((info, conn))
            }
            None => {
                drop(streams);
                conn.shutdown();
                None
            }
        }
    }

    /// A new connection for stream `id` to the daemon found now, if it has a
    /// token other than `stale`'s and the webview hasn't closed the stream.
    fn reopen(&self, id: u32, stale: &DaemonInfo) -> Option<(DaemonInfo, Conn)> {
        let fresh = self.refresh().ok().filter(|f| f.token != stale.token)?;
        let conn = Conn::open(&fresh).ok()?;
        let mut streams = self.streams.lock().ok()?;
        *streams.get_mut(&id)? = Stream { conn: Some(conn.try_clone().ok()?), socket: None };
        Some((fresh, conn))
    }

    /// A text frame from the webview (a terminal's control frame) to stream `id`.
    fn send_stream(&self, id: u32, data: String) -> Result<(), String> {
        let socket = self
            .streams
            .lock()
            .map_err(|e| e.to_string())?
            .get(&id)
            .and_then(|s| s.socket.clone())
            .ok_or_else(|| format!("no open stream {id}"))?;
        let mut ws = socket.lock().map_err(|e| e.to_string())?;
        ws.send(Message::Text(data.into()))
            .map_err(|e| format!("the brindle daemon: {e}"))
    }

    fn close_stream(&self, id: u32) {
        if let Some(stream) = self.streams.lock().ok().and_then(|mut s| s.remove(&id)) {
            stream.shutdown();
        }
    }

    /// Close every open stream: the webview (re)loads its page, and the page
    /// that opened them, gone, will never close them itself. One it asked for
    /// that is still connecting loses its claim, and is closed as it connects
    /// (`connect_stream`).
    pub fn close_all_streams(&self) {
        let streams: Vec<Stream> = match self.streams.lock() {
            Ok(mut streams) => streams.drain().map(|(_, stream)| stream).collect(),
            Err(_) => return,
        };
        for stream in streams {
            stream.shutdown();
        }
    }
}

/// The running daemon as the webview may see it: never its token.
#[derive(Serialize)]
pub struct EngineInfo {
    version: String,
    pid: u32,
}

async fn blocking<T: Send + 'static>(
    f: impl FnOnce() -> Result<T, String> + Send + 'static,
) -> Result<T, String> {
    tauri::async_runtime::spawn_blocking(f)
        .await
        .map_err(|e| e.to_string())?
}

/// Find the daemon, starting it when none runs.
#[tauri::command]
pub async fn daemon_connect(engine: State<'_, Engine>) -> Result<EngineInfo, String> {
    let engine = engine.inner().clone();
    let info = blocking(move || engine.refresh()).await?;
    Ok(EngineInfo {
        version: info.version,
        pid: info.pid,
    })
}

/// A start's step as the webview sees it: what, and for how many seconds now.
#[derive(Serialize)]
pub struct StartNow {
    #[serde(flatten)]
    step: StartStep,
    secs: u64,
}

/// What the engine start under way (a `daemon_connect` still waiting) is
/// doing, for "Starting brindle…" to say; none when none is.
#[tauri::command]
pub fn daemon_start_step() -> Option<StartNow> {
    #[cfg(not(windows))]
    return step_in(&brindle_home()).map(|(step, took)| StartNow { step, secs: took.as_secs() });
    #[cfg(windows)]
    None
}

/// One HTTP request to the daemon, the token added here. Rejects only when no
/// answer came back; an error status is a normal reply.
#[tauri::command]
pub async fn daemon_request(
    engine: State<'_, Engine>,
    method: String,
    path: String,
    body: Option<String>,
) -> Result<Reply, String> {
    let engine = engine.inner().clone();
    blocking(move || engine.request(&method, &path, body.as_deref())).await
}

/// Open a WebSocket route (`/v1/events?since=...`);
/// its frames arrive on `channel`, ending with a close. Returns the id
/// `daemon_stream_send` and `daemon_stream_close` take.
///
/// Not async: an async command's body runs only once the runtime polls it,
/// by when the page that asked may have gone (a page load ran between), and a
/// stream it opened then would belong to no page. A synchronous one runs as
/// the ask is dispatched, where page loads are told too (lib.rs on_page_load),
/// and only claims the id; the connecting is the stream's own thread's.
#[tauri::command]
pub fn daemon_stream_open(engine: State<'_, Engine>, path: String, channel: Channel<Frame>) -> Result<u32, String> {
    engine.open_stream(&path, move |frame| {
        let _ = channel.send(frame);
    })
}

/// Send a text frame (a terminal's key, paste or resize control) on stream `id`.
#[tauri::command]
pub async fn daemon_stream_send(engine: State<'_, Engine>, id: u32, data: String) -> Result<(), String> {
    let engine = engine.inner().clone();
    blocking(move || engine.send_stream(id, data)).await
}

#[tauri::command]
pub fn daemon_stream_close(engine: State<'_, Engine>, id: u32) {
    engine.close_stream(id);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_daemon_json() {
        let unix = r#"{"socket": "/home/me/.brindle/daemon.sock", "port": null,
            "token": "abc", "version": "0.5.0", "pid": 4242}"#;
        assert_eq!(
            parse_info(unix),
            Ok(DaemonInfo {
                socket: Some("/home/me/.brindle/daemon.sock".into()),
                port: None,
                token: "abc".into(),
                version: "0.5.0".into(),
                pid: 4242,
            })
        );
        let tcp = parse_info(r#"{"socket": null, "port": 47321, "token": "t", "version": "0.5.0", "pid": 7}"#);
        assert_eq!(tcp.map(|i| (i.socket, i.port)), Ok((None, Some(47321))));

        for bad in [
            "",
            r#"{"socket": "/s", "port": null, "token": "t", "version": "0.5.0"}"#,
            r#"{"socket": "/s", "port": null, "token": "t", "pid": "7"}"#,
            r#"{"socket": null, "port": null, "token": "t", "pid": 7}"#,
            r#"{"socket": "/s", "port": null, "token": "", "pid": 7}"#,
        ] {
            assert!(parse_info(bad).is_err(), "accepted {bad:?}");
        }
    }

    #[test]
    fn parses_plain_and_chunked_answers() {
        let plain = b"HTTP/1.1 401 Unauthorized\r\ncontent-type: application/json\r\n\
            content-length: 13\r\n\r\n{\"error\": {}}";
        assert_eq!(
            parse_response(plain),
            Ok(Reply { status: 401, body: "{\"error\": {}}".into() })
        );
        let chunked = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n\
            4\r\n{\"a\"\r\n3\r\n: 1\r\n1\r\n}\r\n0\r\n\r\n";
        assert_eq!(
            parse_response(chunked),
            Ok(Reply { status: 200, body: "{\"a\": 1}".into() })
        );
        assert!(parse_response(b"HTTP/1.1 200 OK\r\ncontent-length: 10\r\n\r\n{}").is_err());
    }

    #[test]
    fn the_daemon_gets_the_install_dirs_on_a_finder_path() {
        let home = Path::new("/home/me");
        let path = child_path(Some(OsStr::new("/usr/bin:/bin:/usr/sbin:/sbin")), home).unwrap();
        assert_eq!(
            path,
            "/usr/bin:/bin:/usr/sbin:/sbin:/home/me/.local/bin:/opt/homebrew/bin:/usr/local/bin"
        );
        // Entries already there keep their place.
        assert_eq!(
            child_path(Some(OsStr::new("/usr/local/bin:/usr/bin")), home).unwrap(),
            "/usr/local/bin:/usr/bin:/home/me/.local/bin:/opt/homebrew/bin"
        );
        let cmd = daemon_command(Path::new("/home/me/.local/bin/brindle"), &path);
        assert_eq!(cmd.get_args().collect::<Vec<_>>(), ["daemon", "--idle-exit"]);
        assert_eq!(
            cmd.get_envs().find(|(k, _)| *k == "PATH").and_then(|(_, v)| v),
            Some(path.as_os_str())
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_brindle_without_the_daemon_command_reads_as_an_engine_too_old() {
        use std::os::unix::fs::PermissionsExt;

        // brindle 0.4.1, as click answers a command it doesn't have.
        let dir = std::env::current_dir().unwrap().join("target/engine-too-old");
        let home = dir.join("home");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&home).unwrap();
        let bin = dir.join("brindle");
        std::fs::write(
            &bin,
            "#!/bin/sh\n\
             if [ \"$1\" = --version ]; then echo 'brindle, version 0.4.1'; exit 0; fi\n\
             echo \"Usage: brindle [OPTIONS] COMMAND [ARGS]...\" >&2\n\
             echo \"Try 'brindle --help' for help.\" >&2\n\
             echo >&2\n\
             echo \"Error: No such command 'daemon'.\" >&2\n\
             exit 2\n",
        )
        .unwrap();
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
        let path = std::env::var_os("PATH").unwrap_or_default();

        let err = start_in(&bin, &path, &home, None, START_TIMEOUT).unwrap_err();
        assert_eq!(
            err,
            "engine too old: brindle 0.4.1 has no `daemon` command. Upgrade it: uv tool upgrade brindle"
        );
        assert!(err.starts_with(ENGINE_TOO_OLD));
        // Any other early exit is still reported as it was, with the log's tail.
        assert!(!lacks_daemon("Error: address already in use"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// A brindle whose `daemon` takes `delay` seconds to start (an engine's
    /// first start: Gatekeeper's first look, the first imports), then listens on
    /// `target/<name>.sock` (its daemon.json says so), or exits 1 when `listens`
    /// is false. Each start it makes is a line in `dir/starts`.
    #[cfg(unix)]
    fn slow_engine(dir: &Path, name: &str, delay: &str, listens: bool) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::remove_dir_all(dir);
        std::fs::create_dir_all(dir.join("home")).unwrap();
        let sock = format!("target/{name}.sock");
        let _ = std::fs::remove_file(&sock);
        let dir_text = dir.display();
        let then = if listens {
            format!(
                "printf '{{\"socket\": \"{sock}\", \"token\": \"t\", \"pid\": %s}}' $$ > '{dir_text}/home/daemon.json'\n\
                 /usr/bin/nc -lkU {sock} & wait\n"
            )
        } else {
            "echo 'Traceback: the engine crashed' >&2; exit 1\n".to_string()
        };
        let bin = dir.join("brindle");
        std::fs::write(
            &bin,
            format!(
                "#!/bin/bash\n\
                 echo $$ >> '{dir_text}/starts'\n\
                 sleep {delay}\n\
                 {then}"
            ),
        )
        .unwrap();
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
        bin
    }

    #[cfg(unix)]
    #[test]
    fn a_slow_first_start_is_waited_out_and_only_an_exit_fails() {
        let dir = std::env::current_dir().unwrap().join("target/slow-engine");
        let home = dir.join("home");
        let bin = slow_engine(&dir, "slow-engine", "1.5", true);
        let path = std::env::var_os("PATH").unwrap_or_default();
        let patience = Duration::from_millis(300);

        // Past the start's patience the engine is still starting: said so, not a failure.
        let err = start_in(&bin, &path, &home, None, patience).unwrap_err();
        assert!(err.starts_with(ENGINE_STARTING), "{err}");
        // However long it has been starting, that alone is no failure: as if started
        // over five minutes ago, it is still waited on, never stopped.
        let long_ago = Instant::now().checked_sub(Duration::from_secs(301)).expect("up for five minutes");
        STARTING.lock().unwrap().as_mut().unwrap().get_mut(&home).unwrap().since = long_ago;
        let err = start_in(&bin, &path, &home, None, patience).unwrap_err();
        assert!(err.starts_with(ENGINE_STARTING) && err.contains("starting for 30"), "{err}");
        // Asked again (the webview does), the same daemon is waited on: none is started
        // over it, which would find its lock and exit.
        let begun = Instant::now();
        let info = loop {
            match ensure_in(&home, None, true, |_, _| panic!("judged"), || start_in(&bin, &path, &home, None, patience)) {
                Ok(info) => break info,
                Err(err) => assert!(err.starts_with(ENGINE_STARTING), "{err}"),
            }
            assert!(begun.elapsed() < Duration::from_secs(10), "never started");
        };
        let starts = std::fs::read_to_string(dir.join("starts")).unwrap();
        assert_eq!(starts.lines().count(), 1, "started more than once: {starts}");
        assert_eq!(info.pid.to_string(), starts.trim());
        assert!(Conn::open(&info).is_ok());
        assert!(!starting_in(&home));
        // SAFETY: kill(2) only sends a signal, to the fake daemon's own process group.
        unsafe { libc::kill(-(info.pid as libc::pid_t), libc::SIGKILL) };

        // An engine that crashes while starting is a failure, with what it said.
        let dir = std::env::current_dir().unwrap().join("target/crashing-engine");
        let home = dir.join("home");
        let bin = slow_engine(&dir, "crashing-engine", "0.8", false);
        let err = start_in(&bin, &path, &home, None, patience).unwrap_err();
        assert!(err.starts_with(ENGINE_STARTING), "{err}");
        let err = loop {
            let err = start_in(&bin, &path, &home, None, patience).unwrap_err();
            if !err.starts_with(ENGINE_STARTING) {
                break err;
            }
        };
        assert!(err.starts_with("`brindle daemon` exited") && err.contains("the engine crashed"), "{err}");
        assert!(!starting_in(&home));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn an_app_with_an_engine_uses_only_its_own_builds_daemon() {
        let engine = Path::new("/Applications/brindle.app/Contents/Resources/engine");
        let py = "/Applications/brindle.app/Contents/Resources/engine/python/bin/python3 -I -B";
        let build = Ok("00b2");

        // This engine's, of this build, started by the app or through install_cli's link: used.
        let own = format!("{py} -X brindle-build=00b2 -m brindle daemon --idle-exit");
        assert_eq!(reuse(engine, build, &own), Reuse::Use);
        assert_eq!(reuse(engine, build, &format!("{py} -X brindle-build=00b2 -m brindle daemon")), Reuse::Use);
        // At the same path but of another build: from before an update, whether it
        // replaced the files or renamed a bundle staged (earlier or later) into place.
        let old = format!("{py} -X brindle-build=00b1 -m brindle daemon --idle-exit");
        assert_eq!(reuse(engine, build, &old), Reuse::Replace);
        // From before engines named their build.
        assert_eq!(reuse(engine, build, &format!("{py} -m brindle daemon --idle-exit")), Reuse::Replace);
        // A build id elsewhere on the line isn't the option.
        assert_eq!(reuse(engine, build, &format!("{py} -m brindle daemon brindle-build=00b2")), Reuse::Replace);

        // uv's, started by an app (an older one, before it carried an engine): replaced.
        let uv = "/home/me/.local/share/uv/tools/brindle/bin/python -P -m brindle daemon --idle-exit";
        assert_eq!(reuse(engine, build, uv), Reuse::Replace);
        // A bundle staged elsewhere, run by an app from there: not at this path, replaced.
        let staged = "/private/var/tmp/up/brindle.app/Contents/Resources/engine/python/bin/python3 -I -B \
                      -X brindle-build=00b2 -m brindle daemon --idle-exit";
        assert_eq!(reuse(engine, build, staged), Reuse::Replace);
        // uv's, started by a person: left alone, and the app says so.
        let Reuse::Conflict(why) = reuse(engine, build, "/home/me/.local/bin/brindle daemon") else {
            panic!("a person's daemon is replaced");
        };
        assert!(why.starts_with(ENGINE_CONFLICT), "{why}");
        assert!(why.contains("/home/me/.local/bin/brindle") && why.contains("brindle daemon stop"), "{why}");

        // What can't be established is never used, nor stopped: no command line
        // (ps couldn't say), or no build id for this engine.
        assert!(matches!(reuse(engine, build, ""), Reuse::Conflict(_)));
        // Why it is unknown (require_build's: no BUILD, or one unreadable now) is what the app says.
        let missing = "this brindle app's engine is incomplete (no …/engine/BUILD)";
        assert_eq!(reuse(engine, Err(missing), &own), Reuse::Conflict(missing.into()));
    }

    /// A process `ps` shows as `argv0 … args` (a daemon's command line), and
    /// brindle's home `home` whose daemon.json names it. It exits on SIGTERM.
    #[cfg(unix)]
    fn daemon_process(home: &Path, argv0: &str, args: &[&str]) -> std::process::Child {
        use std::os::unix::process::CommandExt;
        let child = Command::new("/bin/bash")
            .arg0(argv0)
            .args(["-c", "sleep 60 & p=$!; trap 'kill $p; exit 0' TERM; wait $p", "daemon"])
            .args(args)
            .spawn()
            .unwrap();
        let _ = std::fs::remove_dir_all(home);
        std::fs::create_dir_all(home).unwrap();
        std::fs::write(
            home.join("daemon.json"),
            format!(r#"{{"socket": "/nonexistent.sock", "token": "t", "pid": {}}}"#, child.id()),
        )
        .unwrap();
        // ps sees it once it runs.
        let deadline = Instant::now() + Duration::from_secs(5);
        while !is_daemon(child.id()) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(is_daemon(child.id()), "ps doesn't show {argv0} as a daemon");
        child
    }

    /// Unloads the daemon job `label` when dropped: a test's fake daemon
    /// doesn't stay under launchd, however the test ends.
    #[cfg(target_os = "macos")]
    struct Unload(String);

    #[cfg(target_os = "macos")]
    impl Drop for Unload {
        fn drop(&mut self) {
            let _ = launchctl(&["bootout", &launchd_service(&self.0)]);
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn a_live_predecessor_daemon_does_not_hold_up_relaunch() {
        // launchd reports a daemon that remains alive until its input closes.
        // Only the job lookup is faked: the process and kqueue wait are real.
        let mut cmd = Command::new("/bin/cat");
        let mut daemon = cmd.stdin(Stdio::piped()).stdout(Stdio::null()).spawn().unwrap();
        let pid = daemon.id();
        // Concurrent test binaries must not overwrite each other's pause marker.
        let home = std::env::current_dir().unwrap().join(format!("target/predecessor-daemon-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&home);
        std::fs::create_dir_all(&home).unwrap();
        let mut elapsed = Vec::new();
        let mut usable = Vec::new();
        // No marker, a stale marker for another PID, and this daemon pausing.
        for marker in [None, Some(0), Some(pid)] {
            if let Some(marker) = marker {
                std::fs::write(home.join(PAUSING_FILE), marker.to_string()).unwrap();
            }
            let began = Instant::now();
            wait_for_predecessor_job(&home, "predecessor", |_| Ok(Some(LaunchdJob { pid: Some(pid), runs: 1, exit: String::new() })));
            elapsed.push(began.elapsed());
            usable.push(ready_in(&home, engines_own(pid)).is_ok());
        }
        let alive = daemon.try_wait().unwrap().is_none();
        // Once its pause completes, let it exit normally. Another SIGTERM
        // from stop_daemon_while would kill it during cleanup instead.
        stop_daemon_while(pid, STOP_GRACE, || {
            drop(daemon.stdin.take());
            daemon.try_wait().unwrap().is_none()
        }, || pausing(&home, pid)).unwrap();
        assert!(daemon.wait().unwrap().success(), "interrupted an existing pause");
        assert!(alive, "the predecessor daemon must remain available for adoption");
        assert_eq!(usable, [true, true, false], "a start handed back a daemon still pausing");
        assert!(elapsed[..2].iter().all(|elapsed| *elapsed < STOP_GRACE), "live daemon delayed relaunch by {elapsed:?}");
        assert!(elapsed[2] >= STOP_GRACE && elapsed[2] < STOP_GRACE + STOP_GRACE, "pause wait was not bounded: {elapsed:?}");
        eprintln!("predecessor waits (healthy, stale marker, pausing): {elapsed:?}");
        std::fs::remove_dir_all(home).unwrap();
    }

    /// A daemon loaded under the home's label by an earlier start (another
    /// app process's) is adopted and waited on, not started over; once it
    /// listens it is judged as a daemon found running is, and another
    /// build's is stopped and replaced by this start's own.
    #[cfg(target_os = "macos")]
    #[test]
    fn an_adopted_jobs_daemon_of_another_build_is_replaced_once_it_listens() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::current_dir().unwrap().join("target/adopted-daemon");
        let home = dir.join("home");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&home).unwrap();
        let engine = engine_of_build(&dir, Some("00b2"));
        // A daemon that, once `gate` (a fifo) is written to, records its pid, writes
        // the home's daemon.json and listens on its socket. It exits on SIGTERM.
        let fake = |name: &str, gate: Option<&Path>| -> PathBuf {
            let bin = dir.join(name).join("brindle");
            std::fs::create_dir_all(bin.parent().unwrap()).unwrap();
            let sock = format!("target/adopted-{name}.sock");
            let _ = std::fs::remove_file(&sock);
            let wait = gate.map_or(String::new(), |gate| format!("read < '{}'\n", gate.display()));
            std::fs::write(
                &bin,
                format!(
                    "#!/bin/bash\n{wait}echo $$ > '{dir}/{name}.started'\n\
                     printf '{{\"socket\": \"{sock}\", \"token\": \"t\", \"pid\": %s}}' $$ > '{home}/daemon.json'\n\
                     /usr/bin/nc -lkU {sock} & trap 'kill $!; exit 0' TERM; wait\n",
                    dir = dir.display(),
                    home = home.display()
                ),
            )
            .unwrap();
            std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
            bin
        };
        let gate = dir.join("gate");
        let c_gate = std::ffi::CString::new(gate.to_str().unwrap()).unwrap();
        // SAFETY: mkfifo only reads the path it is given.
        assert_eq!(unsafe { libc::mkfifo(c_gate.as_ptr(), 0o600) }, 0);
        let old = fake("old", Some(&gate));
        let new = fake("new", None);
        let path = std::env::var_os("PATH").unwrap_or_default();
        let log = home.join(START_LOG);
        File::create(&log).unwrap();
        let patience = Duration::from_millis(300);

        // An earlier app process loaded the old build's daemon; it doesn't listen yet.
        let earlier = start_daemon(daemon_command(&old, &path), &home, &log).unwrap();
        let _unload = Unload(launchd_label(&home, std::process::id()));
        assert!(!earlier.adopted());
        // This start finds launchd's job and waits on it: nothing is started over it.
        let err = start_in(&new, &path, &home, Some(&engine), patience).unwrap_err();
        assert!(err.starts_with(ENGINE_STARTING), "{err}");
        assert!(!dir.join("new.started").exists(), "started a daemon over the loaded job");
        assert!(STARTING.lock().unwrap().as_ref().unwrap()[&home].child.adopted());

        // The old one publishes its daemon.json and listens. It isn't this engine's build
        // (no brindle-build on its command line; `--idle-exit`: Replace), so it is stopped
        // and this start's own daemon takes its place.
        std::fs::write(&gate, "go\n").unwrap();
        let deadline = Instant::now() + Duration::from_secs(10);
        let info = loop {
            match start_in(&new, &path, &home, Some(&engine), patience) {
                Ok(info) => break info,
                Err(err) => assert!(err.starts_with(ENGINE_STARTING), "{err}"),
            }
            assert!(Instant::now() < deadline, "never replaced");
        };
        let started = |name: &str| std::fs::read_to_string(dir.join(format!("{name}.started"))).unwrap().trim().parse::<u32>().unwrap();
        let (old_pid, new_pid) = (started("old"), started("new"));
        assert_eq!(info.pid, new_pid);
        assert_ne!(new_pid, old_pid);
        assert!(!is_daemon(old_pid), "the other build's daemon still runs");
        assert!(Conn::open(&info).is_ok());
        assert!(!starting_in(&home));
        // SAFETY: kill(2) only sends a signal, to the fake daemon's own process group (launchd's job leads one).
        unsafe { libc::kill(-(new_pid as libc::pid_t), libc::SIGKILL) };
    }

    /// The jetsam coalition `pid` is in: what `lsappinfo` lists as an app's
    /// "coalition", and whose members LaunchServices counts as the app's subordinates.
    #[cfg(target_os = "macos")]
    fn coalition_of(pid: u32) -> u64 {
        #[repr(C)]
        struct Coalitions {
            ids: [u64; 2],
            reserved: [u64; 3],
        }
        const PROC_PIDCOALITIONINFO: libc::c_int = 20;
        let mut info = Coalitions { ids: [0; 2], reserved: [0; 3] };
        let size = std::mem::size_of::<Coalitions>() as libc::c_int;
        // SAFETY: proc_pidinfo writes at most `size` bytes into `info`, which is that big.
        let n = unsafe { libc::proc_pidinfo(pid as libc::c_int, PROC_PIDCOALITIONINFO, 0, (&mut info as *mut Coalitions).cast(), size) };
        assert!(n > 0, "proc_pidinfo({pid}) failed");
        info.ids[1]
    }

    /// The app's daemon on macOS is launchd's job, in a coalition of its own
    /// rather than the app's (which LaunchServices would keep as the app's
    /// subordinates, kept and killed with the app's Dock icon), started with
    /// the command's PATH, and seen through to its end: unloaded once it exits.
    #[cfg(target_os = "macos")]
    #[test]
    fn the_daemon_is_launchds_job_outside_the_apps_coalition() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::current_dir().unwrap().join("target/launchd-daemon");
        let home = dir.join("home");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&home).unwrap();
        let bin = dir.join("brindle");
        // A daemon that records its PATH and stays until SIGTERM, as the engine's does.
        std::fs::write(
            &bin,
            format!("#!/bin/bash\necho \"$PATH\" > '{}/path'\ntail -f /dev/null & trap 'kill $!; exit 0' TERM; wait\n", dir.display()),
        )
        .unwrap();
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
        let path = OsString::from("/usr/bin:/bin:/the/apps/tools");
        let log = home.join(START_LOG);
        File::create(&log).unwrap();

        // Nothing loaded: this start's job is loaded. A loaded job is adopted while it runs;
        // one loaded and idle (at login, or its last run over) is reloaded and run.
        assert_eq!(launchd_plan(None), Plan::Load);
        assert_eq!(launchd_plan(Some(&LaunchdJob { pid: Some(7), runs: 1, exit: String::new() })), Plan::Adopt(7));
        assert_eq!(launchd_plan(Some(&LaunchdJob { pid: None, runs: 0, exit: String::new() })), Plan::Reload);
        assert_eq!(launchd_plan(Some(&LaunchdJob { pid: None, runs: 1, exit: String::new() })), Plan::Reload);
        // Switched off under Allow in the Background, as launchctl print-disabled lists it.
        let listing = "gui/501 = {\n\t\t\"com.apple.foo\" => enabled\n\t\t\"the.label\" => disabled\n\t}\n";
        assert!(disabled_in(listing, "the.label"));
        assert!(!disabled_in(listing, "com.apple.foo") && !disabled_in(listing, "the"));

        let mut daemon = start_daemon(daemon_command(&bin, &path), &home, &log).unwrap();
        let Daemon::Launchd { label, adopted: false, .. } = &daemon else { panic!("not launchd's job, or adopted") };
        let label = label.clone();
        let _unload = Unload(label.clone());
        let deadline = Instant::now() + Duration::from_secs(5);
        // launchd may spawn the job a moment after loading it: its pid comes with that.
        while daemon.id() == 0 {
            assert!(matches!(daemon.try_wait(), Ok(None)), "ended before it ran");
            assert!(Instant::now() < deadline, "launchd didn't spawn {}", bin.display());
        }
        let pid = daemon.id();
        assert!(daemon.owns(pid) && !daemon.adopted());
        let recorded = loop {
            let recorded = std::fs::read_to_string(dir.join("path")).unwrap_or_default();
            if is_daemon(pid) && recorded.ends_with('\n') {
                break recorded;
            }
            assert!(Instant::now() < deadline, "launchd didn't run {}", bin.display());
        };
        assert_eq!(recorded.trim(), path.to_str().unwrap());
        // The job's plist carries the app's environment: the person's own to read. It runs
        // only when kickstarted, never at load.
        let plist = std::fs::read_to_string(home.join(LAUNCHD_PLIST)).unwrap();
        assert_eq!(std::fs::metadata(home.join(LAUNCHD_PLIST)).unwrap().permissions().mode() & 0o777, 0o600);
        assert!(plist.contains("<key>RunAtLoad</key>\n\t<false/>"), "{plist}");
        assert!(!launchctl(&["print", &launchd_service(&label)]).unwrap().contains("keepalive"));
        // Its own coalition, not this process's (the app's, when the app starts it): what
        // the pid's parent being launchd (ppid 1) alone doesn't give a child of the app's.
        assert_ne!(coalition_of(pid), coalition_of(std::process::id()), "in the app's coalition");
        assert!(matches!(daemon.try_wait(), Ok(None)));

        // Stopped as an old build's daemon is (stop_daemon): seen as ended, and unloaded.
        stop_daemon(&home, pid, Duration::from_secs(5)).unwrap();
        let ended = loop {
            if let Some(how) = daemon.try_wait().unwrap() {
                break how;
            }
            assert!(Instant::now() < deadline, "launchd still shows the daemon running");
        };
        assert_eq!(ended, "last exit code = 0");
        assert!(launchd_job(&label).unwrap().is_none(), "still loaded");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// With launchd's job switched off, the daemon is the app's own child: told
    /// the app's pid (to stop itself after a force quit), and stopped with SIGTERM,
    /// which it shuts down gracefully on, when the app exits. A daemon serving the
    /// home that this app didn't start that way is left alone.
    #[cfg(target_os = "macos")]
    #[test]
    fn the_apps_own_child_daemon_knows_the_app_and_stops_gracefully_when_it_exits() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::current_dir().unwrap().join("target/app-child-daemon");
        let home = dir.join("home");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&home).unwrap();
        // Not the app's: another's daemon serving the home is never stopped here.
        let mut foreign = daemon_process(&home, "brindle", &[]);
        stop_app_daemon_in(&home);
        assert!(is_daemon(foreign.id()), "stopped a daemon the app didn't start");
        foreign.kill().unwrap();
        foreign.wait().unwrap();

        // A daemon that records the app pid it was told, publishes its daemon.json and
        // records a graceful stop on SIGTERM.
        let bin = dir.join("brindle");
        std::fs::write(
            &bin,
            format!(
                "#!/bin/bash\necho \"${APP_PID_ENV}\" > '{dir}/app'\n\
                 printf '{{\"socket\": \"/nonexistent.sock\", \"token\": \"t\", \"pid\": %s}}' $$ > '{home}/daemon.json'\n\
                 tail -f /dev/null & p=$!; trap 'kill $p; echo stopped > \"{dir}/graceful\"; exit 0' TERM; wait $p\n",
                dir = dir.display(),
                home = home.display()
            ),
        )
        .unwrap();
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
        let log = home.join(START_LOG);
        File::create(&log).unwrap();
        let path = std::env::var_os("PATH").unwrap_or_default();
        let mut daemon = start_app_child(daemon_command(&bin, &path), &log).unwrap();
        let pid = daemon.id();
        let deadline = Instant::now() + Duration::from_secs(5);
        while running_in(&home).map(|info| info.pid) != Some(pid) {
            assert!(Instant::now() < deadline, "the child daemon never published its daemon.json");
        }
        assert_eq!(std::fs::read_to_string(dir.join("app")).unwrap().trim(), std::process::id().to_string());

        stop_app_daemon_in(&home);
        assert!(!is_daemon(pid), "still running after the app exited");
        assert!(matches!(daemon.try_wait(), Ok(Some(_))));
        assert!(dir.join("graceful").exists(), "killed, not stopped gracefully");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// `root/brindle.app`'s engine, of build `build` (none when None).
    #[cfg(unix)]
    fn engine_of_build(root: &Path, build: Option<&str>) -> PathBuf {
        let engine = root.join("brindle.app/Contents/Resources/engine");
        std::fs::create_dir_all(engine.join("bin")).unwrap();
        std::fs::write(engine.join("bin/brindle"), "#!/bin/sh\n").unwrap();
        if let Some(build) = build {
            std::fs::write(engine.join("BUILD"), format!("{build}\n")).unwrap();
        }
        engine
    }

    #[cfg(unix)]
    fn engines_own(pid: u32) -> DaemonInfo {
        DaemonInfo { socket: Some("/the/engines.sock".into()), port: None, token: "new".into(), version: String::new(), pid }
    }

    #[cfg(unix)]
    #[test]
    fn an_update_by_renaming_a_staged_bundle_replaces_the_old_builds_daemon() {
        let dir = std::env::current_dir().unwrap().join("target/daemon-update");
        let _ = std::fs::remove_dir_all(&dir);
        // The new build is staged first, before the old one's daemon starts.
        let staged = engine_of_build(&dir.join("staged"), Some("00b2"));
        let installed = engine_of_build(&dir.join("Applications"), Some("00b1"));
        let python = format!("{}/python/bin/python3", installed.display());
        let home = dir.join("home");
        let mut old = daemon_process(&home, &python, &["-I", "-B", "-X", "brindle-build=00b1", "-m", "brindle", "daemon", "--idle-exit"]);

        // While the old build is installed, its daemon is the app's.
        let got = ensure_in(&home, Some(&installed), true, reuse_running, || panic!("started")).unwrap();
        assert_eq!(got.pid, old.id());

        // The update swaps the staged bundle in by renames: same path, files as staged.
        let app = dir.join("Applications/brindle.app");
        std::fs::rename(&app, dir.join("old.app")).unwrap();
        std::fs::rename(staged.parent().unwrap().parent().unwrap().parent().unwrap(), &app).unwrap();
        assert_eq!(crate::engine::require_build(&installed).as_deref(), Ok("00b2"));
        let old_pid = old.id();
        let start = || {
            // Meanwhile "Starting brindle…" says the old daemon was being stopped.
            assert!(matches!(step_in(&home), Some((StartStep::Stopping { pid }, _)) if pid == old_pid));
            Ok(engines_own(7))
        };
        let got = ensure_in(&home, Some(&installed), false, reuse_running, start).unwrap();
        assert_eq!(got, engines_own(7));
        assert!(old.wait().unwrap().success(), "stopped by SIGTERM");

        // This build's daemon is already pausing and completes that pause as
        // the app judges its build. It must be replaced, not adopted mid-exit.
        use std::os::unix::process::CommandExt;
        let mut pausing_daemon = Command::new("/bin/bash")
            .arg0(&python)
            .args(["-c", "read", "daemon", "-X", "brindle-build=00b2", "brindle", "daemon"])
            .stdin(Stdio::piped()).spawn().unwrap();
        let pid = pausing_daemon.id();
        std::fs::write(home.join("daemon.json"), format!(r#"{{"socket":"/unused.sock","token":"t","pid":{pid}}}"#)).unwrap();
        std::fs::write(home.join(PAUSING_FILE), pid.to_string()).unwrap();
        let judge = |engine: &Path, info: &DaemonInfo| {
            let reuse = reuse_running(engine, info);
            // A completed pause releases the process; no second TERM is needed.
            drop(pausing_daemon.stdin.take());
            reuse
        };
        let got = ensure_in(&home, Some(&installed), false, judge, || {
            assert!(!is_daemon(pid), "started a second daemon before the first exited");
            Ok(engines_own(7))
        }).unwrap();
        pausing_daemon.wait().unwrap();
        assert_eq!(got, engines_own(7));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn a_daemon_still_busy_past_its_grace_is_killed_unless_it_says_it_is_pausing() {
        use std::io::BufRead;
        use std::os::unix::process::{CommandExt, ExitStatusExt};
        // An older daemon finishing what an app that quit for its update asked of it
        // (a provider's sign-in probe) stays past SIGTERM: this one ignores it outright.
        let busy_daemon = || {
            let mut busy = Command::new("/bin/bash")
                .arg0("/opt/uv/tools/brindle/bin/python")
                .args(["-c", "trap '' TERM; echo ready; read", "daemon", "-m", "brindle", "daemon", "--idle-exit"])
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .spawn()
                .unwrap();
            let mut ready = String::new();
            io::BufReader::new(busy.stdout.take().unwrap()).read_line(&mut ready).unwrap();
            assert!(is_daemon(busy.id()), "ps doesn't show it as a daemon");
            busy
        };
        let home = std::env::current_dir().unwrap().join("target/daemon-pausing");
        let _ = std::fs::remove_dir_all(&home);
        std::fs::create_dir_all(&home).unwrap();
        let mut busy = busy_daemon();
        stop_daemon(&home, busy.id(), Duration::ZERO).unwrap();
        assert_eq!(busy.wait().unwrap().signal(), Some(libc::SIGKILL));

        // The app's daemon pausing its sessions on its way out says so (lifecycle.say_pausing):
        // it is waited on past the short grace while it does, then gets the short grace.
        let mut busy = busy_daemon();
        let pid = busy.id();
        std::fs::write(home.join(PAUSING_FILE), format!("{pid}\n")).unwrap();
        let looks = std::cell::Cell::new(0);
        let running = || {
            looks.set(looks.get() + 1);
            if looks.get() == 3 {
                std::fs::remove_file(home.join(PAUSING_FILE)).unwrap();   // its pause is over
            }
            is_daemon(pid)
        };
        stop_daemon_while(pid, Duration::ZERO, running, || pausing(&home, pid)).unwrap();
        assert_eq!(busy.wait().unwrap().signal(), Some(libc::SIGKILL));
        assert_eq!(looks.get(), 4, "killed while it said it was pausing, or not once it stopped");
        let _ = std::fs::remove_dir_all(&home);
    }

    #[cfg(unix)]
    #[test]
    fn another_installs_daemon_is_replaced_if_an_app_started_it_and_else_left_running() {
        let dir = std::env::current_dir().unwrap().join("target/daemon-reuse");
        let _ = std::fs::remove_dir_all(&dir);
        let engine = engine_of_build(&dir, Some("00b2"));
        let uv = "/opt/uv/tools/brindle/bin/python";

        // An older app's uv daemon (--idle-exit): stopped, then the engine's started.
        let home = dir.join("home-app");
        let mut old = daemon_process(&home, uv, &["-P", "-m", "brindle", "daemon", "--idle-exit"]);
        let got = ensure_in(&home, Some(&engine), false, reuse_running, || Ok(engines_own(7))).unwrap();
        assert_eq!(got, engines_own(7));
        assert!(old.wait().unwrap().success(), "stopped by SIGTERM");

        // A person's own `brindle daemon`: left running, and no engine started over it.
        let home = dir.join("home-person");
        let mut theirs = daemon_process(&home, uv, &["-P", "-m", "brindle", "daemon"]);
        let err = ensure_in(&home, Some(&engine), false, reuse_running, || panic!("started over it")).unwrap_err();
        assert!(err.starts_with(ENGINE_CONFLICT), "{err}");
        assert!(is_daemon(theirs.id()), "it still runs");

        // A release missing its engine uses no daemon, not even one that runs.
        let err = ensure_in(&home, None, false, |_, _| panic!("judged"), || panic!("started")).unwrap_err();
        assert_eq!(err, crate::engine::MISSING_ENGINE);
        // Nor does one whose engine has no build id.
        let bare = engine_of_build(&dir.join("bare"), None);
        let err = ensure_in(&home, Some(&bare), false, |_, _| panic!("judged"), || panic!("started")).unwrap_err();
        assert!(err.contains("incomplete"), "{err}");
        assert!(is_daemon(theirs.id()), "it still runs");
        // A dev build (or Linux's), which may take brindle from PATH, uses whatever runs.
        let got = ensure_in(&home, None, true, |_, _| panic!("judged"), || panic!("started")).unwrap();
        assert_eq!(got.pid, theirs.id());

        theirs.kill().unwrap();
        theirs.wait().unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// A daemon on a Unix socket that answers `GET /v1/meta` with `body`
    /// (status 200) on every connection; `connections` bounds it.
    #[cfg(unix)]
    fn http_daemon(name: &str, connections: usize, body: &'static str) -> PathBuf {
        use std::os::unix::net::UnixListener;
        let sock = PathBuf::from(format!("target/{name}.sock"));
        let _ = std::fs::remove_file(&sock);
        let listener = UnixListener::bind(&sock).unwrap();
        std::thread::spawn(move || {
            for stream in listener.incoming().take(connections) {
                let mut stream = stream.unwrap();
                read_head(&mut stream);
                let answer = format!("HTTP/1.1 200 OK\r\ncontent-length: {}\r\n\r\n{body}", body.len());
                let _ = stream.write_all(answer.as_bytes());
                let _ = stream.shutdown(Shutdown::Both);
            }
        });
        sock
    }

    /// Read a request up to the end of its head, so the client's write isn't refused.
    #[cfg(unix)]
    fn read_head(stream: &mut std::os::unix::net::UnixStream) {
        let mut head = Vec::new();
        let mut byte = [0u8; 1];
        while !head.ends_with(b"\r\n\r\n") && stream.read(&mut byte).is_ok_and(|n| n == 1) {
            head.push(byte[0]);
        }
    }

    /// A daemon that dies under each request: it reads the request, then
    /// closes without a byte of answer; after `connections` it is gone.
    #[cfg(unix)]
    fn dying_daemon(name: &str, connections: usize) -> PathBuf {
        use std::os::unix::net::UnixListener;
        let sock = PathBuf::from(format!("target/{name}.sock"));
        let _ = std::fs::remove_file(&sock);
        let listener = UnixListener::bind(&sock).unwrap();
        let path = sock.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming().take(connections) {
                let mut stream = stream.unwrap();
                read_head(&mut stream);
                drop(stream);
            }
            // Dead for good: nothing listens on its socket any more.
            drop(listener);
            let _ = std::fs::remove_file(&path);
        });
        sock
    }

    /// An engine whose daemon.json names `first` now and `then` once the
    /// first is found gone (as `ensure` starts a new daemon and reads its
    /// file); counts how often the daemon was looked for.
    #[cfg(unix)]
    fn engine_over(first: DaemonInfo, then: DaemonInfo) -> (Engine, Arc<AtomicU32>) {
        let looked = Arc::new(AtomicU32::new(0));
        let count = looked.clone();
        let engine = Engine {
            discover: Arc::new(move || {
                Ok(if count.fetch_add(1, Ordering::SeqCst) == 0 { first.clone() } else { then.clone() })
            }),
            ..Engine::default()
        };
        (engine, looked)
    }

    #[cfg(unix)]
    #[test]
    fn self_repair_a_request_to_a_daemon_that_died_reaches_the_one_started_in_its_place() {
        // The new daemon, as `ensure` would start it: answers meta.
        let fresh = http_daemon("self-repair-new", 3, r#"{"pid": 2}"#);
        let new = daemon_info(&fresh, "new");

        // Gone before the request: the connection is refused, the daemon is looked for (and
        // started) again, and the request goes to the new one.
        let gone = PathBuf::from("target/self-repair-gone.sock");
        let _ = std::fs::remove_file(&gone);
        let (engine, looked) = engine_over(daemon_info(&gone, "old"), new.clone());
        let reply = engine.request("GET", "/v1/meta", None).unwrap();
        assert_eq!(reply, Reply { status: 200, body: r#"{"pid": 2}"#.into() });
        assert_eq!(looked.load(Ordering::SeqCst), 2);
        // Found once, the new daemon is the one asked from then on.
        assert_eq!(engine.request("GET", "/v1/meta", None).unwrap().status, 200);
        assert_eq!(looked.load(Ordering::SeqCst), 2);

        // Dies under the request (accepted, then closed without an answer): a GET is asked
        // again of the daemon started in its place, and the webview sees one answer.
        let dying = dying_daemon("self-repair-dying", 2);
        let (engine, looked) = engine_over(daemon_info(&dying, "old"), new.clone());
        let reply = engine.request("GET", "/v1/meta", None).unwrap();
        assert_eq!(reply.status, 200);
        assert_eq!(looked.load(Ordering::SeqCst), 2);

        // A POST the daemon died under may have been acted on: reported, never sent again.
        let (engine, looked) = engine_over(daemon_info(&dying, "old"), new);
        let err = engine.request("POST", "/v1/agents/w1/kill", Some("{}")).unwrap_err();
        assert!(err.starts_with("the brindle daemon:"), "{err}");
        assert_eq!(looked.load(Ordering::SeqCst), 1);

        // What a daemon's death looks like to a request, and what doesn't count as one.
        assert!(died(&io::Error::new(io::ErrorKind::UnexpectedEof, "")));
        assert!(died(&io::Error::new(io::ErrorKind::ConnectionReset, "")));
        assert!(died(&io::Error::new(io::ErrorKind::BrokenPipe, "")));
        assert!(!died(&io::Error::new(io::ErrorKind::TimedOut, "")));
        assert!(!died(&io::Error::new(io::ErrorKind::InvalidData, "")));
        let _ = std::fs::remove_file(&fresh);
    }

    /// Held by each test that counts this process's sockets, so one's many
    /// streams don't count against another's bound.
    #[cfg(unix)]
    static COUNTING_SOCKETS: Mutex<()> = Mutex::new(());

    /// How many sockets this process holds open now (fstat of each fd in /dev/fd).
    #[cfg(unix)]
    fn open_sockets() -> usize {
        let fds: Vec<i32> = std::fs::read_dir("/dev/fd")
            .unwrap()
            .filter_map(|e| e.ok()?.file_name().to_str()?.parse().ok())
            .collect();
        fds.into_iter()
            .filter(|&fd| {
                // SAFETY: fstat only fills `st`; an fd closed meanwhile just fails.
                let mut st: libc::stat = unsafe { std::mem::zeroed() };
                unsafe { libc::fstat(fd, &mut st) == 0 && st.st_mode & libc::S_IFMT == libc::S_IFSOCK }
            })
            .count()
    }

    /// Whether the sockets open settle back to at most `before` plus a little
    /// slack for the tests running alongside, within a few seconds.
    #[cfg(unix)]
    fn settles_back(before: usize, slack: usize) -> Result<(), usize> {
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            let now = open_sockets();
            if now <= before + slack {
                return Ok(());
            }
            if std::time::Instant::now() > deadline {
                return Err(now);
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    #[cfg(unix)]
    #[test]
    fn requests_and_streams_hold_no_socket_once_done() {
        let _counting = COUNTING_SOCKETS.lock().unwrap_or_else(|e| e.into_inner());
        const N: usize = 200;
        let sock = http_daemon("fd-leak-http", N, r#"{"pid": 1}"#);
        let info = daemon_info(&sock, "tok");
        let engine = Engine {
            discover: Arc::new(move || Ok(info.clone())),
            ..Engine::default()
        };
        // The first request finds the daemon; its own sockets are the baseline.
        assert_eq!(engine.request("GET", "/v1/meta", None).unwrap().status, 200);
        let before = open_sockets();
        for _ in 1..N {
            assert_eq!(engine.request("GET", "/v1/meta", None).unwrap().status, 200);
        }
        if let Err(now) = settles_back(before, N / 10) {
            panic!("{N} requests left {} sockets open", now - before);
        }

        // Streams the webview opens and closes again (a terminal pane, the event
        // stream's reconnects) give their connections back too.
        let ws = fake_daemon("fd-leak-ws", N, |ws| {
            let _ = ws.read();
            let _ = ws.send(Message::Text(r#"{"type":"event"}"#.into()));
        });
        let info = daemon_info(&ws, "tok");
        let engine = Engine {
            discover: Arc::new(move || Ok(info.clone())),
            ..Engine::default()
        };
        let before = open_sockets();
        for _ in 0..N / 4 {
            let (tx, rx) = std::sync::mpsc::channel();
            let id = engine
                .open_stream("/v1/events?since=0", move |frame| {
                    let _ = tx.send(frame);
                })
                .unwrap();
            assert_eq!(rx.recv_timeout(Duration::from_secs(5)).unwrap(), Frame::Open);
            engine.close_stream(id);
            assert!(matches!(collect(&rx).last(), Some(Frame::Close { .. })));
        }
        if let Err(now) = settles_back(before, N / 20) {
            panic!("{} streams left {} sockets open", N / 4, now - before);
        }
        let _ = std::fs::remove_file(&sock);
        let _ = std::fs::remove_file(&ws);
    }

    #[cfg(unix)]
    #[test]
    fn streams_closed_while_the_daemon_stays_silent_give_their_sockets_back() {
        use std::os::unix::net::UnixListener;
        let _counting = COUNTING_SOCKETS.lock().unwrap_or_else(|e| e.into_inner());
        // The real daemon holds every stream open at once and says nothing on a quiet bus:
        // each connection gets a thread that takes the token and then only waits.
        const N: usize = 60;
        let sock = PathBuf::from("target/fd-silent.sock");
        let _ = std::fs::remove_file(&sock);
        let listener = UnixListener::bind(&sock).unwrap();
        std::thread::spawn(move || {
            for stream in listener.incoming().take(2 * N) {
                let stream = stream.unwrap();
                std::thread::spawn(move || {
                    let Ok(mut ws) = tungstenite::accept(stream) else { return };
                    while ws.read().is_ok() {}
                });
            }
        });
        let info = daemon_info(&sock, "tok");
        let engine = Engine {
            discover: Arc::new(move || Ok(info.clone())),
            ..Engine::default()
        };
        let before = open_sockets();
        for i in 0..N {
            let (tx, rx) = std::sync::mpsc::channel();
            let id = engine
                .open_stream("/v1/events?since=0", move |frame| {
                    let _ = tx.send(frame);
                })
                .unwrap();
            // Half are closed open, half mid-handshake (a pane unmounted at once).
            if i % 2 == 0 {
                assert_eq!(rx.recv_timeout(Duration::from_secs(5)).unwrap(), Frame::Open);
            }
            engine.close_stream(id);
            assert!(
                matches!(collect(&rx).last(), Some(Frame::Close { .. })),
                "stream {i} never ended after its close"
            );
        }
        if let Err(now) = settles_back(before, 6) {
            panic!("{N} silent streams closed left {} sockets open", now - before);
        }

        // A page that reloads never closes the streams it opened (the event stream, its
        // panes): the next page's load closes them.
        let frames: Vec<_> = (0..N)
            .map(|_| {
                let (tx, rx) = std::sync::mpsc::channel();
                engine
                    .open_stream("/v1/terminal/w1", move |frame| {
                        let _ = tx.send(frame);
                    })
                    .unwrap();
                assert_eq!(rx.recv_timeout(Duration::from_secs(5)).unwrap(), Frame::Open);
                rx
            })
            .collect();
        assert!(open_sockets() >= before + N, "the streams aren't open");
        engine.close_all_streams();
        for rx in &frames {
            assert!(matches!(collect(rx).last(), Some(Frame::Close { .. })), "a stream outlived its page");
        }
        if let Err(now) = settles_back(before, 6) {
            panic!("{N} streams of a page reloaded left {} sockets open", now - before);
        }
        let _ = std::fs::remove_file(&sock);
    }

    #[cfg(unix)]
    #[test]
    fn a_stream_still_connecting_when_its_page_goes_is_closed_not_registered() {
        use std::os::unix::net::UnixListener;
        use std::sync::mpsc;
        let _counting = COUNTING_SOCKETS.lock().unwrap_or_else(|e| e.into_inner());
        // A daemon that says nothing on its streams, and tells when one is closed.
        let sock = PathBuf::from("target/fd-stale-page.sock");
        let _ = std::fs::remove_file(&sock);
        let listener = UnixListener::bind(&sock).unwrap();
        let (closed_tx, closed) = mpsc::channel();
        std::thread::spawn(move || {
            for stream in listener.incoming().take(3) {
                let (stream, closed_tx) = (stream.unwrap(), closed_tx.clone());
                std::thread::spawn(move || {
                    if let Ok(mut ws) = tungstenite::accept(stream) {
                        while ws.read().is_ok() {}
                    }
                    let _ = closed_tx.send(());
                });
            }
        });
        // Each open stops just before its stream is registered, until let go.
        let (reached_tx, reached) = mpsc::channel();
        let (resume, resume_rx) = mpsc::channel::<()>();
        let (reached_tx, resume_rx) = (Mutex::new(reached_tx), Mutex::new(resume_rx));
        let info = daemon_info(&sock, "tok");
        let engine = Engine {
            discover: Arc::new(move || Ok(info.clone())),
            registering: Arc::new(move || {
                let _ = reached_tx.lock().unwrap().send(());
                let _ = resume_rx.lock().unwrap().recv();
            }),
            ..Engine::default()
        };
        let before = open_sockets();

        // The page's ask is dispatched (daemon_stream_open runs then): the id is the
        // page's at once, while what was put off (finding the daemon, connecting) waits.
        let (tx, rx) = mpsc::channel();
        let id = engine
            .open_stream("/v1/events?since=0", move |frame| {
                let _ = tx.send(frame);
            })
            .unwrap();
        assert!(engine.streams.lock().unwrap().contains_key(&id), "not claimed as it was asked for");
        // Connected, not yet kept, when the webview loads another page; then let go.
        reached.recv_timeout(Duration::from_secs(5)).unwrap();
        engine.close_all_streams();
        resume.send(()).unwrap();
        // Never kept, never run: it ends as a stream that couldn't open, its
        // connection closed on both sides.
        assert_eq!(collect(&rx), [Frame::Close { code: 1006 }]);
        closed.recv_timeout(Duration::from_secs(5)).expect("the daemon's side of it stays open");
        assert!(engine.streams.lock().unwrap().is_empty());
        if let Err(now) = settles_back(before, 2) {
            panic!("a stream of a gone page left {} sockets open", now - before);
        }

        // One the page closes itself while it connects (a pane unmounted at once) goes too.
        let (tx, rx) = mpsc::channel();
        let id = engine
            .open_stream("/v1/terminal/w1", move |frame| {
                let _ = tx.send(frame);
            })
            .unwrap();
        reached.recv_timeout(Duration::from_secs(5)).unwrap();
        engine.close_stream(id);
        resume.send(()).unwrap();
        assert_eq!(collect(&rx), [Frame::Close { code: 1006 }]);
        closed.recv_timeout(Duration::from_secs(5)).expect("the daemon's side of it stays open");
        if let Err(now) = settles_back(before, 2) {
            panic!("a stream closed while it connected left {} sockets open", now - before);
        }

        // The new page's own stream opens as usual.
        resume.send(()).unwrap();
        let (tx, rx) = mpsc::channel();
        let id = engine
            .open_stream("/v1/events?since=0", move |frame| {
                let _ = tx.send(frame);
            })
            .unwrap();
        assert_eq!(rx.recv_timeout(Duration::from_secs(5)).unwrap(), Frame::Open);
        engine.close_stream(id);
        closed.recv_timeout(Duration::from_secs(5)).unwrap();
        let _ = std::fs::remove_file(&sock);
    }

    #[test]
    fn an_answer_is_whole_at_its_length_or_last_chunk() {
        assert!(!answered(b""));
        assert!(!answered(b"HTTP/1.1 200 OK\r\ncontent-length: 4\r\n"));
        assert!(!answered(b"HTTP/1.1 200 OK\r\ncontent-length: 4\r\n\r\n{}"));
        assert!(answered(b"HTTP/1.1 200 OK\r\ncontent-length: 4\r\n\r\n{\"\"}"));
        assert!(!answered(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n2\r\n{}\r\n"));
        assert!(answered(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"));
        // Neither: the body ends with the connection.
        assert!(!answered(b"HTTP/1.1 200 OK\r\n\r\n{}"));
    }

    #[cfg(unix)]
    #[test]
    fn a_request_ends_with_its_answer_even_while_the_daemon_keeps_the_connection_open() {
        use std::os::unix::net::UnixListener;
        let sock = PathBuf::from("target/fd-linger.sock");
        let _ = std::fs::remove_file(&sock);
        let listener = UnixListener::bind(&sock).unwrap();
        std::thread::spawn(move || {
            let mut stream = listener.incoming().next().unwrap().unwrap();
            read_head(&mut stream);
            let _ = stream.write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n{}");
            // Kept open until the app closes its end.
            let _ = stream.read(&mut [0u8; 1]);
        });
        let info = daemon_info(&sock, "tok");
        let engine = Engine {
            discover: Arc::new(move || Ok(info.clone())),
            ..Engine::default()
        };
        let asked = std::time::Instant::now();
        assert_eq!(engine.request("GET", "/v1/meta", None).unwrap(), Reply { status: 200, body: "{}".into() });
        assert!(asked.elapsed() < Duration::from_secs(5), "waited {:?} for the daemon to close", asked.elapsed());
        let _ = std::fs::remove_file(&sock);
    }

    #[cfg(unix)]
    #[test]
    fn a_burst_of_requests_holds_only_a_few_sockets_at_once() {
        use std::os::unix::net::UnixListener;
        let _counting = COUNTING_SOCKETS.lock().unwrap_or_else(|e| e.into_inner());
        // The webview asks many things at once (a reload per replayed event at launch,
        // each a request per repo), of a daemon that takes a while to answer each.
        const N: usize = 200;
        let sock = PathBuf::from("target/fd-burst.sock");
        let _ = std::fs::remove_file(&sock);
        let listener = UnixListener::bind(&sock).unwrap();
        std::thread::spawn(move || {
            for stream in listener.incoming().take(N) {
                let mut stream = stream.unwrap();
                std::thread::spawn(move || {
                    read_head(&mut stream);
                    std::thread::sleep(Duration::from_millis(30));
                    let body = r#"{"pid": 1}"#;
                    let answer = format!("HTTP/1.1 200 OK\r\ncontent-length: {}\r\n\r\n{body}", body.len());
                    let _ = stream.write_all(answer.as_bytes());
                });
            }
        });
        let info = daemon_info(&sock, "tok");
        let engine = Engine {
            discover: Arc::new(move || Ok(info.clone())),
            ..Engine::default()
        };
        let before = open_sockets();
        let done = Arc::new(AtomicBool::new(false));
        let sampler = {
            let done = done.clone();
            std::thread::spawn(move || {
                let mut peak = 0;
                while !done.load(Ordering::SeqCst) {
                    peak = peak.max(open_sockets());
                    std::thread::sleep(Duration::from_millis(2));
                }
                peak
            })
        };
        // As tauri runs each daemon_request: on a blocking thread of its own.
        let requests: Vec<_> = (0..N)
            .map(|_| {
                let engine = engine.clone();
                std::thread::spawn(move || engine.request("GET", "/v1/meta", None).map(|r| r.status))
            })
            .collect();
        for request in requests {
            assert_eq!(request.join().unwrap(), Ok(200));
        }
        done.store(true, Ordering::SeqCst);
        let peak = sampler.join().unwrap();
        // The app's side and the fake daemon's (in this process too) of each connection
        // open at once, and some slack for the tests alongside.
        let most = before + 2 * MAX_CONNECTIONS + 20;
        assert!(peak <= most, "{N} requests at once held {} sockets at the peak (at most {})", peak - before, most - before);
        let _ = std::fs::remove_file(&sock);
    }

    #[test]
    fn encodes_base64_with_padding() {
        assert_eq!(base64(b""), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v");
        assert_eq!(base64(b"\x1bc$ claude\r\n"), "G2MkIGNsYXVkZQ0K");
        assert_eq!(base64(&[0, 255, 254]), "AP/+");
    }

    /// A fake daemon on a Unix socket, for the stream tests: `serve` gets each
    /// accepted WebSocket in turn.
    #[cfg(unix)]
    fn fake_daemon(
        name: &str,
        connections: usize,
        serve: impl Fn(&mut tungstenite::WebSocket<std::os::unix::net::UnixStream>) + Send + 'static,
    ) -> PathBuf {
        use std::os::unix::net::UnixListener;
        // Relative: a socket path must stay short (104 bytes on macOS).
        let sock = PathBuf::from(format!("target/{name}.sock"));
        let _ = std::fs::remove_file(&sock);
        let listener = UnixListener::bind(&sock).unwrap();
        std::thread::spawn(move || {
            for stream in listener.incoming().take(connections) {
                let mut ws = tungstenite::accept(stream.unwrap()).unwrap();
                serve(&mut ws);
                while ws.read().is_ok() {}
            }
        });
        sock
    }

    #[cfg(unix)]
    fn daemon_info(sock: &Path, token: &str) -> DaemonInfo {
        DaemonInfo {
            socket: Some(sock.to_path_buf()),
            port: None,
            token: token.into(),
            version: "0.5.0".into(),
            pid: 1,
        }
    }

    #[cfg(unix)]
    fn collect(rx: &std::sync::mpsc::Receiver<Frame>) -> Vec<Frame> {
        let mut frames = Vec::new();
        while let Ok(frame) = rx.recv_timeout(Duration::from_secs(5)) {
            let done = matches!(frame, Frame::Close { .. });
            frames.push(frame);
            if done {
                break;
            }
        }
        frames
    }

    #[cfg(unix)]
    #[test]
    fn a_terminals_key_frame_reaches_the_daemon_and_its_bytes_come_back() {
        use std::sync::mpsc;

        // The daemon: the token, then a hello; on the first key it answers with the
        // pane's bytes (binary) and closes.
        let sock = fake_daemon("terminal-test", 1, |ws| {
            assert_eq!(ws.read().unwrap().to_text().unwrap(), "tok");
            ws.send(Message::Text(r#"{"type":"terminal","agent":"w1","shared":false}"#.into()))
                .unwrap();
            let key = ws.read().unwrap();
            assert_eq!(key.to_text().unwrap(), r#"{"type":"key","keys":["Enter"]}"#);
            ws.send(Message::Binary(b"\x1bc$ claude\r\n".to_vec().into())).unwrap();
            let _ = ws.close(Some(tungstenite::protocol::CloseFrame {
                code: tungstenite::protocol::frame::coding::CloseCode::Normal,
                reason: "".into(),
            }));
        });
        let info = daemon_info(&sock, "tok");
        let engine = Engine {
            discover: Arc::new(move || Ok(info.clone())),
            ..Engine::default()
        };
        let (tx, rx) = mpsc::channel();
        let id = engine
            .open_stream("/v1/terminal/w1", move |frame| {
                let _ = tx.send(frame);
            })
            .unwrap();
        assert_eq!(rx.recv_timeout(Duration::from_secs(5)).unwrap(), Frame::Open);
        assert_eq!(
            rx.recv_timeout(Duration::from_secs(5)).unwrap(),
            Frame::Text { data: r#"{"type":"terminal","agent":"w1","shared":false}"#.into() }
        );
        // The webview's key frame goes out on the stream's socket, mid-read.
        engine
            .send_stream(id, r#"{"type":"key","keys":["Enter"]}"#.into())
            .unwrap();
        assert_eq!(
            collect(&rx),
            [
                Frame::Binary { data: base64(b"\x1bc$ claude\r\n") },
                Frame::Close { code: 1000 },
            ]
        );
        // The stream is gone once closed: a late frame has nowhere to go.
        assert!(engine.send_stream(id, "{}".into()).is_err());
        let _ = std::fs::remove_file(&sock);
    }

    #[cfg(unix)]
    #[test]
    fn a_stream_finds_a_daemon_restarted_with_a_new_token() {
        use std::os::unix::net::UnixListener;
        use std::sync::mpsc;
        use tungstenite::protocol::frame::coding::CloseCode;
        use tungstenite::protocol::CloseFrame;

        // Relative: a socket path must stay short (104 bytes on macOS).
        let sock = PathBuf::from("target/stream-test.sock");
        let _ = std::fs::remove_file(&sock);
        let listener = UnixListener::bind(&sock).unwrap();
        // The daemon running now wants "new", and closes 4401 on any other token.
        std::thread::spawn(move || {
            for stream in listener.incoming().take(2) {
                let mut ws = tungstenite::accept(stream.unwrap()).unwrap();
                let close = if ws.read().unwrap().to_text().unwrap() == "new" {
                    ws.send(Message::Text(r#"{"type":"event"}"#.into())).unwrap();
                    CloseCode::Normal
                } else {
                    CloseCode::from(WS_UNAUTHORIZED)
                };
                let _ = ws.close(Some(CloseFrame { code: close, reason: "".into() }));
                while ws.read().is_ok() {}
            }
        });
        let info = |token: &str| DaemonInfo {
            socket: Some(sock.clone()),
            port: None,
            token: token.into(),
            version: "0.5.0".into(),
            pid: 1,
        };
        // daemon.json still names the old daemon when the stream opens, the new one after.
        let (old, new, reads) = (info("old"), info("new"), AtomicU32::new(0));
        let engine = Engine {
            discover: Arc::new(move || {
                Ok(if reads.fetch_add(1, Ordering::SeqCst) == 0 { old.clone() } else { new.clone() })
            }),
            ..Engine::default()
        };
        let (tx, rx) = mpsc::channel();
        engine
            .open_stream("/v1/events?since=0", move |frame| {
                let _ = tx.send(frame);
            })
            .unwrap();
        let mut frames = Vec::new();
        while let Ok(frame) = rx.recv_timeout(Duration::from_secs(5)) {
            let done = matches!(frame, Frame::Close { .. });
            frames.push(frame);
            if done {
                break;
            }
        }
        assert_eq!(
            frames,
            [
                Frame::Open,
                Frame::Text { data: r#"{"type":"event"}"#.into() },
                Frame::Close { code: 1000 },
            ]
        );
        let _ = std::fs::remove_file(&sock);
    }

    #[cfg(unix)]
    #[test]
    fn a_read_timeout_shows_the_busy_message() {
        use std::os::unix::net::UnixListener;
        use std::sync::mpsc;

        // Relative: a socket path must stay short (104 bytes on macOS).
        let sock = PathBuf::from("target/busy-test.sock");
        let _ = std::fs::remove_file(&sock);
        let listener = UnixListener::bind(&sock).unwrap();
        // Accepts the connection and holds it open, never answering, until the test ends.
        let (release, held) = mpsc::channel::<()>();
        std::thread::spawn(move || {
            let _conn = listener.accept();
            let _ = held.recv();
        });
        let info = daemon_info(&sock, "tok");
        let engine = Engine {
            request_timeout: Duration::from_millis(50),
            discover: Arc::new(move || Ok(info.clone())),
            ..Engine::default()
        };
        assert_eq!(engine.request("GET", "/v1/status", None).unwrap_err(), BUSY_MESSAGE);
        drop(release);
        let _ = std::fs::remove_file(&sock);
    }
}
