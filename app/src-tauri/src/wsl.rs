//! The Windows app as a WSL thin client (plan 3.3). brindle runs every agent
//! in tmux, so on Windows the engine and its daemon run inside a WSL
//! distribution and the app talks to them from outside: `brindle daemon --tcp`
//! in the distribution listens on 127.0.0.1 too (WSL2 forwards localhost to
//! Windows) and writes the port and token to its daemon.json, which the app
//! reads through the distribution's share, `\\wsl$\<distro>\home\<user>\.brindle\daemon.json`.
//! Its pid is a Linux one no Windows tool can check, so a daemon counts as
//! running when it answers an authenticated `GET /v1/meta` on that port; when
//! none does the app starts one with `wsl.exe`. The first run's detection and
//! install (see `setup`) run inside the distribution as well.
//!
//! The distribution is $BRINDLE_WSL_DISTRO, else WSL's default (the first
//! `wsl.exe --list --quiet` names), never Docker's or Rancher's own.
//!
//! What only parses or builds is compiled everywhere, so its tests run on any
//! OS; what runs wsl.exe is Windows-only.
#![cfg_attr(not(windows), allow(dead_code))]

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::daemon::{answers_meta, parse_info, DaemonInfo, START_LOG};

/// Distributions other tools install for themselves, with no brindle in them.
const NOT_FOR_PEOPLE: [&str; 2] = ["docker-desktop", "rancher-desktop"];

/// Every script run in the distribution starts with this: wsl.exe runs it
/// without a login shell, so ~/.local/bin (where uv puts brindle) isn't on
/// PATH. Added after the inherited entries, as `daemon::child_path` does.
pub const PATH_SETUP: &str = "PATH=\"$PATH:$HOME/.local/bin\"; export PATH\n";

pub const NO_DISTRO: &str = "brindle runs inside WSL on Windows, and WSL has no Linux distribution \
     yet: install one with `wsl --install` in PowerShell, then open the app again";

/// What wsl.exe printed, as text: its own messages (`--list`) come in
/// UTF-16LE unless WSL_UTF8 is set; a Linux command's output in UTF-8.
pub fn decode(raw: &[u8]) -> String {
    let utf16 = raw.starts_with(&[0xFF, 0xFE]) || (raw.len() >= 2 && raw[1] == 0);
    if !utf16 {
        return String::from_utf8_lossy(raw).into_owned();
    }
    let units: Vec<u16> = raw
        .chunks_exact(2)
        .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
        .collect();
    String::from_utf16_lossy(&units).trim_start_matches('\u{feff}').to_string()
}

/// The distributions `wsl.exe --list --quiet` names, the default first.
/// A name has no spaces, so a message WSL prints instead (no distribution
/// yet, an update notice) isn't taken for one.
pub fn parse_distros(raw: &[u8]) -> Vec<String> {
    decode(raw)
        .lines()
        .map(|line| line.trim_matches(|c: char| c.is_whitespace() || c == '\0'))
        .filter(|name| !name.is_empty() && !name.contains(char::is_whitespace))
        .filter(|name| !NOT_FOR_PEOPLE.iter().any(|other| name.starts_with(other)))
        .map(String::from)
        .collect()
}

/// The one to use: `wanted` ($BRINDLE_WSL_DISTRO) when set, else the default.
pub fn pick_distro(distros: &[String], wanted: Option<&str>) -> Result<String, String> {
    match wanted.map(str::trim).filter(|w| !w.is_empty()) {
        Some(wanted) => distros
            .iter()
            .find(|d| d.eq_ignore_ascii_case(wanted))
            .cloned()
            .ok_or_else(|| {
                format!(
                    "BRINDLE_WSL_DISTRO names {wanted}, which WSL doesn't have (it has: {})",
                    if distros.is_empty() { "none".to_string() } else { distros.join(", ") }
                )
            }),
        None => distros.first().cloned().ok_or_else(|| NO_DISTRO.to_string()),
    }
}

/// `$HOME` as the distribution printed it: an absolute path.
pub fn parse_home(raw: &[u8]) -> Result<String, String> {
    let text = decode(raw);
    let home = text.trim().trim_end_matches('/');
    if !home.starts_with('/') {
        return Err(format!("couldn't read $HOME in WSL: it printed {:?}", text.trim()));
    }
    Ok(home.to_string())
}

/// Linux path `linux` (absolute) inside `distro`, as Windows sees it.
pub fn unc_path(distro: &str, linux: &str) -> PathBuf {
    PathBuf::from(format!(r"\\wsl$\{distro}{}", linux.replace('/', "\\")))
}

/// The distribution brindle runs in, and its user's home there.
#[derive(Debug, Clone, PartialEq)]
pub struct Target {
    pub distro: String,
    pub home: String,
}

