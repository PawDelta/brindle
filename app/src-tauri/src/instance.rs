//! One app at a time, and restarting into an update (macOS).
//!
//! A brindle.app that runs is the only one: a second launch (`open -n`, the
//! binary run by hand) finds the first one's claim (`claim`: a lock on
//! `app.lock` in brindle's home, held while it runs and gone with it, force
//! quit included), brings that one to the front and exits.
//!
//! Restarting into an installed update (`relaunch`) asks LaunchServices for
//! the new instance (`open -n`) instead of spawning it as a child, as Tauri's
//! `restart` does: a child is in this instance's coalition, and LaunchServices
//! keeps an exited app whose coalition still has members in the Dock as
//! "exited-with-subordinates", with its subordinates (here the new app itself,
//! and anything else started as a child) killed when that icon is quit. The new
//! instance is told to wait for this one to exit (`--after-exit-of <pid>`,
//! `wait_for_exit`) before it claims the app's one place.

#![cfg_attr(not(target_os = "macos"), allow(dead_code))]

use std::fs::File;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

use tauri::{AppHandle, Runtime};

/// The argument the relaunched instance gets, followed by the pid to outlive.
pub const AFTER_EXIT_FLAG: &str = "--after-exit-of";
/// The claim's lock file, in brindle's home.
const LOCK: &str = "app.lock";
/// How long `wait_for_exit` waits at most: an instance that doesn't exit by then is dealt with by `claim`.
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

/// The command that launches the bundle of `exe` again through LaunchServices,
/// as a new instance that first waits for `pid` (this one) to exit; None
/// outside a bundle.
pub fn relaunch_command(exe: &Path, pid: u32) -> Option<Command> {
    let bundle = bundle_of(exe)?;
    let mut cmd = Command::new("/usr/bin/open");
    cmd.arg("-n").arg(bundle).arg("--args").arg(AFTER_EXIT_FLAG).arg(pid.to_string());
    Some(cmd)
}

/// Restart the app into the bundle at its path (the update installed there):
/// on macOS through LaunchServices (`relaunch_command`), so the new instance
/// is in a coalition of its own and this one leaves nothing behind in the
/// Dock; elsewhere, or outside a bundle, Tauri's restart.
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
/// `EXIT_WAIT`: a kqueue's NOTE_EXIT, not a poll.
#[cfg(target_os = "macos")]
pub fn wait_for_exit(pid: u32) {
    // SAFETY: a kqueue of our own, given one change and one event buffer we own, closed after.
    unsafe {
        let kq = libc::kqueue();
        if kq < 0 {
            return;
        }
        let mut change: libc::kevent = std::mem::zeroed();
        change.ident = pid as libc::uintptr_t;
        change.filter = libc::EVFILT_PROC;
        change.flags = libc::EV_ADD | libc::EV_ONESHOT;
        change.fflags = libc::NOTE_EXIT;
        let mut event: libc::kevent = std::mem::zeroed();
        // A pid already gone fails to register (ESRCH, as an EV_ERROR event or -1): done.
        libc::kevent(kq, &change, 1, &mut event, 1, &EXIT_WAIT);
        libc::close(kq);
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

    #[cfg(target_os = "macos")]
    #[test]
    fn the_relaunch_is_launchservices_and_waits_for_the_old_instance() {
        let cmd = relaunch_command(Path::new("/Applications/brindle.app/Contents/MacOS/brindle-desktop"), 4242).unwrap();
        assert_eq!(cmd.get_program(), "/usr/bin/open");
        assert_eq!(cmd.get_args().collect::<Vec<_>>(), ["-n", "/Applications/brindle.app", "--args", AFTER_EXIT_FLAG, "4242"]);
        assert!(relaunch_command(Path::new("/Users/me/brindle/target/debug/brindle-desktop"), 4242).is_none());

        // The old instance: a process that lives until its stdin closes.
        let mut old = Command::new("/bin/cat").stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::null()).spawn().unwrap();
        let pid = old.id();
        let waiting = std::thread::spawn(move || wait_for_exit(pid));
        assert!(!waiting.is_finished());
        drop(old.stdin.take());
        waiting.join().unwrap();
        assert!(old.try_wait().unwrap().is_some(), "returned before the old instance exited");
        // One already gone: no wait at all.
        wait_for_exit(pid);
    }
}
