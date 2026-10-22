//! First run: what brindle needs on this machine, and installing it. When the
//! app can't find or start the daemon, the front end's FirstRun screen asks
//! `setup_detect` what is here (uv, tmux, brindle and Claude Code; Codex and
//! Antigravity optional), found the way `brindle doctor` finds them
//! (brindle/doctor.py), and runs the equivalent of brindle's
//! scripts/install.sh (uv, Homebrew's tmux, `uv tool install brindle`) in a
//! pty of its own that a terminal pane shows: the `setup_install_*` commands.
//! Once it's done the front end connects again, which starts `brindle daemon`
//! (see `daemon`).
//!
//! Nothing here signs in to anything. Claude Code's sign-in is only read
//! (`claude auth status`, as doctor does); when it's signed out the screen
//! tells the person to run `claude` once themselves.
//!
//! On Windows brindle runs inside WSL (see `wsl`): the detection asks the
//! distribution (`detect_script`), and the install runs there through
//! wsl.exe, on pipes rather than a pty, with the same events for its pane.
//! The local detection and install are unused there.
#![cfg_attr(windows, allow(dead_code))]

use std::collections::HashMap;
use std::ffi::{OsStr, OsString};
use std::fs::File;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::ipc::Channel;
use tauri::State;

use crate::daemon::{base64, child_path, home_dir};

/// How long a `--version` or `claude auth status` gets (doctor's timeouts).
const PROBE_TIMEOUT: Duration = Duration::from_secs(10);
/// The install's output kept for a pane that attaches late (or again).
const KEEP_OUTPUT: usize = 1 << 20;
/// The Codex the ChatGPT app ships (providers.CODEX_BUNDLED).
const CODEX_BUNDLED: &str = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex";
/// The environment that signs Claude Code in without its own login (providers._ENV_AUTH).
const CLAUDE_ENV_AUTH: [&str; 6] = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
];

/// A tool brindle uses: the command, the variable that names another copy
/// of it, and how to ask its version.
struct Spec {
    name: &'static str,
    required: bool,
    env: Option<&'static str>,
    version_args: &'static [&'static str],
}

/// doctor.checks' tools, plus uv (which installs brindle) and brindle itself.
const SPECS: [Spec; 6] = [
    Spec { name: "uv", required: true, env: None, version_args: &["--version"] },
    Spec { name: "tmux", required: true, env: None, version_args: &["-V"] },
    Spec { name: "brindle", required: true, env: Some("BRINDLE_BIN"), version_args: &["--version"] },
    Spec { name: "claude", required: true, env: Some("BRINDLE_CLAUDE_BIN"), version_args: &["--version"] },
    Spec { name: "codex", required: false, env: Some("BRINDLE_CODEX_BIN"), version_args: &["--version"] },
    Spec { name: "agy", required: false, env: Some("BRINDLE_AGY_BIN"), version_args: &["--version"] },
];

/// One tool as found here.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Tool {
    pub name: &'static str,
    pub required: bool,
    pub path: Option<PathBuf>,
    /// The last line `--version` printed (doctor._version), cut to 40 characters.
    pub version: Option<String>,
}

/// Claude Code's sign-in, as doctor's signin_checks reads it.
#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SignIn {
    SignedIn,
    /// A key or cloud provider from the environment (CLAUDE_ENV_AUTH).
    EnvironmentKey,
    SignedOut,
    /// `claude auth status` gave no answer (an older CLI, a timeout).
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Detection {
    /// "mac", "linux" or "other": what installing tmux takes differs.
    pub os: &'static str,
    pub tools: Vec<Tool>,
    /// Homebrew, which installs tmux on macOS.
    pub homebrew: Option<PathBuf>,
    /// None when Claude Code isn't installed.
    pub claude_signin: Option<SignIn>,
}

fn is_executable(path: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        path.metadata()
            .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
    }
    #[cfg(not(unix))]
    path.is_file()
}

/// `cmd` as a shell finds it: a path as is, a bare name on `path`.
fn locate(cmd: &OsStr, path: &OsStr) -> Option<PathBuf> {
    let cmd = Path::new(cmd);
    if cmd.components().count() > 1 {
        return is_executable(cmd).then(|| cmd.to_path_buf());
    }
    std::env::split_paths(path)
        .map(|dir| dir.join(cmd))
        .find(|p| is_executable(p))
}

/// What a tool's `--version` printed: the last line, as doctor shows it.
fn version_line(output: &str) -> Option<String> {
    let line = output.trim().lines().last()?.trim();
    (!line.is_empty()).then(|| line.chars().take(40).collect())
}

/// `claude auth status` prints JSON with `loggedIn`; anything else is unknown.
pub fn parse_auth_status(output: Option<&str>) -> SignIn {
    let logged_in = output
        .and_then(|text| serde_json::from_str::<serde_json::Value>(text).ok())
        .and_then(|v| v.get("loggedIn")?.as_bool());
    match logged_in {
        Some(true) => SignIn::SignedIn,
        Some(false) => SignIn::SignedOut,
        None => SignIn::Unknown,
    }
}