impl Target {
    /// daemon.json, which `brindle daemon` writes to ~/.brindle.
    pub fn info_path(&self) -> PathBuf {
        unc_path(&self.distro, &format!("{}/.brindle/daemon.json", self.home))
    }

    /// Where a daemon the app started puts its stderr (as on macOS).
    pub fn log_path(&self) -> PathBuf {
        unc_path(&self.distro, &format!("{}/.brindle/{START_LOG}", self.home))
    }
}

/// The daemon a daemon.json from WSL at `path` names, if it answers there.
/// Only its port is any use from Windows: the socket it names is a Linux
/// path, dropped so the connection goes over TCP.
pub fn running_at(path: &Path) -> Option<DaemonInfo> {
    let mut info = parse_info(&std::fs::read_to_string(path).ok()?).ok()?;
    info.socket = None;
    let port = info.port?;
    answers_meta(port, &info.token).then_some(info)
}

/// Whether daemon.json at `path` still names `info`'s port and token. A
/// daemon that exits removes it, so a port it gave up (which any local
/// process may take) isn't sent to again on the strength of a cached find.
pub fn still_named_at(path: &Path, info: &DaemonInfo) -> bool {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|text| parse_info(&text).ok())
        .is_some_and(|now| now.port.is_some() && now.port == info.port && now.token == info.token)
}

/// Whether a daemon.json at `path` names a daemon that listens on no TCP
/// port, which a Windows app can't reach (it may be stale: only a failed
/// start asks).
pub fn socket_only_at(path: &Path) -> bool {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|text| parse_info(&text).ok())
        .is_some_and(|info| info.port.is_none())
}

/// `brindle daemon --tcp --idle-exit`, its stderr in ~/.brindle/app-daemon.log
/// as the macOS app keeps it.
pub fn daemon_script() -> String {
    format!(
        "{PATH_SETUP}mkdir -p \"$HOME/.brindle\"\n\
         exec brindle daemon --tcp --idle-exit </dev/null >/dev/null 2>\"$HOME/.brindle/{START_LOG}\"\n"
    )
}

/// `$HOME`, for `Target`.
pub const HOME_SCRIPT: &str = "printf '%s' \"$HOME\"";

/// The install (`setup::INSTALL_SCRIPT`) inside the distribution, on pipes
/// rather than a pty: its stderr joins its output.
pub fn install_script() -> String {
    format!("exec 2>&1\n{PATH_SETUP}TERM=xterm-256color; export TERM\n{}", crate::setup::INSTALL_SCRIPT)
}

/// wsl.exe's arguments for `sh -c script` in `distro`, in the Linux home
/// (not the Windows directory wsl.exe would map). --exec, not `--`: `--`
/// hands the line to the user's shell to split again, which would read the
/// script's quotes a second time.
pub fn args(distro: &str, script: &str) -> Vec<OsString> {
    ["-d", distro, "--cd", "~", "--exec", "sh", "-c", script]
        .into_iter()
        .map(OsString::from)
        .collect()
}

/// wsl.exe, with no console window of its own and its messages in UTF-8
/// (`decode` reads either).
fn wsl_exe() -> Command {
    let mut cmd = Command::new("wsl.exe");
    cmd.env("WSL_UTF8", "1");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd
}

/// `script` run in `distro` by wsl.exe.
pub fn command(distro: &str, script: &str) -> Command {
    let mut cmd = wsl_exe();
    cmd.args(args(distro, script));
    cmd
}

#[cfg(windows)]
mod run {
    use std::process::{Command, Stdio};
    use std::sync::Mutex;
    use std::time::{Duration, Instant};

    use super::*;
    use crate::daemon::{lacks_daemon, log_tail, too_old, START_TIMEOUT};

    /// Found once: listing the distributions and asking one its $HOME each
    /// start a wsl.exe, too slow for every reconnect.
    static TARGET: Mutex<Option<Target>> = Mutex::new(None);

    fn output(mut cmd: Command, what: &str) -> Result<std::process::Output, String> {
        cmd.stdin(Stdio::null())
            .output()
            .map_err(|e| format!("couldn't run wsl.exe for {what} ({e}): {NO_DISTRO}"))
    }

    pub fn target() -> Result<Target, String> {
        let mut known = TARGET.lock().map_err(|e| e.to_string())?;
        if let Some(target) = known.as_ref() {
            return Ok(target.clone());
        }
        let mut list = wsl_exe();
        list.args(["--list", "--quiet"]);
        let listed = output(list, "its distributions")?;
        // No distribution yet: wsl.exe says so and fails.
        let distros = if listed.status.success() { parse_distros(&listed.stdout) } else { Vec::new() };
        let distro = pick_distro(&distros, std::env::var("BRINDLE_WSL_DISTRO").ok().as_deref())?;
        let home = output(command(&distro, HOME_SCRIPT), "$HOME")?;
        if !home.status.success() {
            return Err(format!(
                "WSL couldn't start {distro}: {}",
                decode(&[home.stdout, home.stderr].concat()).trim()
            ));
        }
        let target = Target { distro, home: parse_home(&home.stdout)? };
        *known = Some(target.clone());
        Ok(target)
    }

