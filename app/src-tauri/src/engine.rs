//! The engine brindle.app carries: its own Python with brindle installed, and
//! tmux (app/scripts/build_engine.sh, bundled by tauri.macos.conf.json as
//! `Contents/Resources/engine`), so nobody installs or updates brindle by hand.
//! `bin/brindle` there is a launcher that runs it with that Python and puts
//! the engine's bin (that brindle, its tmux) first on PATH.
//!
//! A release build on macOS starts only that engine's daemon. A dev build
//! (`tauri dev`, cargo test), which has no bundle around it, and the Linux
//! build, which doesn't carry an engine yet, start the brindle found on PATH.
//!
//! `install_cli` links the engine's brindle into a directory on the person's
//! PATH, so `brindle` in a terminal is the app's too.

// Windows starts its engine in WSL (see `wsl`): only install_cli is used there.
#![cfg_attr(windows, allow(dead_code))]

use std::ffi::OsStr;
#[cfg(unix)]
use std::io;
use std::path::{Path, PathBuf};

use serde::Serialize;

/// Where the bundled engine sits in the .app, from `Contents/MacOS`.
const ENGINE_IN_BUNDLE: &str = "../Resources/engine";
/// The launcher, inside the engine.
const LAUNCHER: &str = "bin/brindle";

/// The engine of the app whose executable is `exe`, when it carries one.
pub fn bundled_engine_at(exe: &Path) -> Option<PathBuf> {
    let engine = exe.parent()?.join(ENGINE_IN_BUNDLE);
    let engine = engine.canonicalize().ok()?;
    engine.join(LAUNCHER).is_file().then_some(engine)
}

/// The engine this app carries, or None (a dev build, a Linux build).
pub fn bundled_engine() -> Option<PathBuf> {
    bundled_engine_at(&std::env::current_exe().ok()?.canonicalize().ok()?)
}

/// The prefix of the command line of a brindle the launcher of `engine` runs:
/// its own Python (`ps` shows the path the launcher exec'd it by).
pub fn python_command(engine: &Path) -> String {
    format!("{}/python/bin/python3 ", engine.display())
}

/// The file in the engine naming its build (build_engine.sh: its inputs' hash).
const BUILD_FILE: &str = "BUILD";

/// How the launcher names the build on the command line of every brindle it
/// runs (`python3 -X brindle-build=<id>`, which Python keeps in
/// sys._xoptions and otherwise ignores): the process's own record of which
/// engine it is, for as long as it runs.
pub fn build_option(build: &str) -> String {
    format!("brindle-build={build}")
}

/// `engine`'s build id (hex, from its BUILD file), or why an app can't run that engine: incomplete only when
/// it has no BUILD file. One that can't be read now says why (an app out of
/// file descriptors gets "Too many open files"), as does one that names no build.
pub fn require_build(engine: &Path) -> Result<String, String> {
    let file = engine.join(BUILD_FILE);
    let text = match std::fs::read_to_string(&file) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(format!(
                "this brindle app's engine is incomplete (no {}): download it again from pawdelta.com/brindle",
                file.display()
            ))
        }
        Err(e) => return Err(format!("couldn't read this brindle app's engine build ({}): {e}", file.display())),
    };
    let build = text.trim();
    if build.is_empty() || build.len() > 64 || !build.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(format!(
            "this brindle app's engine names no build ({} holds {build:?}): download it again from pawdelta.com/brindle",
            file.display()
        ));
    }
    Ok(build.to_string())
}

/// Why a release build that should carry an engine can't run.
pub const MISSING_ENGINE: &str = "this brindle app is missing its engine (Contents/Resources/engine): \
                                  download it again from pawdelta.com/brindle";

/// Whether this build may start a brindle from PATH: only a dev build, or a
/// platform whose bundle doesn't carry an engine yet.
pub const PATH_FALLBACK: bool = cfg!(debug_assertions) || !cfg!(target_os = "macos");

/// The `brindle` to start: the bundled engine's launcher; without one, when
/// `path_fallback` allows it, `on_path`'s ($BRINDLE_BIN, else the first on PATH).
pub fn brindle_to_start(
    engine: Option<&Path>,
    path_fallback: bool,
    on_path: impl FnOnce() -> Option<PathBuf>,
) -> Result<PathBuf, String> {
    if let Some(engine) = engine {
        return Ok(engine.join(LAUNCHER));
    }
    if !path_fallback {
        return Err(MISSING_ENGINE.into());
    }
    on_path().ok_or_else(|| {
        "brindle isn't installed: no `brindle` on PATH, in uv's tool directory, in ~/.local/bin or in Homebrew's bin"
            .into()
    })
}

