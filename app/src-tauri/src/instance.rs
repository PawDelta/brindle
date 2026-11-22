//! One app at a time, and restarting into an update (macOS).
//!
//! A brindle.app that runs is the only one: a second launch (`open -n`, the
//! binary run by hand) finds the first one's claim (`claim`: a lock on
//! `app.lock` in brindle's home, held while it runs and gone with it, force
//! quit included), brings that one to the front and exits.
//!
//! Restarting into an installed update (`relaunch`) asks LaunchServices for
//! the new instance (`open`) instead of spawning the app as a child, as Tauri's
//! `restart` does: a child is in this instance's coalition, and LaunchServices
//! keeps an exited app whose coalition still has members in the Dock as
//! "exited-with-subordinates", with its subordinates killed when that icon is
//! quit. Nor `open -n` while this instance lives: that second instance gets a
//! Dock tile of its own and keeps it. Instead a short-lived headless helper
//! (this executable with `--reopen-after-exit-of <pid> <bundle>`, never
//! reaching any app set-up) waits for this one to exit (`wait_for_exit`), runs
//! a plain `open <bundle>`, and exits. `--after-exit-of <pid>` is still
//! honoured for relaunches started by older apps.

#![cfg_attr(not(target_os = "macos"), allow(dead_code))]

use std::fs::File;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

use tauri::{AppHandle, Runtime};

/// The argument the relaunched instance gets, followed by the pid to outlive.
/// Kept for instances relaunched by an older app (`open -n ... --args --after-exit-of <pid>`).
pub const AFTER_EXIT_FLAG: &str = "--after-exit-of";
/// The headless relaunch helper's argument: `<pid> <bundle>` follow (`reopen_after_exit`).
pub const REOPEN_FLAG: &str = "--reopen-after-exit-of";
/// The claim's lock file, in brindle's home.
const LOCK: &str = "app.lock";
/// How long `wait_for_exit` waits at most: an instance that doesn't exit by then is dealt with by `claim`.
#[cfg(target_os = "macos")]
const EXIT_WAIT: libc::timespec = libc::timespec { tv_sec: 30, tv_nsec: 0 };

/// The .app bundle whose `Contents/MacOS/<exe>` is `exe`, or None outside a bundle (a dev build).
pub fn bundle_of(exe: &Path) -> Option<PathBuf> {
    let macos = exe.parent()?;
    let contents = macos.parent()?;
    let bundle = contents.parent()?;
    (macos.file_name()? == "MacOS" && contents.file_name()? == "Contents" && bundle.extension()? == "app")
        .then(|| bundle.to_path_buf())
}

/// This instance's claim to be the one app on brindle's home: held while it lives.
pub struct Claim {
    _lock: File,
}

/// Claim the app's one place on brindle's home `home`, for the instance in
/// `bundle`: None when another instance holds it (see `holder`).
pub fn claim(home: &Path, bundle: &Path) -> io::Result<Option<Claim>> {
    std::fs::create_dir_all(home)?;
    let mut lock = File::options().read(true).write(true).create(true).truncate(false).open(home.join(LOCK))?;
    #[cfg(unix)]
    {
        use std::os::fd::AsRawFd;
        // SAFETY: flock on a descriptor this process owns.
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            let err = io::Error::last_os_error();
            // Held by another instance; anything else is the lock file's trouble.
            return if err.kind() == io::ErrorKind::WouldBlock { Ok(None) } else { Err(err) };
        }
    }
    lock.set_len(0)?;
    writeln!(lock, "{}\n{}", std::process::id(), bundle.display())?;
    Ok(Some(Claim { _lock: lock }))
}

/// The bundle of the instance holding the claim on `home`, as it wrote it.
pub fn holder(home: &Path) -> Option<PathBuf> {
    let text = std::fs::read_to_string(home.join(LOCK)).ok()?;
    let bundle = text.lines().nth(1)?;
    (!bundle.is_empty()).then(|| PathBuf::from(bundle))
}

/// What a launch does once it knows the other instances: run, or leave it to the one that runs.
pub enum Place {
    /// This instance's: kept while it runs.
    Ours(Option<Claim>),
    /// Another instance's: it was brought to the front, and this one exits.
    Taken,
}

/// Take the app's one place on brindle's home `home` for the instance whose
/// executable is `exe`. Another instance that holds it is brought to the
/// front (`open` of its bundle activates it, and starts nothing when it runs).
/// Outside a bundle (a dev build) nothing is claimed.
pub fn take_place(home: &Path, exe: Option<&Path>) -> Place {
    let Some(bundle) = exe.and_then(bundle_of) else { return Place::Ours(None) };
    match claim(home, &bundle) {
        Ok(Some(claim)) => Place::Ours(Some(claim)),
        Ok(None) => {
            if let Some(theirs) = holder(home).filter(|theirs| theirs.is_dir()) {
                let _ = Command::new("/usr/bin/open").arg(theirs).status();
            }
            Place::Taken
        }
        // A home that can't hold the lock file: run anyway, as the app always did.
        Err(_) => Place::Ours(None),
    }
}