    /// Start `brindle daemon --tcp` in the distribution and wait until it answers.
    fn start(target: &Target) -> Result<DaemonInfo, String> {
        let distro = &target.distro;
        // wsl.exe stays while the daemon runs: the distribution with it.
        let mut child = command(distro, &daemon_script())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("couldn't start brindle in WSL ({distro}): {e}"))?;
        let deadline = Instant::now() + START_TIMEOUT;
        loop {
            if let Some(info) = running_at(&target.info_path()) {
                std::thread::spawn(move || child.wait());
                return Ok(info);
            }
            if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
                // One started by someone else in the meantime makes this one exit: use that.
                if let Some(info) = running_at(&target.info_path()) {
                    return Ok(info);
                }
                if status.code() == Some(127) {
                    return Err(format!("brindle isn't installed in WSL ({distro}): no `brindle` on PATH or in ~/.local/bin"));
                }
                let tail = log_tail(&target.log_path());
                if lacks_daemon(&tail) {
                    return Err(too_old(None));
                }
                let mut why = format!("`brindle daemon` exited in WSL ({distro}) ({status}): {tail}");
                if socket_only_at(&target.info_path()) {
                    why += ". A daemon already runs there without --tcp, which the Windows app can't \
                            reach: stop it with `brindle daemon stop` in WSL and the app starts one that can";
                }
                return Err(why);
            }
            if Instant::now() > deadline {
                return Err(format!(
                    "`brindle daemon` didn't start in WSL ({distro}) within {}s (see {})",
                    START_TIMEOUT.as_secs(),
                    target.log_path().display()
                ));
            }
            std::thread::sleep(Duration::from_millis(200));
        }
    }

    /// The daemon running in WSL, started first if none answers.
    pub fn ensure() -> Result<DaemonInfo, String> {
        let target = target()?;
        running_at(&target.info_path()).map_or_else(|| start(&target), Ok)
    }

    /// Whether the daemon last found is still the one daemon.json names.
    pub fn still_named(info: &DaemonInfo) -> bool {
        target().is_ok_and(|target| still_named_at(&target.info_path(), info))
    }

    /// The first run's install, in the distribution.
    pub fn install_command() -> Result<Command, String> {
        Ok(command(&target()?.distro, &install_script()))
    }

    /// What the distribution has: `setup::DETECT_SCRIPT`'s report there.
    pub fn detect_output() -> Result<String, String> {
        let target = target()?;
        let out = output(command(&target.distro, &crate::setup::detect_script()), "the setup check")?;
        Ok(decode(&out.stdout))
    }
}