/// Where `detect` looks: the OS, the PATH commands are found on
/// (`child_path`), copies an app ships off PATH (the ChatGPT app's Codex),
/// the environment, and a way to run a command for what it printed (stdout,
/// else stderr; None when it didn't run or timed out).
pub struct Machine<'a> {
    pub os: &'static str,
    pub path: &'a OsStr,
    pub bundled: &'a [(&'static str, PathBuf)],
    pub var: &'a dyn Fn(&str) -> Option<OsString>,
    pub run: &'a dyn Fn(&Path, &[&str]) -> Option<String>,
}

/// What's here.
pub fn detect(here: &Machine) -> Detection {
    let tools: Vec<Tool> = SPECS
        .iter()
        .map(|spec| {
            let named = spec.env.and_then(|name| (here.var)(name)).filter(|v| !v.is_empty());
            let found = locate(named.as_deref().unwrap_or(OsStr::new(spec.name)), here.path).or_else(|| {
                // An override that isn't there doesn't fall back (providers.codex_binary).
                let (_, bundled) = here.bundled.iter().find(|(name, _)| *name == spec.name)?;
                (named.is_none() && is_executable(bundled)).then(|| bundled.clone())
            });
            let version = found
                .as_deref()
                .and_then(|bin| (here.run)(bin, spec.version_args))
                .and_then(|out| version_line(&out));
            Tool { name: spec.name, required: spec.required, path: found, version }
        })
        .collect();
    let claude_signin = tools
        .iter()
        .find(|t| t.name == "claude")
        .and_then(|t| t.path.as_deref())
        .map(|bin| {
            if CLAUDE_ENV_AUTH.iter().any(|k| (here.var)(k).is_some_and(|v| !v.is_empty())) {
                SignIn::EnvironmentKey
            } else {
                parse_auth_status((here.run)(bin, &["auth", "status"]).as_deref())
            }
        });
    Detection {
        os: here.os,
        tools,
        homebrew: locate(OsStr::new("brew"), here.path),
        claude_signin,
    }
}

/// What `setup_detect` runs inside WSL; `detect_in` reads its report: a `tool`
/// line per tool (SPECS, then Homebrew) with its path and the last line its
/// version printed, `envauth` when a key in the environment signs Claude Code
/// in, and `auth` with what `claude auth status` printed. The override
/// variables (BRINDLE_BIN and the like) aren't set in wsl.exe's shell, so
/// tools are only looked for on PATH.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn detect_script() -> String {
    let mut script = String::from(crate::wsl::PATH_SETUP);
    script += r#"t() { if command -v timeout >/dev/null 2>&1; then timeout 10 "$@"; else "$@"; fi; }
tool() {
  name=$1; shift
  p="$(command -v "$name" 2>/dev/null)" || p=
  v=
  [ -n "$p" ] && v="$(t "$p" "$@" </dev/null 2>&1 | tail -n 1 | tr '\t' ' ')"
  printf 'tool\t%s\t%s\t%s\n' "$name" "$p" "$v"
}
"#;
    for spec in &SPECS {
        script += &format!("tool {} {}\n", spec.name, spec.version_args.join(" "));
    }
    script += "tool brew --version\n";
    script += &format!(
        "for k in {}; do eval \"v=\\${{$k:-}}\"; [ -n \"$v\" ] && echo envauth; done\n",
        CLAUDE_ENV_AUTH.join(" ")
    );
    script += r#"c="$(command -v claude 2>/dev/null)" && printf 'auth\t%s\n' "$(t "$c" auth status </dev/null 2>/dev/null | tr '\t\n' '  ')"
exit 0
"#;
    script
}

/// What `detect_script` reported, as `detect` tells it.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn detect_in(report: &str) -> Detection {
    let mut found: HashMap<&str, (Option<PathBuf>, Option<String>)> = HashMap::new();
    let (mut env_key, mut auth) = (false, None);
    for line in report.lines() {
        let mut fields = line.trim_end_matches('\r').splitn(4, '\t');
        match fields.next() {
            Some("tool") => {
                let (Some(name), Some(path)) = (fields.next(), fields.next()) else { continue };
                let path = (!path.is_empty()).then(|| PathBuf::from(path));
                let version = path.as_ref().and(fields.next()).and_then(version_line);
                found.insert(name, (path, version));
            }
            Some("envauth") => env_key = true,
            Some("auth") => auth = fields.next().map(str::trim),
            _ => {}
        }
    }
    let tools: Vec<Tool> = SPECS
        .iter()
        .map(|spec| {
            let (path, version) = found.get(spec.name).cloned().unwrap_or_default();
            Tool { name: spec.name, required: spec.required, path, version }
        })
        .collect();
    let claude_signin = tools.iter().find(|t| t.name == "claude" && t.path.is_some()).map(|_| {
        if env_key {
            SignIn::EnvironmentKey
        } else {
            parse_auth_status(auth.filter(|a| !a.is_empty()))
        }
    });
    Detection {
        os: "linux",
        tools,
        homebrew: found.get("brew").and_then(|(path, _)| path.clone()),
        claude_signin,
    }
}