/// The command that launches the bundle of `exe` again once `pid` (this
/// instance) has exited; None outside a bundle. It runs `exe` itself as a
/// headless helper (`REOPEN_FLAG`, see `reopen_after_exit`) that waits for the
/// exit and only then asks LaunchServices for the bundle with a plain `open`.
/// Never `open -n`: a second instance started while this one still lives gets
/// a Dock tile of its own, apart from the person's pinned one, and keeps it.
pub fn relaunch_command(exe: &Path, pid: u32) -> Option<Command> {
    let bundle = bundle_of(exe)?;
    let mut cmd = Command::new(exe);
    cmd.arg(REOPEN_FLAG).arg(pid.to_string()).arg(bundle);
    cmd.stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null());
    Some(cmd)
}

/// The command that opens `bundle` through LaunchServices as the one instance:
/// no `-n`, so it takes the app's usual Dock tile.
pub fn reopen_command(bundle: &Path) -> Command {
    let mut cmd = Command::new("/usr/bin/open");
    cmd.arg(bundle);
    cmd
}

/// What the relaunch helper's arguments ask for (`reopen_after_exit`).
pub enum Reopen {
    /// Not the helper: start the app.
    No,
    /// The old instance has exited: run this `open <bundle>`, then exit.
    Open(Command),
    /// The old instance wasn't seen to exit: open nothing (a plain `open` would
    /// only bring the old one forward, and it would then quit with no successor).
    GaveUp,
}

/// The relaunch helper (`relaunch_command`): for `args` (the process's,
/// program name first) of the form `<exe> REOPEN_FLAG <pid> <bundle>`,
/// `wait(pid)` until the old instance has exited (true) and give the
/// `open <bundle>` to run. Any other launch is `Reopen::No`.
pub fn reopen_after_exit(args: &[String], wait: impl FnOnce(u32) -> bool) -> Reopen {
    let [_, flag, pid, bundle, ..] = args else { return Reopen::No };
    if flag != REOPEN_FLAG {
        return Reopen::No;
    }
    let Ok(pid) = pid.parse() else { return Reopen::No };
    if wait(pid) {
        Reopen::Open(reopen_command(Path::new(bundle)))
    } else {
        Reopen::GaveUp
    }
}

/// The app's entry point: the relaunch helper is handled first, before any
/// Tauri or AppKit set-up (`start_app`), so it never gets a Dock tile. In
/// helper mode it waits (`wait`), runs the open (`open`, its exit code) and
/// returns the code for the process to exit with, never starting the app;
/// otherwise it starts the app and returns None.
pub fn start(
    args: &[String],
    wait: impl FnOnce(u32) -> bool,
    open: impl FnOnce(Command) -> i32,
    start_app: impl FnOnce(),
) -> Option<i32> {
    match reopen_after_exit(args, wait) {
        Reopen::No => {
            start_app();
            None
        }
        Reopen::Open(cmd) => Some(open(cmd)),
        Reopen::GaveUp => Some(1),
    }
}

/// Restart the app into the bundle at its path (the update installed there):
/// on macOS through the relaunch helper (`relaunch_command`), which opens the
/// bundle through LaunchServices once this instance has exited, so the new
/// instance is in a coalition of its own and keeps the person's Dock tile;
/// elsewhere, or outside a bundle, Tauri's restart.
pub fn relaunch<R: Runtime>(app: &AppHandle<R>) {
    #[cfg(target_os = "macos")]
    if let Some(mut cmd) = std::env::current_exe().ok().and_then(|exe| relaunch_command(&exe, std::process::id())) {
        if cmd.spawn().is_ok() {
            app.exit(0);
            return;
        }
    }
    app.restart();
}

/// Wait until the process `pid` has exited (at once when it has), for up to
/// `EXIT_WAIT`: a kqueue's NOTE_EXIT, not a poll. True only when it has
/// exited; false when the bound ran out or the wait itself failed.
#[cfg(target_os = "macos")]
pub fn wait_for_exit(pid: u32) -> bool {
    wait_for_exit_with_limit(pid, &EXIT_WAIT)
}