#[cfg(windows)]
pub use run::{detect_output, ensure, install_command, still_named};

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    fn utf16(text: &str) -> Vec<u8> {
        let mut raw = vec![0xFF, 0xFE];
        raw.extend(text.encode_utf16().flat_map(u16::to_le_bytes));
        raw
    }

    #[test]
    fn lists_the_distributions_wsl_prints_in_either_encoding() {
        let listing = "Ubuntu-24.04\r\ndocker-desktop\r\nDebian\r\ndocker-desktop-data\r\nrancher-desktop\r\n";
        let want = ["Ubuntu-24.04", "Debian"];
        assert_eq!(parse_distros(&utf16(listing)), want);
        // No BOM (older wsl.exe), and UTF-8 (WSL_UTF8=1).
        assert_eq!(parse_distros(&utf16(listing)[2..]), want);
        assert_eq!(parse_distros(listing.as_bytes()), want);
        // None yet: WSL prints a message, not a name.
        let none = "Windows Subsystem for Linux has no installed distributions.\r\n\
                    Use 'wsl.exe --list --online' to list available distributions\r\n";
        assert!(parse_distros(&utf16(none)).is_empty());
        assert!(parse_distros(b"").is_empty());
    }

    #[test]
    fn picks_the_named_distribution_else_the_default() {
        let distros = vec!["Ubuntu".to_string(), "Debian".to_string()];
        assert_eq!(pick_distro(&distros, None), Ok("Ubuntu".into()));
        assert_eq!(pick_distro(&distros, Some(" ")), Ok("Ubuntu".into()));
        assert_eq!(pick_distro(&distros, Some("debian")), Ok("Debian".into()));
        assert!(pick_distro(&distros, Some("Arch")).unwrap_err().contains("Ubuntu, Debian"));
        assert_eq!(pick_distro(&[], None), Err(NO_DISTRO.into()));
    }

    #[test]
    fn finds_daemon_json_through_the_distributions_share() {
        assert_eq!(parse_home(b"/home/me\n"), Ok("/home/me".into()));
        assert_eq!(parse_home(b"/root/"), Ok("/root".into()));
        assert!(parse_home(b"").is_err());
        assert!(parse_home(b"sh: 1: printf: not found").is_err());
        let target = Target { distro: "Ubuntu-24.04".into(), home: "/home/me".into() };
        assert_eq!(
            target.info_path(),
            PathBuf::from(r"\\wsl$\Ubuntu-24.04\home\me\.brindle\daemon.json")
        );
        assert_eq!(target.log_path(), PathBuf::from(r"\\wsl$\Ubuntu-24.04\home\me\.brindle\app-daemon.log"));
    }

    #[test]
    fn runs_scripts_in_the_distribution_with_wsl_exe() {
        let cmd = command("Ubuntu", &daemon_script());
        assert_eq!(cmd.get_program(), "wsl.exe");
        let args: Vec<_> = cmd.get_args().map(|a| a.to_string_lossy().into_owned()).collect();
        assert_eq!(args[..7], ["-d", "Ubuntu", "--cd", "~", "--exec", "sh", "-c"]);
        assert!(args[7].contains("exec brindle daemon --tcp --idle-exit"), "{}", args[7]);
        assert!(args[7].starts_with(PATH_SETUP));
        // The scripts parse (sh -n reads without running).
        #[cfg(unix)]
        for script in [daemon_script(), install_script(), HOME_SCRIPT.to_string()] {
            assert!(Command::new("/bin/sh").args(["-n", "-c", &script]).status().unwrap().success());
        }
    }

    /// A fake daemon on 127.0.0.1 answering each GET /v1/meta: 200 with the
    /// right token, 401 without.
    fn fake_tcp_daemon(token: &'static str, connections: usize) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            for stream in listener.incoming().take(connections) {
                let mut stream = stream.unwrap();
                let mut head = Vec::new();
                let mut byte = [0u8];
                while !head.ends_with(b"\r\n\r\n") && stream.read(&mut byte).unwrap() == 1 {
                    head.push(byte[0]);
                }
                let head = String::from_utf8_lossy(&head);
                let authorized = head.starts_with("GET /v1/meta ")
                    && head.contains(&format!("Authorization: Bearer {token}\r\n"));
                let (status, body) = if authorized {
                    ("200 OK", r#"{"engine_version": "0.5.0"}"#)
                } else {
                    ("401 Unauthorized", r#"{"error": {}}"#)
                };
                let _ = write!(stream, "HTTP/1.1 {status}\r\ncontent-length: {}\r\n\r\n{body}", body.len());
            }
        });
        port
    }

    #[test]
    fn a_daemon_runs_when_it_answers_meta_with_the_token() {
        let port = fake_tcp_daemon("good", 2);
        assert!(answers_meta(port, "good"));
        assert!(!answers_meta(port, "stale"));
        // Nothing listens there any more.
        let closed = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        assert!(!answers_meta(closed, "good"));
    }

    #[test]
    fn reads_a_wsl_daemon_json_and_keeps_only_a_daemon_that_answers() {
        let dir = PathBuf::from("target/wsl-test");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("daemon.json");
        let write = |port: Option<u16>, token: &str| {
            let port = port.map_or("null".to_string(), |p| p.to_string());
            std::fs::write(
                &path,
                format!(r#"{{"socket": "/home/me/.brindle/daemon.sock", "port": {port}, "token": "{token}", "version": "0.5.0", "pid": 77}}"#),
            )
            .unwrap();
        };
        let port = fake_tcp_daemon("tok", 2);
        write(Some(port), "tok");
        let info = running_at(&path).expect("the daemon answers");
        // Its socket is a Linux path: the connection goes over TCP.
        assert_eq!((info.socket.clone(), info.port, info.pid), (None, Some(port), 77));
        assert!(!socket_only_at(&path));
        assert!(still_named_at(&path, &info));
        // Left behind by a daemon that has since been replaced: its token is refused,
        // and the one found before is no longer trusted.
        write(Some(port), "old");
        assert_eq!(running_at(&path), None);
        assert!(!still_named_at(&path, &info));
        // A daemon started without --tcp can't be reached from Windows.
        write(None, "tok");
        assert_eq!(running_at(&path), None);
        assert!(socket_only_at(&path));
        std::fs::remove_file(&path).unwrap();
        assert_eq!(running_at(&path), None);
        assert!(!socket_only_at(&path));
        // The daemon exited (and removed its daemon.json): find the next one.
        assert!(!still_named_at(&path, &info));
    }
}