fn this_os() -> &'static str {
    match std::env::consts::OS {
        "macos" => "mac",
        "linux" => "linux",
        _ => "other",
    }
}

/// Run `bin args` with `path` for its PATH, for at most `timeout`.
fn probe(bin: &Path, args: &[&str], path: &OsStr, timeout: Duration) -> Option<String> {
    let mut child = Command::new(bin)
        .args(args)
        .env("PATH", path)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + timeout;
    while child.try_wait().ok()?.is_none() {
        if Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let read = |pipe: Option<&mut dyn Read>| {
        let mut text = String::new();
        pipe.map(|p| p.read_to_string(&mut text));
        text
    };
    let stdout = read(child.stdout.as_mut().map(|p| p as &mut dyn Read));
    if !stdout.trim().is_empty() {
        return Some(stdout);
    }
    Some(read(child.stderr.as_mut().map(|p| p as &mut dyn Read)))
}

/// Where `uv tool install` puts brindle: `uv tool dir --bin`, which follows
/// UV_TOOL_BIN_DIR and the XDG directories before ~/.local/bin. None without
/// a uv on `path`.
fn uv_tool_bin(path: &OsStr) -> Option<PathBuf> {
    let uv = locate(OsStr::new("uv"), path)?;
    let dir = PathBuf::from(probe(&uv, &["tool", "dir", "--bin"], path, PROBE_TIMEOUT)?.trim());
    dir.is_absolute().then_some(dir)
}

/// `path` with uv's tool bin directory added last, when uv names one not on it.
pub fn with_uv_tool_bin(path: OsString) -> Result<OsString, String> {
    let Some(dir) = uv_tool_bin(&path) else {
        return Ok(path);
    };
    let mut dirs: Vec<PathBuf> = std::env::split_paths(&path).collect();
    if !dirs.contains(&dir) {
        dirs.push(dir);
    }
    std::env::join_paths(dirs).map_err(|e| format!("PATH: {e}"))
}

/// The PATH brindle and its tools are looked for on, and that the install and
/// the daemon run with: `child_path`, plus wherever uv installs tools, so a
/// brindle installed into a UV_TOOL_BIN_DIR off PATH is found too.
pub fn tool_path() -> Result<OsString, String> {
    with_uv_tool_bin(child_path(std::env::var_os("PATH").as_deref(), &home_dir())?)
}

/// The install: scripts/install.sh without its download and its closing
/// `brindle doctor` (the FirstRun screen checks again itself). Safe to run
/// again: it upgrades brindle in place. BRINDLE_VERSION and BRINDLE_SOURCE
/// work as they do there.
pub const INSTALL_SCRIPT: &str = r#"set -eu
step() { printf '\033[32m==>\033[0m %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

if ! have uv; then
  step "installing uv (Python tool installer, https://docs.astral.sh/uv)"
  # Quiet: its closing "run: source ..." hint is a command for a terminal the person never sees.
  curl -LsSf https://astral.sh/uv/install.sh | sh -s -- --quiet
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi

# The app ships its own tmux; Homebrew's, where there is one, is only a fallback.
# Nothing here tells the person to type a command: the app's screens say what's left.
if ! have tmux && have brew; then
  step "installing tmux"
  brew install tmux
fi

spec="brindle"
[ -n "${BRINDLE_VERSION:-}" ] && spec="brindle==$BRINDLE_VERSION"
[ -n "${BRINDLE_SOURCE:-}" ] && spec="$BRINDLE_SOURCE"
step "installing $spec"
uv tool install --upgrade --python '>=3.11' "$spec"

bin_dir="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
# The app finds brindle wherever uv put it; the providers come in the first launch's own step.
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) PATH="$bin_dir:$PATH"; export PATH ;;
esac

step "brindle $(brindle --version 2>/dev/null | cut -d' ' -f2) is installed."
"#;

fn install_command(path: &OsStr) -> Command {
    let mut cmd = Command::new("/bin/sh");
    cmd.args(["-c", INSTALL_SCRIPT])
        .env("PATH", path)
        .env("TERM", "xterm-256color");
    cmd
}

/// What an install's pane gets: its output (base64, as daemon streams send a
/// terminal's bytes) and, last, the script's exit code (-1 for a signal).
#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum PtyEvent {
    Data { data: String },
    Exit { code: i32 },
}

/// A pty's two ends: the master the app reads and writes, the slave the
/// command gets as its terminal.
#[cfg(unix)]
fn open_pty(cols: u16, rows: u16) -> io::Result<(File, File)> {
    use std::os::fd::FromRawFd;

    let (mut master, mut slave) = (0, 0);
    let mut size = libc::winsize { ws_row: rows, ws_col: cols, ws_xpixel: 0, ws_ypixel: 0 };
    // SAFETY: openpty writes the two descriptors it opens; the name and
    // termios pointers may be null; size outlives the call.
    let rc = unsafe {
        libc::openpty(
            &mut master,
            &mut slave,
            std::ptr::null_mut(),
            std::ptr::null_mut::<libc::termios>(),
            &mut size as *mut libc::winsize,
        )
    };
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: both descriptors were just opened and are owned by nothing else.
    let (master, slave) = unsafe { (File::from_raw_fd(master), File::from_raw_fd(slave)) };
    // The command mustn't inherit the master: it would hold its own pty open.
    // SAFETY: fcntl on a descriptor this function owns.
    unsafe { libc::fcntl(std::os::fd::AsRawFd::as_raw_fd(&master), libc::F_SETFD, libc::FD_CLOEXEC) };
    Ok((master, slave))
}