/// What `install_cli` did.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CliLink {
    /// The link: `<dir>/brindle`.
    pub path: PathBuf,
    /// What it points at: the bundled engine's launcher.
    pub target: PathBuf,
}

/// Whether the app runs from somewhere a link into it wouldn't outlive: the
/// mounted .dmg, or the random copy macOS runs a quarantined download from
/// (App Translocation) until it's moved to Applications.
pub fn runs_from_temporary_place(engine: &Path) -> bool {
    engine.starts_with("/Volumes") || engine.components().any(|c| c.as_os_str() == OsStr::new("AppTranslocation"))
}

/// Link `target` as `dir/brindle`. Already linked there: nothing to do.
/// Something else there (another brindle install, say uv's) is left alone
/// unless `replace`; a directory always is. The new link takes the old one's
/// place in a single rename, so `brindle` is never missing in between.
#[cfg(unix)]
pub fn link_cli(target: &Path, dir: &Path, replace: bool) -> Result<CliLink, String> {
    let err = |what: &Path, e: io::Error| format!("{}: {e}", what.display());
    std::fs::create_dir_all(dir).map_err(|e| err(dir, e))?;
    let path = dir.join("brindle");
    let done = CliLink { path: path.clone(), target: target.to_path_buf() };
    match std::fs::symlink_metadata(&path) {
        Ok(_) if std::fs::read_link(&path).is_ok_and(|t| t == target) => return Ok(done),
        Ok(meta) if meta.is_dir() => {
            return Err(format!("{} is a directory", path.display()));
        }
        Ok(_) if !replace => {
            return Err(format!(
                "{} already exists (another brindle install?): replace it to use the app's",
                path.display()
            ));
        }
        Ok(_) => {}
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(err(&path, e)),
    }
    let staged = dir.join(format!(".brindle.{}.tmp", std::process::id()));
    let _ = std::fs::remove_file(&staged);
    std::os::unix::fs::symlink(target, &staged).map_err(|e| err(&staged, e))?;
    std::fs::rename(&staged, &path).map_err(|e| {
        let _ = std::fs::remove_file(&staged);
        err(&path, e)
    })?;
    Ok(done)
}