/// Wait on an exiting process with the caller's bound. Daemon handover uses
/// a shorter bound than waiting for the old app to release its instance lock.
/// True only when the process is known to have exited.
#[cfg(target_os = "macos")]
pub(crate) fn wait_for_exit_with_limit(pid: u32, limit: &libc::timespec) -> bool {
    // SAFETY: a kqueue of our own, given one change and one event buffer we own, closed after.
    unsafe {
        let kq = libc::kqueue();
        if kq < 0 {
            return false;
        }
        let mut change: libc::kevent = std::mem::zeroed();
        change.ident = pid as libc::uintptr_t;
        change.filter = libc::EVFILT_PROC;
        change.flags = libc::EV_ADD | libc::EV_ONESHOT;
        change.fflags = libc::NOTE_EXIT;
        let mut event: libc::kevent = std::mem::zeroed();
        let n = libc::kevent(kq, &change, 1, &mut event, 1, limit);
        let err = io::Error::last_os_error().raw_os_error();
        libc::close(kq);
        match n {
            // A pid already gone fails to register (ESRCH, as an EV_ERROR event or -1): exited.
            1 if event.flags & libc::EV_ERROR != 0 => event.data == libc::ESRCH as libc::intptr_t,
            1 => event.fflags & libc::NOTE_EXIT != 0,
            -1 => err == Some(libc::ESRCH),
            // 0: the bound ran out with the process still alive.
            _ => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn one_instance_holds_the_claim_and_the_next_finds_whose_it_is() {
        let dir = std::env::current_dir().unwrap().join("target/instance-claim");
        let _ = std::fs::remove_dir_all(&dir);
        let first = claim(&dir, Path::new("/Applications/brindle.app")).unwrap().expect("free");
        // Held: a second instance gets none, and learns the holder's bundle to bring to the front.
        assert!(claim(&dir, Path::new("/tmp/staged/brindle.app")).unwrap().is_none());
        assert_eq!(holder(&dir), Some(PathBuf::from("/Applications/brindle.app")));
        // Gone with the instance: the next claim is free (a sibling test's spawn may hold a
        // copy of the descriptor, and so the lock, until it execs: waited out).
        drop(first);
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
        while claim(&dir, Path::new("/tmp/staged/brindle.app")).unwrap().is_none() {
            assert!(std::time::Instant::now() < deadline, "the lock outlived its holder");
        }
        // Outside a bundle nothing is claimed; a bundle is its Contents/MacOS's parent.
        assert!(bundle_of(Path::new("/Users/me/brindle/target/debug/brindle-desktop")).is_none());
        assert_eq!(bundle_of(Path::new("/Applications/brindle.app/Contents/MacOS/brindle-desktop")), Some(PathBuf::from("/Applications/brindle.app")));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_relaunch_reopens_the_bundle_without_open_n_after_the_old_instance_exits() {
        let exe = "/Applications/brindle.app/Contents/MacOS/brindle-desktop";
        let cmd = relaunch_command(Path::new(exe), 4242).unwrap();
        // The helper is this executable, not `open -n` (a second Dock tile).
        assert_eq!(cmd.get_program(), exe);
        let args: Vec<_> = cmd.get_args().map(|a| a.to_str().unwrap().to_string()).collect();
        assert_eq!(args, [REOPEN_FLAG, "4242", "/Applications/brindle.app"]);
        assert!(!args.iter().any(|a| a == "-n"));
        assert!(relaunch_command(Path::new("/Users/me/brindle/target/debug/brindle-desktop"), 4242).is_none());

        // The helper, started with those arguments: waits for that pid, runs a plain
        // `open <bundle>` and exits with its code, never starting the app.
        let argv: Vec<String> = std::iter::once(exe.to_string()).chain(args).collect();
        let mut waited = None;
        let mut opened = None;
        let code = start(
            &argv,
            |pid| {
                waited = Some(pid);
                true
            },
            |cmd| {
                opened = Some((cmd.get_program().to_owned(), cmd.get_args().map(|a| a.to_owned()).collect::<Vec<_>>()));
                0
            },
            || panic!("the helper started the app"),
        );
        assert_eq!((code, waited), (Some(0), Some(4242)));
        let (program, open_args) = opened.expect("opened the bundle");
        assert_eq!(program, "/usr/bin/open");
        assert_eq!(open_args, ["/Applications/brindle.app"]);

        // The old instance not seen to exit (the bound ran out): open nothing, and fail.
        let code = start(&argv, |_| false, |_| panic!("opened while the old instance lived"), || panic!("started the app"));
        assert_eq!(code, Some(1));

        // Any other launch, the old flag included, starts the app, waiting for nothing.
        for argv in [vec![exe], vec![exe, AFTER_EXIT_FLAG, "4242"], vec![exe, REOPEN_FLAG, "x", "/Applications/brindle.app"]] {
            let argv: Vec<String> = argv.into_iter().map(String::from).collect();
            let mut started = false;
            let code = start(&argv, |_| panic!("waited outside helper mode"), |_| panic!("opened"), || started = true);
            assert!(code.is_none() && started);
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn waiting_for_exit_returns_once_the_old_instance_is_gone() {
        // The old instance: a process that lives until its stdin closes.
        let mut old = Command::new("/bin/cat").stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::null()).spawn().unwrap();
        let pid = old.id();
        // A bound that runs out while it lives is no exit: the helper must not open then.
        assert!(!wait_for_exit_with_limit(pid, &libc::timespec { tv_sec: 0, tv_nsec: 0 }));
        let waiting = std::thread::spawn(move || wait_for_exit(pid));
        assert!(!waiting.is_finished());
        drop(old.stdin.take());
        assert!(waiting.join().unwrap(), "the exit wasn't reported as one");
        assert!(old.try_wait().unwrap().is_some(), "returned before the old instance exited");
        // One already gone: no wait at all, and an exit.
        assert!(wait_for_exit(pid));
    }
}