/// A started command's terminal: what it prints, where its keys go, and the
/// pty master its size is set on (none without a pty).
pub(crate) struct Term {
    pub(crate) output: Box<dyn Read + Send>,
    pub(crate) input: Box<dyn Write + Send>,
    pub(crate) master: Option<File>,
    pub(crate) child: Child,
}

/// Start `cmd` on a new pty as its controlling terminal.
#[cfg(unix)]
pub(crate) fn spawn_terminal(cmd: Command) -> io::Result<Term> {
    spawn_terminal_sized(cmd, 80, 24)
}

/// Start `cmd` on a new pty of `cols` x `rows` as its controlling terminal.
#[cfg(unix)]
pub(crate) fn spawn_terminal_sized(mut cmd: Command, cols: u16, rows: u16) -> io::Result<Term> {
    use std::os::unix::process::CommandExt;

    let (master, slave) = open_pty(cols, rows)?;
    cmd.stdin(Stdio::from(slave.try_clone()?))
        .stdout(Stdio::from(slave.try_clone()?))
        .stderr(Stdio::from(slave));
    // SAFETY: only async-signal-safe calls between fork and exec.
    unsafe {
        cmd.pre_exec(|| {
            if libc::setsid() == -1 {
                return Err(io::Error::last_os_error());
            }
            if libc::ioctl(0, libc::TIOCSCTTY as _, 0) == -1 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let child = cmd.spawn()?;
    // `cmd`, and with it this process's copies of the slave, is dropped here:
    // the master reads to its end once the command's side closes.
    Ok(Term {
        output: Box::new(master.try_clone()?),
        input: Box::new(master.try_clone()?),
        master: Some(master),
        child,
    })
}

/// No pty on Windows: the command (wsl.exe, see `wsl::install_script`) gets
/// pipes, its shell joins its stderr to its output, and the output gets a
/// pty's line ends for the pane.
#[cfg(windows)]
fn spawn_terminal(mut cmd: Command) -> io::Result<Term> {
    let mut child = cmd
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let output = child.stdout.take().ok_or_else(|| io::Error::other("no stdout"))?;
    let input = child.stdin.take().ok_or_else(|| io::Error::other("no stdin"))?;
    Ok(Term { output: Box::new(CrLf::new(output)), input: Box::new(input), master: None, child })
}

#[cfg(not(any(unix, windows)))]
fn spawn_terminal(_cmd: Command) -> io::Result<Term> {
    Err(io::Error::new(io::ErrorKind::Unsupported, "installing needs macOS, Linux or WSL"))
}

/// A command's output on a pipe, as a terminal would show it: a bare \n
/// becomes \r\n (what a pty's onlcr does), so each line starts at the left.
#[cfg_attr(not(windows), allow(dead_code))]
struct CrLf<R> {
    inner: R,
    after_cr: bool,
    pending: Vec<u8>,
}

impl<R> CrLf<R> {
    #[cfg_attr(not(windows), allow(dead_code))]
    fn new(inner: R) -> Self {
        CrLf { inner, after_cr: false, pending: Vec::new() }
    }
}

impl<R: Read> Read for CrLf<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        while self.pending.is_empty() {
            let mut raw = [0u8; 4096];
            let n = self.inner.read(&mut raw)?;
            if n == 0 {
                return Ok(0);
            }
            for &b in &raw[..n] {
                if b == b'\n' && !self.after_cr {
                    self.pending.push(b'\r');
                }
                self.after_cr = b == b'\r';
                self.pending.push(b);
            }
        }
        let n = buf.len().min(self.pending.len());
        buf[..n].copy_from_slice(&self.pending[..n]);
        self.pending.drain(..n);
        Ok(n)
    }
}

type Watcher = Box<dyn Fn(PtyEvent) + Send>;

/// One install run: its pty's master (for its size; none on pipes), the queue to its
/// writer, what it printed so far, its exit code once it ended, and the pane
/// watching it. Keys go through the writer's own thread: a write into a
/// command that isn't reading blocks, and must hold neither this run's lock
/// (the reader takes it for every chunk of output) nor the caller.
struct Run {
    master: Option<File>,
    input: Option<mpsc::Sender<Vec<u8>>>,
    output: Vec<u8>,
    exit: Option<i32>,
    watcher: Option<Watcher>,
}

/// The install the FirstRun screen started, if any. A pane attaches to it
/// (and again, after a remount) and gets everything it printed so far first.
#[derive(Clone, Default)]
pub struct Installer {
    run: Arc<Mutex<Option<Arc<Mutex<Run>>>>>,
}

impl Installer {
    /// Run `cmd` on a pty of its own, unless the last run is still going.
    fn start(&self, cmd: Command) -> Result<(), String> {
        let mut current = self.run.lock().map_err(|e| e.to_string())?;
        if let Some(run) = current.as_ref() {
            if run.lock().map_err(|e| e.to_string())?.exit.is_none() {
                return Err("an install is already running".into());
            }
        }
        let Term { output: mut reader, input: mut writer, master, mut child } =
            spawn_terminal(cmd).map_err(|e| format!("couldn't start the install: {e}"))?;
        let (input, keys) = mpsc::channel::<Vec<u8>>();
        // Ends once the run does (its queue is dropped), or the pty refuses a write.
        std::thread::spawn(move || {
            for data in keys {
                if writer.write_all(&data).is_err() {
                    break;
                }
            }
        });
        let run = Arc::new(Mutex::new(Run { master, input: Some(input), output: Vec::new(), exit: None, watcher: None }));
        *current = Some(run.clone());
        std::thread::spawn(move || {
            let mut buf = [0u8; 8192];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) => break,
                    Ok(n) => {
                        let Ok(mut run) = run.lock() else { break };
                        run.output.extend_from_slice(&buf[..n]);
                        let over = run.output.len().saturating_sub(KEEP_OUTPUT);
                        run.output.drain(..over);
                        if let Some(watch) = &run.watcher {
                            watch(PtyEvent::Data { data: base64(&buf[..n]) });
                        }
                    }
                    Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                    // EIO on Linux once the command's side has closed.
                    Err(_) => break,
                }
            }
            let code = child.wait().ok().and_then(|s| s.code()).unwrap_or(-1);
            if let Ok(mut run) = run.lock() {
                run.exit = Some(code);
                run.input = None;
                if let Some(watch) = &run.watcher {
                    watch(PtyEvent::Exit { code });
                }
            }
        });
        Ok(())
    }

    fn current(&self) -> Result<Arc<Mutex<Run>>, String> {
        self.run
            .lock()
            .map_err(|e| e.to_string())?
            .clone()
            .ok_or_else(|| "no install has started".to_string())
    }

    /// Watch the install: its output so far, then live, then its exit. A new
    /// watcher replaces the last.
    fn attach(&self, watch: Watcher) -> Result<(), String> {
        let run = self.current()?;
        let mut run = run.lock().map_err(|e| e.to_string())?;
        if !run.output.is_empty() {
            watch(PtyEvent::Data { data: base64(&run.output) });
        }
        if let Some(code) = run.exit {
            watch(PtyEvent::Exit { code });
        }
        run.watcher = Some(watch);
        Ok(())
    }

    /// What the person typed into the pane, queued for the writer: never
    /// waits on the pty. Dropped once the run has ended.
    fn write(&self, data: &[u8]) -> Result<(), String> {
        let run = self.current()?;
        let run = run.lock().map_err(|e| e.to_string())?;
        if let Some(input) = &run.input {
            let _ = input.send(data.to_vec());
        }
        Ok(())
    }

    fn resize(&self, cols: u16, rows: u16) -> Result<(), String> {
        let run = self.current()?;
        let run = run.lock().map_err(|e| e.to_string())?;
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            let size = libc::winsize { ws_row: rows, ws_col: cols, ws_xpixel: 0, ws_ypixel: 0 };
            if let Some(master) = &run.master {
                // SAFETY: TIOCSWINSZ reads a winsize that outlives the call.
                if unsafe { libc::ioctl(master.as_raw_fd(), libc::TIOCSWINSZ as _, &size) } == -1 {
                    return Err(io::Error::last_os_error().to_string());
                }
            }
        }
        #[cfg(not(unix))]
        let _ = (run, cols, rows);
        Ok(())
    }
}