/// Link the bundled brindle into `dir` (default `~/.local/bin`, where uv and
/// pipx put their commands, writable without an administrator), replacing a
/// `brindle` already there only when `replace`.
#[tauri::command]
pub fn install_cli(dir: Option<String>, replace: Option<bool>) -> Result<CliLink, String> {
    let engine = bundled_engine().ok_or(
        "this build of the app carries no engine to link (a dev build, or not macOS)",
    )?;
    if runs_from_temporary_place(&engine) {
        return Err("move brindle to Applications and open it from there first: \
                    a link to where it runs now would break"
            .into());
    }
    let dir = dir.map_or_else(|| crate::daemon::home_dir().join(".local/bin"), PathBuf::from);
    if !dir.is_absolute() {
        return Err(format!("{}: not an absolute path", dir.display()));
    }
    #[cfg(unix)]
    return link_cli(&engine.join(LAUNCHER), &dir, replace.unwrap_or(false));
    #[cfg(not(unix))]
    {
        let _ = replace;
        Err("linking brindle onto PATH is for macOS".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A fresh directory under target/, removed first if a run left it.
    fn scratch(name: &str) -> PathBuf {
        let dir = PathBuf::from(format!("target/engine-test/{name}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir.canonicalize().unwrap()
    }

    /// `root/brindle.app` with an executable and, when `with_engine`, an engine.
    fn fake_app(root: &Path, with_engine: bool) -> PathBuf {
        let contents = root.join("brindle.app/Contents");
        std::fs::create_dir_all(contents.join("MacOS")).unwrap();
        std::fs::write(contents.join("MacOS/brindle"), "").unwrap();
        if with_engine {
            std::fs::create_dir_all(contents.join("Resources/engine/bin")).unwrap();
            std::fs::write(contents.join("Resources/engine/bin/brindle"), "#!/bin/sh\n").unwrap();
        }
        contents.join("MacOS/brindle")
    }

    #[test]
    fn the_engine_is_found_beside_the_apps_executable() {
        let root = scratch("find");
        let exe = fake_app(&root, true);
        let engine = root.join("brindle.app/Contents/Resources/engine");
        assert_eq!(bundled_engine_at(&exe), Some(engine));
        let bare = scratch("find-bare");
        assert_eq!(bundled_engine_at(&fake_app(&bare, false)), None);
        // A dev build's executable, in target/debug, has none.
        assert_eq!(bundled_engine_at(&bare.join("target/debug/brindle-desktop")), None);
    }

    #[test]
    fn the_bundled_engine_is_started_and_path_only_when_allowed() {
        let engine = Path::new("/Applications/brindle.app/Contents/Resources/engine");
        let on_path = || Some(PathBuf::from("/opt/homebrew/bin/brindle"));
        // With an engine, its brindle, even where PATH would do.
        assert_eq!(
            brindle_to_start(Some(engine), true, on_path).unwrap(),
            engine.join("bin/brindle")
        );
        assert_eq!(
            brindle_to_start(Some(engine), false, || panic!("PATH looked up")).unwrap(),
            engine.join("bin/brindle")
        );
        // Without one: PATH's in a dev build, an error in a release.
        assert_eq!(
            brindle_to_start(None, true, on_path).unwrap(),
            PathBuf::from("/opt/homebrew/bin/brindle")
        );
        let err = brindle_to_start(None, false, || panic!("PATH looked up")).unwrap_err();
        assert!(err.contains("missing its engine"), "{err}");
        let err = brindle_to_start(None, true, || None).unwrap_err();
        assert!(err.contains("isn't installed"), "{err}");
    }

    #[test]
    // A constant per target, asserted for each.
    #[allow(clippy::assertions_on_constants)]
    fn a_release_on_macos_never_falls_back_to_path() {
        if cfg!(target_os = "macos") {
            assert_eq!(PATH_FALLBACK, cfg!(debug_assertions));
        } else {
            assert!(PATH_FALLBACK);
        }
    }

    #[test]
    fn a_link_into_the_dmg_or_a_translocated_copy_is_refused() {
        assert!(runs_from_temporary_place(Path::new("/Volumes/brindle/brindle.app/Contents/Resources/engine")));
        assert!(runs_from_temporary_place(Path::new(
            "/private/var/folders/x/T/AppTranslocation/1234/d/brindle.app/Contents/Resources/engine"
        )));
        assert!(!runs_from_temporary_place(Path::new("/Applications/brindle.app/Contents/Resources/engine")));
    }

    /// The launcher build_engine.sh puts in the engine, run through `link` (a
    /// symlink to it, as install_cli makes) with only `env`, in an engine of
    /// `build` (BUILD's text; none when None) whose python3 prints the PATH it
    /// got and its arguments.
    #[cfg(unix)]
    fn run_launcher(name: &str, env: &[(&str, &str)], build: Option<&str>) -> (PathBuf, Vec<String>) {
        use std::os::unix::fs::PermissionsExt;
        let root = scratch(name);
        let engine = root.join("brindle.app/Contents/Resources/engine");
        let exec = |path: &Path, text: &str| {
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, text).unwrap();
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
        };
        exec(&engine.join(LAUNCHER), include_str!("../../scripts/engine/brindle"));
        exec(&engine.join("python/bin/python3"), "#!/bin/sh\necho \"$PATH\"\necho \"$@\"\n");
        if let Some(build) = build {
            std::fs::write(engine.join(BUILD_FILE), build).unwrap();
        }
        let link = root.join("home/.local/bin/brindle");
        link_cli(&engine.join(LAUNCHER), link.parent().unwrap(), false).unwrap();
        let out = std::process::Command::new("/usr/bin/env")
            .arg("-i")
            .args(env.iter().map(|(k, v)| format!("{k}={v}")))
            .arg(&link)
            .args(["daemon", "--idle-exit"])
            .output()
            .unwrap();
        assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
        let lines = String::from_utf8(out.stdout).unwrap().lines().map(str::to_string).collect();
        (engine, lines)
    }

    #[cfg(unix)]
    #[test]
    fn the_launcher_runs_the_engines_python_with_its_bin_first_and_macos_own_dirs() {
        // No PATH at all (env -i): the engine's bin first, then whatever sh
        // makes up less its `.`, and the system's, so /usr/bin/security (the
        // Keychain) and ps are found.
        let (engine, out) = run_launcher("launcher-bare", &[], None);
        let bin = engine.join("bin").display().to_string();
        let dirs: Vec<&str> = out[0].split(':').collect();
        assert_eq!(dirs[0], bin);
        assert!(dirs.iter().all(|d| d.starts_with('/')), "{}", out[0]);
        for system in ["/usr/bin", "/bin", "/usr/sbin", "/sbin"] {
            assert_eq!(dirs.iter().filter(|d| **d == system).count(), 1, "{}", out[0]);
        }
        assert_eq!(out[1], "-I -B -m brindle daemon --idle-exit");

        // The app's PATH (setup::tool_path) without the system dirs: they're added last.
        let (engine, out) = run_launcher("launcher-app", &[("PATH", "/h/.local/bin:/opt/homebrew/bin")], None);
        let bin = engine.join("bin").display().to_string();
        assert_eq!(out[0], format!("{bin}:/h/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"));

        // A terminal's PATH keeps its order; the engine's bin isn't added twice.
        let engine = scratch("launcher-terminal").join("brindle.app/Contents/Resources/engine");
        let path = format!("{}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", engine.join("bin").display());
        let (_, out) = run_launcher("launcher-terminal", &[("PATH", &path)], None);
        assert_eq!(out[0], path);
    }

    #[cfg(unix)]
    #[test]
    fn the_launcher_puts_the_engines_build_on_the_command_line() {
        // The build daemon::reuse tells this engine's daemon by, ahead of brindle's own arguments.
        let (engine, out) = run_launcher("launcher-build", &[], Some("00b2c3\n"));
        assert_eq!(out[1], "-I -B -X brindle-build=00b2c3 -m brindle daemon --idle-exit");
        assert_eq!(require_build(&engine).unwrap(), "00b2c3");
        // A BUILD that isn't hex names no build: none on the line, none for the app.
        let (engine, out) = run_launcher("launcher-badbuild", &[], Some("x; rm -rf /\n"));
        assert_eq!(out[1], "-I -B -m brindle daemon --idle-exit");
        let why = require_build(&engine).unwrap_err();
        assert!(why.contains("names no build") && !why.contains("incomplete"), "{why}");
    }

    #[test]
    fn an_engine_is_incomplete_only_without_its_build_file() {
        let engine = scratch("build-missing");
        let why = require_build(&engine).unwrap_err();
        assert!(why.contains("incomplete") && why.contains(&engine.join(BUILD_FILE).display().to_string()), "{why}");

        // There but unreadable now (an app out of file descriptors reads EMFILE; a
        // directory in its place fails as surely): the real error, not "incomplete".
        let engine = scratch("build-unreadable");
        std::fs::create_dir_all(engine.join(BUILD_FILE)).unwrap();
        let why = require_build(&engine).unwrap_err();
        assert!(!why.contains("incomplete"), "{why}");
        assert!(why.starts_with("couldn't read this brindle app's engine build"), "{why}");
        let io = std::fs::read_to_string(engine.join(BUILD_FILE)).unwrap_err().to_string();
        assert!(why.ends_with(&io), "{why} carries {io:?}");
    }

    #[cfg(unix)]
    #[test]
    fn install_cli_links_the_engines_brindle_and_leaves_another_alone() {
        let root = scratch("link");
        let target = root.join("brindle.app/Contents/Resources/engine/bin/brindle");
        let dir = root.join("home/.local/bin"); // made when missing
        let link = link_cli(&target, &dir, false).unwrap();
        assert_eq!(link, CliLink { path: dir.join("brindle"), target: target.clone() });
        assert_eq!(std::fs::read_link(dir.join("brindle")).unwrap(), target);
        // Again: nothing to do, no replace needed.
        assert_eq!(link_cli(&target, &dir, false).unwrap(), link);

        // Another brindle there (uv's link, a script) stays unless replaced.
        std::fs::remove_file(dir.join("brindle")).unwrap();
        std::fs::write(dir.join("brindle"), "#!/bin/sh\necho uv's\n").unwrap();
        let err = link_cli(&target, &dir, false).unwrap_err();
        assert!(err.contains("already exists"), "{err}");
        assert_eq!(std::fs::read_to_string(dir.join("brindle")).unwrap(), "#!/bin/sh\necho uv's\n");
        link_cli(&target, &dir, true).unwrap();
        assert_eq!(std::fs::read_link(dir.join("brindle")).unwrap(), target);
        // No staged link is left behind.
        let names: Vec<_> = std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().file_name()).collect();
        assert_eq!(names, ["brindle"]);

        // A directory is never replaced.
        let other = root.join("other");
        std::fs::create_dir_all(other.join("brindle")).unwrap();
        let err = link_cli(&target, &other, true).unwrap_err();
        assert!(err.contains("is a directory"), "{err}");
        std::fs::remove_dir_all(&root).unwrap();
    }
}