#[cfg(not(windows))]
fn detect_here() -> Result<Detection, String> {
    let path = tool_path()?;
    Ok(detect(&Machine {
        os: this_os(),
        path: &path,
        bundled: &[("codex", CODEX_BUNDLED.into()), ("agy", home_dir().join(".local/bin/agy"))],
        var: &|name| std::env::var_os(name),
        run: &|bin, args| probe(bin, args, &path, PROBE_TIMEOUT),
    }))
}

/// Inside the WSL distribution brindle runs in.
#[cfg(windows)]
fn detect_here() -> Result<Detection, String> {
    crate::wsl::detect_output().map(|report| detect_in(&report))
}

/// What brindle needs and what is here.
#[tauri::command]
pub async fn setup_detect() -> Result<Detection, String> {
    tauri::async_runtime::spawn_blocking(detect_here)
        .await
        .map_err(|e| e.to_string())?
}

#[cfg(not(windows))]
fn install_here() -> Result<Command, String> {
    Ok(install_command(&tool_path()?))
}

#[cfg(windows)]
fn install_here() -> Result<Command, String> {
    crate::wsl::install_command()
}

/// Start the install (uv, tmux, brindle) on a pty; `setup_install_attach` shows it.
#[tauri::command]
pub async fn setup_install_start(installer: State<'_, Installer>) -> Result<(), String> {
    let installer = installer.inner().clone();
    tauri::async_runtime::spawn_blocking(move || installer.start(install_here()?))
        .await
        .map_err(|e| e.to_string())?
}

/// The install's output so far and from now on, on `channel`, ending with its exit.
#[tauri::command]
pub fn setup_install_attach(installer: State<'_, Installer>, channel: Channel<PtyEvent>) -> Result<(), String> {
    installer.attach(Box::new(move |event| {
        let _ = channel.send(event);
    }))
}

/// Keys typed into the install's pane (a sudo password, a confirmation).
#[tauri::command]
pub fn setup_install_write(installer: State<'_, Installer>, data: String) -> Result<(), String> {
    installer.write(data.as_bytes())
}

#[tauri::command]
pub fn setup_install_resize(installer: State<'_, Installer>, cols: u16, rows: u16) -> Result<(), String> {
    installer.resize(cols, rows)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    /// A directory of fake tools under target/: each an executable file.
    fn bin_dir(name: &str, tools: &[&str]) -> PathBuf {
        let dir = PathBuf::from(format!("target/setup-test/{name}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        for tool in tools {
            let file = dir.join(tool);
            std::fs::write(&file, "#!/bin/sh\n").unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o755)).unwrap();
            }
        }
        dir
    }

    fn found(d: &Detection) -> Vec<(&str, bool)> {
        d.tools.iter().map(|t| (t.name, t.path.is_some())).collect()
    }

    #[test]
    fn finds_what_is_installed_and_what_is_not() {
        let dir = bin_dir("partial", &["tmux", "claude", "brew"]);
        // Not executable: doesn't count, as a shell wouldn't run it.
        std::fs::write(dir.join("uv"), "").unwrap();
        let path = std::env::join_paths([dir.clone()]).unwrap();
        let versions: HashMap<&str, &str> =
            [("tmux", "tmux 3.5a\n"), ("claude", "2.1.0 (Claude Code)\n")].into();
        let run = |bin: &Path, args: &[&str]| -> Option<String> {
            let name = bin.file_name()?.to_str()?;
            if args == ["auth", "status"] {
                return Some(r#"{"loggedIn": false, "authMethod": "none"}"#.into());
            }
            versions.get(name).map(|v| v.to_string())
        };
        // Codex off PATH, where the ChatGPT app ships it; Antigravity's spot is empty.
        let app = bin_dir("partial-app", &["codex"]);
        let bundled = [("codex", app.join("codex")), ("agy", app.join("agy"))];
        let d = detect(&Machine { os: "mac", path: &path, bundled: &bundled, var: &|_| None, run: &run });
        assert_eq!(
            found(&d),
            [("uv", false), ("tmux", true), ("brindle", false), ("claude", true), ("codex", true), ("agy", false)]
        );
        assert_eq!(d.tools[4].path, Some(app.join("codex")));
        let tmux = &d.tools[1];
        assert_eq!((tmux.required, tmux.version.as_deref()), (true, Some("tmux 3.5a")));
        assert_eq!(d.tools[3].version.as_deref(), Some("2.1.0 (Claude Code)"));
        assert!(!d.tools[4].required && !d.tools[5].required);
        assert_eq!(d.homebrew, Some(dir.join("brew")));
        assert_eq!(d.claude_signin, Some(SignIn::SignedOut));
    }

    #[test]
    fn an_override_names_the_tool_and_an_environment_key_signs_claude_in() {
        let dir = bin_dir("override", &["uv", "tmux", "my-brindle", "claude"]);
        let path = std::env::join_paths([dir.clone()]).unwrap();
        let env: HashMap<&str, OsString> = [
            ("BRINDLE_BIN", dir.join("my-brindle").into_os_string()),
            ("ANTHROPIC_API_KEY", "sk-test".into()),
            // Empty: not set.
            ("BRINDLE_CLAUDE_BIN", "".into()),
        ]
        .into();
        let probed = Mutex::new(Vec::new());
        let run = |bin: &Path, args: &[&str]| -> Option<String> {
            probed.lock().unwrap().push(format!("{} {}", bin.file_name()?.to_str()?, args.join(" ")));
            None
        };
        let var = |k: &str| env.get(k).cloned();
        let d = detect(&Machine { os: "linux", path: &path, bundled: &[], var: &var, run: &run });
        assert_eq!(d.tools[2].path, Some(dir.join("my-brindle")));
        assert_eq!(d.tools[3].path, Some(dir.join("claude")));
        // No version printed: none shown.
        assert_eq!(d.tools[0].version, None);
        assert_eq!(d.claude_signin, Some(SignIn::EnvironmentKey));
        // A key stands in for the login: claude's status isn't asked.
        assert!(!probed.lock().unwrap().iter().any(|p| p.contains("auth")));
        assert_eq!(d.homebrew, None);

        // Without claude there is no sign-in to tell.
        let empty = std::env::join_paths([bin_dir("empty", &[])]).unwrap();
        let none = detect(&Machine { os: "linux", path: &empty, bundled: &[], var: &|_| None, run: &run });
        assert!(none.tools.iter().all(|t| t.path.is_none()));
        assert_eq!(none.claude_signin, None);
    }

    #[test]
    fn reads_claudes_auth_status() {
        assert_eq!(parse_auth_status(Some(r#"{"loggedIn": true}"#)), SignIn::SignedIn);
        assert_eq!(parse_auth_status(Some(r#"{"loggedIn": false}"#)), SignIn::SignedOut);
        // An older CLI with no status command, a timeout, something else printed.
        assert_eq!(parse_auth_status(None), SignIn::Unknown);
        assert_eq!(parse_auth_status(Some("error: unknown command 'auth'")), SignIn::Unknown);
        assert_eq!(parse_auth_status(Some(r#"{"loggedIn": "yes"}"#)), SignIn::Unknown);
        assert_eq!(version_line("uv 0.9.1 (abc 2026-01-01)\n"), Some("uv 0.9.1 (abc 2026-01-01)".into()));
        assert_eq!(version_line("  \n"), None);
    }

    #[test]
    fn the_install_script_is_install_sh_without_a_sign_in() {
        for step in ["astral.sh/uv/install.sh", "brew install tmux", "uv tool install --upgrade"] {
            assert!(INSTALL_SCRIPT.contains(step), "missing {step}");
        }
        assert!(!INSTALL_SCRIPT.contains("claude auth") && !INSTALL_SCRIPT.contains("login"));
        // It never tells the person to type a command: no provider installs, no package-manager lines.
        for told in ["npm install", "apt-get", "dnf install", "Install it with", "update-shell", "Claude Code isn't installed"] {
            assert!(!INSTALL_SCRIPT.contains(told), "tells the person to run {told}");
        }
        // It parses (sh -n reads without running).
        assert!(Command::new("/bin/sh").args(["-n", "-c", INSTALL_SCRIPT]).status().unwrap().success());
        let cmd = install_command(OsStr::new("/usr/bin:/bin"));
        assert_eq!(cmd.get_program(), "/bin/sh");
        assert_eq!(
            cmd.get_envs().find(|(k, _)| *k == "PATH").and_then(|(_, v)| v),
            Some(OsStr::new("/usr/bin:/bin"))
        );
    }

    /// A pane's base64 back to text.
    fn decode(data: &str) -> String {
        let mut bits = 0u32;
        let mut n = 0;
        let mut out = Vec::new();
        for c in data.bytes().filter(|&c| c != b'=') {
            let v = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
                .iter()
                .position(|&x| x == c)
                .unwrap() as u32;
            bits = bits << 6 | v;
            n += 6;
            if n >= 8 {
                n -= 8;
                out.push((bits >> n) as u8);
            }
        }
        String::from_utf8_lossy(&out).into_owned()
    }

    #[cfg(unix)]
    #[test]
    fn runs_a_command_on_a_pty_a_pane_can_attach_to_late() {
        let installer = Installer::default();
        // Nothing to attach to before a start.
        assert!(installer.attach(Box::new(|_| {})).is_err());
        let mut cmd = Command::new("/bin/sh");
        cmd.args(["-c", "test -t 0 && printf 'tty '; read line; printf 'got:%s ' \"$line\"; exit 3"]);
        installer.start(cmd).unwrap();
        // One at a time.
        assert_eq!(installer.start(Command::new("true")), Err("an install is already running".into()));
        installer.resize(100, 30).unwrap();

        let (tx, rx) = mpsc::channel();
        let first = tx.clone();
        installer.attach(Box::new(move |e| { let _ = first.send(e); })).unwrap();
        installer.write(b"hi\n").unwrap();
        let mut output = String::new();
        let code = loop {
            match rx.recv_timeout(Duration::from_secs(10)).unwrap() {
                PtyEvent::Data { data } => output += &decode(&data),
                PtyEvent::Exit { code } => break code,
            }
        };
        assert_eq!(code, 3);
        assert!(output.contains("tty ") && output.contains("got:hi"), "{output:?}");

        // A pane that attaches after the end gets it all again, then the exit.
        let (tx, rx) = mpsc::channel();
        installer.attach(Box::new(move |e| { let _ = tx.send(e); })).unwrap();
        let PtyEvent::Data { data } = rx.recv().unwrap() else { panic!("no output") };
        assert!(decode(&data).contains("got:hi"));
        assert_eq!(rx.recv().unwrap(), PtyEvent::Exit { code: 3 });
        // Done: another run may start.
        installer.start(Command::new("true")).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn a_paste_into_a_command_that_isnt_reading_stalls_neither_the_caller_nor_the_output() {
        let installer = Installer::default();
        // Never reads its input; prints plenty after a moment, which only gets
        // out while the pane's reader keeps draining the pty.
        let mut cmd = Command::new("/bin/sh");
        cmd.args(["-c", "sleep 0.2; i=0; while [ $i -lt 3000 ]; do echo \"line $i\"; i=$((i+1)); done"]);
        installer.start(cmd).unwrap();
        let (tx, rx) = mpsc::channel();
        installer.attach(Box::new(move |e| { let _ = tx.send(e); })).unwrap();

        // Far more than a pty's buffers hold. On a thread of its own, so a write
        // that waits on the pty fails the test instead of hanging it.
        let paste = "paste me\n".repeat(64 * 1024);
        let (done, written) = mpsc::channel();
        let writer = installer.clone();
        std::thread::spawn(move || {
            let _ = done.send(writer.write(paste.as_bytes()));
        });
        written
            .recv_timeout(Duration::from_secs(1))
            .expect("the write waited on the pty")
            .unwrap();

        let mut output = String::new();
        let code = loop {
            match rx.recv_timeout(Duration::from_secs(20)).expect("the output stalled") {
                PtyEvent::Data { data } => output += &decode(&data),
                PtyEvent::Exit { code } => break code,
            }
        };
        assert_eq!(code, 0);
        assert!(output.contains("line 2999"), "{} bytes, no last line", output.len());
        // The run is over: further keys are dropped, not queued.
        installer.write(b"late\n").unwrap();
    }

    #[test]
    fn output_on_pipes_gets_a_terminals_line_ends() {
        /// Hands out its bytes a few at a time, as a pipe might.
        struct Trickle(&'static [u8]);
        impl Read for Trickle {
            fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
                let n = self.0.len().min(buf.len()).min(3);
                buf[..n].copy_from_slice(&self.0[..n]);
                self.0 = &self.0[n..];
                Ok(n)
            }
        }
        let mut text = String::new();
        CrLf::new(Trickle(b"==> uv\nok\r\n\n\x1b[32mdone\x1b[0m\n")).read_to_string(&mut text).unwrap();
        assert_eq!(text, "==> uv\r\nok\r\n\r\n\x1b[32mdone\x1b[0m\r\n");
    }

    /// detect_script in a shell with fake tools on PATH (as WSL runs it), read by detect_in.
    #[cfg(unix)]
    #[test]
    fn detects_what_a_wsl_distribution_has() {
        use std::os::unix::fs::PermissionsExt;

        let dir = std::fs::canonicalize(bin_dir("wsl-tools", &[])).unwrap();
        for (name, body) in [
            ("tmux", "echo 'tmux 3.4'"),
            ("uv", "echo 'uv 0.9.1'"),
            ("claude", r#"[ "$1" = auth ] && echo '{"loggedIn": true}' || echo '2.1.0 (Claude Code)'"#),
        ] {
            let file = dir.join(name);
            std::fs::write(&file, format!("#!/bin/sh\n{body}\n")).unwrap();
            std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let run = |extra: &[(&str, &str)]| {
            let mut cmd = Command::new("/bin/sh");
            cmd.args(["-c", &detect_script()])
                .env_clear()
                .env("PATH", format!("{}:/usr/bin:/bin", dir.display()))
                .env("HOME", dir.join("home"));
            cmd.envs(extra.iter().copied());
            detect_in(&String::from_utf8(cmd.output().unwrap().stdout).unwrap())
        };
        let d = run(&[]);
        assert_eq!(d.os, "linux");
        assert_eq!(
            found(&d),
            [("uv", true), ("tmux", true), ("brindle", false), ("claude", true), ("codex", false), ("agy", false)]
        );
        assert_eq!(d.tools[0].path, Some(dir.join("uv")));
        assert_eq!(d.tools[1].version.as_deref(), Some("tmux 3.4"));
        assert_eq!(d.tools[3].version.as_deref(), Some("2.1.0 (Claude Code)"));
        assert_eq!(d.claude_signin, Some(SignIn::SignedIn));
        assert_eq!(d.homebrew, None);
        // A key in the distribution's environment signs Claude Code in.
        assert_eq!(run(&[("ANTHROPIC_API_KEY", "sk-test")]).claude_signin, Some(SignIn::EnvironmentKey));
        // Nothing reported (the distribution didn't answer): nothing found.
        let none = detect_in("");
        assert!(none.tools.iter().all(|t| t.path.is_none()) && none.claude_signin.is_none());
    }

    #[test]
    fn finds_brindle_where_uv_installs_tools_off_path() {
        // uv on PATH, configured (UV_TOOL_BIN_DIR) to put tools in a directory that isn't.
        let tools = std::fs::canonicalize(bin_dir("uv-tools", &["brindle"])).unwrap();
        let on_path = bin_dir("uv-on-path", &[]);
        let uv = on_path.join("uv");
        std::fs::write(&uv, format!("#!/bin/sh\n[ \"$*\" = 'tool dir --bin' ] && echo '{}'\n", tools.display())).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&uv, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let path = std::env::join_paths([on_path.clone(), "/usr/bin".into(), "/bin".into()]).unwrap();

        let full = with_uv_tool_bin(path.clone()).unwrap();
        assert_eq!(std::env::split_paths(&full).last(), Some(tools.clone()));
        // Already there: not added twice.
        assert_eq!(with_uv_tool_bin(full.clone()).unwrap(), full);
        let d = detect(&Machine { os: "mac", path: &full, bundled: &[], var: &|_| None, run: &|_, _| None });
        assert_eq!(d.tools[2].path, Some(tools.join("brindle")));

        // Without a uv there is nothing to ask.
        let bare = std::env::join_paths([bin_dir("uv-none", &[])]).unwrap();
        assert_eq!(with_uv_tool_bin(bare.clone()).unwrap(), bare);
    }
}
