//! App updates: the Tauri updater plugin against the static manifest the
//! release workflow publishes (`latest.json` under
//! pawdelta.com/brindle/downloads/desktop/), each bundle checked against the
//! minisign public key built into the app. A release-candidate build reads
//! the rc channel's `rc/latest.json` there instead (`endpoint`), and never by
//! itself: only brindle staff update it, with Settings' Update
//! (`app_update_install`, then `app_restart`).
//!
//! Staff can also install the latest release-branch build
//! (`app_release_build_install`): the release/desktop pipeline's own
//! `release/latest.json` there (`release_endpoint`), whose versions carry the
//! build as `<version>+release.<run>.<sha>` (`release_build_wanted`). Nobody
//! else's app ever reads that feed. Its bundles are signed with a separate
//! staff key (`STAFF_UPDATER_KEY`), and only that install trusts it.
//!
//! One check a launch, in the background once the daemon answers
//! (`wait_for_engine`), and Settings' About can check again (`app_update_check`);
//! an update found is installed and starts with the next launch, which the
//! Inbox says ("Restart to update", `UpdateStatus`); the app never restarts
//! by itself. None at all when brindle is air-gapped or the person turned
//! them off: `"app_updates": false` or `"airgap": true` in
//! `~/.brindle/config.json`, `BRINDLE_AIRGAP` in the environment, or the
//! engine saying so (`engine_says`). A build without a public key (a local
//! `tauri build`) never checks.

use std::cmp::Ordering;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use semver::Version;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::ipc::Channel;
use tauri::{AppHandle, Emitter, Manager, Runtime};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_updater::UpdaterExt;

use crate::daemon::Engine;

/// The engine's air-gap switch (brindle/airgap.py `ENV`), and the values it reads as on.
const AIRGAP_ENV: &str = "BRINDLE_AIRGAP";
const TRUE: [&str; 4] = ["1", "true", "yes", "on"];

/// Whether `~/.brindle/config.json` (its text, if any) and the environment's
/// `BRINDLE_AIRGAP` leave update checks on.
pub fn allowed(config: Option<&str>, airgap_env: Option<&str>) -> bool {
    if airgap_env.is_some_and(|v| TRUE.contains(&v.trim().to_ascii_lowercase().as_str())) {
        return false;
    }
    let config: Value = config
        .and_then(|text| serde_json::from_str(text).ok())
        .unwrap_or(Value::Null);
    config.get("app_updates") != Some(&Value::Bool(false))
        && config.get("airgap") != Some(&Value::Bool(true))
}

/// A query parameter's value, percent-encoded (a repo path may hold spaces).
fn query_value(text: &str) -> String {
    text.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' | b'/' => {
                (b as char).to_string()
            }
            _ => format!("%{b:02X}"),
        })
        .collect()
}

/// What the engine says of updates on this machine (`engine_says`).
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum EngineSays {
    On,
    /// Air-gapped: a repo's config or the daemon itself says `"airgap": true`.
    Off,
    /// No daemon answering yet (a launch starts it, a first run has none).
    NoAnswer,
}

/// What the engine, asked through `get` (a daemon GET: status and body),
/// says of updates. Air-gap can be armed by any repo's own
/// `.brindle/config.json`, which a freshly started daemon hasn't read, so its
/// `GET /v1/meta` alone proves nothing: every repo brindle knows
/// (`GET /v1/repos`) has its config loaded first (`GET /v1/config?repo=`,
/// which also arms the daemon's air-gap when that config asks), then the
/// daemon's own flag (the environment it runs with, anything armed) is read.
/// Only an explicit `"airgap": true` turns updates off: a repo whose config
/// can't be read is logged and passed over, since it can't arm the daemon
/// either, and one such repo mustn't hold back everyone's updates. A daemon
/// not answering `GET /v1/meta` is no answer at all (`wait_for_engine`).
pub fn engine_says(get: impl Fn(&str) -> Result<(u16, String), String>) -> EngineSays {
    let json = |path: &str| match get(path) {
        Ok((200, body)) => serde_json::from_str::<Value>(&body).ok(),
        _ => None,
    };
    if json("/v1/meta").is_none() {
        return EngineSays::NoAnswer;
    }
    let repos = json("/v1/repos").and_then(|v| v.as_array().cloned()).unwrap_or_else(|| {
        eprintln!("brindle: update check: couldn't list the repos to read their air-gap setting");
        vec![]
    });
    for repo in &repos {
        let Some(root) = repo.get("repo_root").and_then(Value::as_str) else {
            eprintln!("brindle: update check: a repo without a root: {repo}");
            continue;
        };
        let config = json(&format!("/v1/config?repo={}", query_value(root)));
        match config.and_then(|c| c.pointer("/repo/airgap").cloned()) {
            Some(Value::Bool(true)) => return EngineSays::Off,
            Some(Value::Bool(false)) => {}
            _ => eprintln!("brindle: update check: couldn't read {root}'s config; passed over"),
        }
    }
    match json("/v1/meta") {
        None => EngineSays::NoAnswer,
        Some(meta) if meta.get("airgap") == Some(&Value::Bool(true)) => EngineSays::Off,
        Some(_) => EngineSays::On,
    }
}

/// `engine_says`, asked again while the daemon doesn't answer and `again`
/// (which waits a while first) says there is still time: at launch the app
/// starts the daemon alongside, and its first start can take a while.
pub fn wait_for_engine(
    get: impl Fn(&str) -> Result<(u16, String), String>,
    mut again: impl FnMut() -> bool,
) -> EngineSays {
    loop {
        match engine_says(&get) {
            EngineSays::NoAnswer if again() => {}
            said => return said,
        }
    }
}

/// How long the launch check waits for the daemon to answer, and the pause
/// between asking it.
const PATIENCE: Duration = Duration::from_secs(300);
const PAUSE: Duration = Duration::from_secs(5);

/// Whether `version` is a release candidate's (`X.Y.Z-rc…`, as
/// app-release.yml tells them apart).
fn is_rc(version: &str) -> bool {
    version.split_once('-').is_some_and(|(_, pre)| pre.starts_with("rc"))
}

/// Whether a build at `version` installs updates by itself (at launch, and
/// from About's Check now). A release candidate (a beta) never does (owner
/// decision): staff update one from Settings (`app_update_install`), and
/// anyone else keeps it as installed. Nor does a staff release-branch build
/// (`<version>+release.<run>.<sha>`): staff move it on from Settings too.
pub fn checks_at_launch(version: &str) -> bool {
    let staff_build = Version::parse(version).ok().and_then(|v| release_run(&v)).is_some();
    !is_rc(version) && !staff_build
}

/// The version this launch was updated to, once: the first run of `current`
/// after `last` (the version last run, as `app-update.json` records it), when
/// it is newer. None on the first launch ever, or the same or an older build.
pub fn updated_to(last: Option<&str>, current: &Version) -> Option<String> {
    let last = Version::parse(last?).ok()?;
    newer(&last, current).then(|| current.to_string())
}

/// The manifest a build at `version` checks, given the stable one the config
/// names: a release candidate reads the rc channel's `rc/latest.json` beside
/// it, which each rc release overwrites; every other build reads `stable` as
/// it is.
pub fn endpoint(stable: &str, version: &str) -> String {
    match stable.strip_suffix("latest.json") {
        Some(base) if is_rc(version) => format!("{base}rc/latest.json"),
        _ => stable.to_string(),
    }
}

/// The release-branch builds' manifest beside the stable one the config
/// names (`release/latest.json`), which the release/desktop pipeline
/// overwrites after each successful build; none when `stable` isn't a
/// latest.json.
pub fn release_endpoint(stable: &str) -> Option<String> {
    stable.strip_suffix("latest.json").map(|base| format!("{base}release/latest.json"))
}

/// Whether a feed's `remote` build updates one at `current` (the launch
/// check and the rc Update): newer by SemVer precedence, build metadata
/// ignored. The plugin's own default also orders build metadata, as text, so
/// this is set explicitly: a staff release-branch build of 0.6.0
/// (`0.6.0+release.42.…`) never "updates" to plain 0.6.0, and 0.6.1 does.
pub fn newer(current: &Version, remote: &Version) -> bool {
    remote.cmp_precedence(current) == Ordering::Greater
}

/// A release-branch build's run number, from its version's build metadata
/// `release.<run>.<sha>` (the release/desktop pipeline's contract: the CI
/// run that built it, then 12 hex digits of its commit); none for any other
/// build.
fn release_run(version: &Version) -> Option<u64> {
    let mut parts = version.build.as_str().split('.');
    let (Some("release"), Some(run), Some(sha), None) =
        (parts.next(), parts.next(), parts.next(), parts.next())
    else {
        return None;
    };
    let sha_ok = sha.len() == 12 && sha.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'));
    if !sha_ok || run.is_empty() || !run.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    run.parse().ok()
}

/// Whether the release feed's build (`remote`, as the feed names it) is one
/// to install over this build at `current`. Anything but a release-branch
/// build there is an error. One newer by SemVer precedence is wanted, an
/// older one never (a stale feed rolls nothing back); at the same version,
/// only a later run than this build's own, or any run when this isn't a
/// release-branch build.
pub fn release_build_wanted(current: &Version, remote: &str) -> Result<bool, String> {
    let bad = || format!("the release feed names {remote:?}, not a release-branch build");
    let remote = Version::parse(remote).map_err(|_| bad())?;
    let run = release_run(&remote).ok_or_else(bad)?;
    Ok(match remote.cmp_precedence(current) {
        Ordering::Greater => true,
        Ordering::Less => false,
        Ordering::Equal => release_run(current).map_or(true, |mine| run > mine),
    })
}

/// Whether the stable feed's `remote` is one to install over this build at
/// `current` for "Return to stable": only a release-branch build returns
/// (anything else never), only to a plain release (no pre-release, no build
/// metadata), and only one at or above this build's major.minor.patch, which
/// build metadata doesn't change (`0.2.4+release.7.…` returns to 0.2.4, never
/// to 0.2.3).
pub fn stable_return_wanted(current: &Version, remote: &str) -> Result<bool, String> {
    let remote = Version::parse(remote)
        .ok()
        .filter(|v| v.pre.is_empty() && v.build.is_empty())
        .ok_or_else(|| format!("the stable feed names {remote:?}, not a stable release"))?;
    Ok(release_run(current).is_some() && remote.cmp_precedence(current) != Ordering::Less)
}

fn version<R: Runtime>(app: &AppHandle<R>) -> String {
    app.package_info().version.to_string()
}

/// The stable feed the config names, if any.
fn stable_endpoint<R: Runtime>(app: &AppHandle<R>) -> Option<String> {
    app.config()
        .plugins
        .0
        .get("updater")
        .and_then(|updater| updater.pointer("/endpoints/0"))
        .and_then(Value::as_str)
        .map(str::to_string)
}

/// The minisign public key release-branch builds are signed with, held only
/// in the `desktop-staff-release` environment (app/README.md, "Release-branch
/// builds"). Only the release feed's bundles are checked against it.
const STAFF_UPDATER_KEY: &str = include_str!("../staff-updater.pub");

/// The release feed's updater, at `feed`: every build it names comes back
/// (`release_build_wanted` decides), each bundle checked against the staff
/// key (`STAFF_UPDATER_KEY`), never the configured production one.
fn release_updater<R: Runtime>(
    app: &AppHandle<R>,
    feed: &str,
) -> Result<tauri_plugin_updater::Updater, String> {
    let url = feed.parse::<tauri::Url>().map_err(|e| e.to_string())?;
    app.updater_builder()
        .pubkey(STAFF_UPDATER_KEY.trim())
        .endpoints(vec![url])
        .map_err(|e| e.to_string())?
        .version_comparator(|_, _| true)
        .build()
        .map_err(|e| e.to_string())
}

/// The stable feed's updater for "Return to stable": every build it names
/// comes back (`stable_return_wanted` decides), each bundle checked against
/// the production key from the config, set explicitly here: never the staff
/// key, and an unsigned or staff-signed bundle fails the check.
fn stable_updater<R: Runtime>(
    app: &AppHandle<R>,
    feed: &str,
) -> Result<tauri_plugin_updater::Updater, String> {
    let url = feed.parse::<tauri::Url>().map_err(|e| e.to_string())?;
    let key = config_pubkey(app).ok_or("this build has no production update key")?;
    app.updater_builder()
        .pubkey(key)
        .endpoints(vec![url])
        .map_err(|e| e.to_string())?
        .version_comparator(|_, _| true)
        .build()
        .map_err(|e| e.to_string())
}

/// This build's own updater: its feed (`endpoint`), an update being `newer`,
/// each bundle checked against the configured (production) key.
fn app_updater<R: Runtime>(app: &AppHandle<R>) -> Result<tauri_plugin_updater::Updater, String> {
    let mut builder = app
        .updater_builder()
        .version_comparator(|current, release| newer(&current, &release.version));
    if let Some(stable) = stable_endpoint(app) {
        let feed = endpoint(&stable, &version(app));
        if feed != stable {
            let url = feed.parse::<tauri::Url>().map_err(|e| e.to_string())?;
            builder = builder.endpoints(vec![url]).map_err(|e| e.to_string())?;
        }
    }
    builder.build().map_err(|e| e.to_string())
}

/// The production minisign public key the config names, if any.
fn config_pubkey<R: Runtime>(app: &AppHandle<R>) -> Option<String> {
    app.config()
        .plugins
        .0
        .get("updater")
        .and_then(|updater| updater.get("pubkey"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|key| !key.is_empty())
        .map(str::to_string)
}

fn has_pubkey<R: Runtime>(app: &AppHandle<R>) -> bool {
    config_pubkey(app).is_some()
}

/// Whether the engine, asked through `get` as in `engine_says`, calls this
/// account brindle staff now: `GET /v1/debug?fresh=1`'s `staff`, the
/// backend asked then rather than the daemon's cached answer, which can
/// outlive a revocation by minutes. Fails closed (the engine also answers not
/// staff when it can't ask).
pub fn engine_says_staff(get: impl Fn(&str) -> Result<(u16, String), String>) -> bool {
    match get("/v1/debug?fresh=1") {
        Ok((200, body)) => serde_json::from_str::<Value>(&body)
            .ok()
            .and_then(|v| v.get("staff").cloned())
            == Some(Value::Bool(true)),
        _ => false,
    }
}

/// What every install shares (app state): one at a time, whichever starts
/// it (the launch check, Settings' Update or its release-branch install),
/// and none over one already installed until the app restarts into it.
#[derive(Default)]
pub struct Installs(Mutex<Install>);

#[derive(Clone, Default)]
enum Install {
    #[default]
    Idle,
    Busy,
    Installed(String),
}

/// What `Installs::begin` answers: go ahead (`Installing`, which settles
/// what came of it), another install is under way, or one is installed and
/// waits for the restart.
enum Begin<'a> {
    Go(Installing<'a>),
    Busy,
    Installed(String),
}

/// An install under way: `done` settles it; dropped (failed, or its task
/// gone), the next one may start.
struct Installing<'a>(&'a Installs);

impl Installs {
    fn state(&self) -> std::sync::MutexGuard<'_, Install> {
        self.0.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn begin(&self) -> Begin<'_> {
        let mut state = self.state();
        match state.clone() {
            Install::Idle => {
                *state = Install::Busy;
                Begin::Go(Installing(self))
            }
            Install::Busy => Begin::Busy,
            Install::Installed(version) => Begin::Installed(version),
        }
    }
}

impl Installing<'_> {
    /// The install answered `version`: when one was installed, it waits for the restart.
    fn done(self, version: Option<&String>) {
        if let Some(version) = version {
            *self.0.state() = Install::Installed(version.clone());
        }
    }
}

impl Drop for Installing<'_> {
    fn drop(&mut self) {
        let mut state = self.0.state();
        if matches!(*state, Install::Busy) {
            *state = Install::Idle;
        }
    }
}

/// Run `work` (an install, answering the version it installed) as the only
/// one: refused while another is under way, and answering the version
/// already installed, without running, when one waits for the restart.
async fn exclusive<F: Future<Output = Result<Option<String>, String>>>(
    installs: &Installs,
    work: impl FnOnce() -> F,
) -> Result<Option<String>, String> {
    let installing = match installs.begin() {
        Begin::Go(installing) => installing,
        Begin::Busy => return Err("an app update is already being installed".into()),
        Begin::Installed(version) => return Ok(Some(version)),
    };
    let got = work().await;
    installing.done(got.as_ref().ok().and_then(Option::as_ref));
    got
}

const UPDATES_OFF: &str = "updates are off on this machine (air-gapped or app_updates off)";

/// Whether this machine lets the app check now, with the reason when not: a
/// public key, and neither the config, the environment nor the engine (asked
/// off the async runtime, again while it doesn't answer and `again` says so:
/// `wait_for_engine`) turning updates off.
async fn may_check<R: Runtime>(
    app: &AppHandle<R>,
    brindle_home: &Path,
    again: impl FnMut() -> bool + Send + 'static,
) -> Result<(), String> {
    let config = std::fs::read_to_string(brindle_home.join("config.json")).ok();
    let airgap = std::env::var(AIRGAP_ENV).ok();
    if !has_pubkey(app) {
        return Err("this build has no update key (a local build)".into());
    }
    if !allowed(config.as_deref(), airgap.as_deref()) {
        return Err(UPDATES_OFF.into());
    }
    let engine = app.state::<Engine>().inner().clone();
    let said = tauri::async_runtime::spawn_blocking(move || wait_for_engine(|p| engine.get(p), again))
        .await
        .map_err(|e| e.to_string())?;
    match said {
        EngineSays::On => Ok(()),
        EngineSays::Off => Err(UPDATES_OFF.into()),
        EngineSays::NoAnswer => Err("the brindle engine isn't answering, so whether updates are on isn't known".into()),
    }
}

/// What `app-update.json` in the app's data directory keeps across launches:
/// the version last run, and when this machine last checked (seconds since
/// the epoch).
#[derive(Clone, Default, Serialize, Deserialize)]
struct Record {
    last_version: Option<String>,
    last_checked: Option<u64>,
}

/// What the app knows of updates beyond the install itself (app state): its
/// record, where it lives, and the version this launch was updated to until
/// the person dismisses it.
#[derive(Default)]
pub struct Seen(Mutex<SeenState>);

#[derive(Default)]
struct SeenState {
    record: Record,
    path: Option<PathBuf>,
    updated: Option<String>,
}

impl Seen {
    fn state(&self) -> std::sync::MutexGuard<'_, SeenState> {
        self.0.lock().unwrap_or_else(|e| e.into_inner())
    }
}

impl SeenState {
    /// Write the record: to a file of this process's own beside it, then
    /// renamed over it, so a reader never sees half of one.
    fn save(&self) {
        let Some(path) = &self.path else { return };
        let part = path.with_extension(format!("json.{}.part", std::process::id()));
        let written = (|| -> std::io::Result<()> {
            if let Some(dir) = path.parent() {
                std::fs::create_dir_all(dir)?;
            }
            std::fs::write(&part, serde_json::to_vec(&self.record)?)?;
            std::fs::rename(&part, path)
        })();
        if let Err(e) = written {
            let _ = std::fs::remove_file(&part);
            eprintln!("brindle: couldn't save {}: {e}", path.display());
        }
    }
}

/// App updates as Settings' About and the Inbox show them (`app_update_status`,
/// and the `app-update` event whenever they change).
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct UpdateStatus {
    version: String,
    /// Whether this build installs updates by itself (`checks_at_launch`).
    automatic: bool,
    /// An update installed, waiting for the restart.
    pending: Option<String>,
    /// The version this launch was updated to, until dismissed.
    updated: Option<String>,
    last_checked: Option<u64>,
    checking: bool,
}

const STATUS_EVENT: &str = "app-update";

fn status<R: Runtime>(app: &AppHandle<R>) -> UpdateStatus {
    let (pending, checking) = match app.state::<Installs>().state().clone() {
        Install::Idle => (None, false),
        Install::Busy => (None, true),
        Install::Installed(version) => (Some(version), false),
    };
    let seen = app.state::<Seen>();
    let seen = seen.state();
    UpdateStatus {
        version: version(app),
        automatic: checks_at_launch(&version(app)),
        pending,
        updated: seen.updated.clone(),
        last_checked: seen.record.last_checked,
        checking,
    }
}

fn announce<R: Runtime>(app: &AppHandle<R>) {
    let _ = app.emit(STATUS_EVENT, status(app));
}

/// Read the record, note whether this launch is the first on a newer version
/// (`updated_to`; only a build that updates by itself says so), and record
/// this version as the last run.
fn remember_launch<R: Runtime>(app: &AppHandle<R>) {
    let path = app.path().app_data_dir().ok().map(|dir| dir.join("app-update.json"));
    let record: Record = path
        .as_ref()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default();
    let current = app.package_info().version.clone();
    let updated = updated_to(record.last_version.as_deref(), &current)
        .filter(|_| checks_at_launch(&current.to_string()));
    let seen = app.state::<Seen>();
    let mut seen = seen.state();
    *seen = SeenState { record, path, updated };
    seen.record.last_version = Some(current.to_string());
    seen.save();
}

fn window_shown<R: Runtime>(app: &AppHandle<R>) -> bool {
    app.get_webview_window("main").is_some_and(|w| {
        w.is_visible().unwrap_or(false) && !w.is_minimized().unwrap_or(false)
    })
}

/// Check this build's feed and install an update found, once `may_check`
/// allows it, as the only install (`exclusive`), answering its version;
/// a check that reached the feed is recorded, and every change announced.
async fn check<R: Runtime>(
    app: &AppHandle<R>,
    brindle_home: &Path,
    again: impl FnMut() -> bool + Send + 'static,
) -> Result<Option<String>, String> {
    may_check(app, brindle_home, again).await?;
    let got = exclusive(&app.state::<Installs>(), || async {
        announce(app);
        install(app).await
    })
    .await;
    if got.is_ok() {
        let seen = app.state::<Seen>();
        let mut seen = seen.state();
        seen.record.last_checked = SystemTime::now().duration_since(UNIX_EPOCH).ok().map(|d| d.as_secs());
        seen.save();
    }
    announce(app);
    got
}

/// At launch: remember it (`remember_launch`), then check once, in the
/// background, waiting for the daemon the app starts alongside (`PATIENCE`);
/// install an update found, which starts with the next launch. The Inbox
/// says so (the `app-update` event); a notification only when the window
/// isn't showing. Never restarts the app. A build that doesn't update by
/// itself makes no check (`checks_at_launch`).
pub fn check_in_background<R: Runtime>(app: &AppHandle<R>, brindle_home: &Path) {
    remember_launch(app);
    if !checks_at_launch(&version(app)) {
        return;
    }
    let (app, brindle_home) = (app.clone(), brindle_home.to_path_buf());
    tauri::async_runtime::spawn(async move {
        let until = Instant::now() + PATIENCE;
        let again = move || {
            let left = until.saturating_duration_since(Instant::now());
            std::thread::park_timeout(PAUSE.min(left));
            !left.is_zero()
        };
        match check(&app, &brindle_home, again).await {
            Ok(Some(version)) if !window_shown(&app) => {
                let _ = app
                    .notification()
                    .builder()
                    .title("brindle updated")
                    .body(format!("brindle {version} is installed. Restart brindle to update."))
                    .show();
            }
            Ok(_) => {}
            Err(e) => eprintln!("brindle: update check: {e}"),
        }
    });
}

/// App updates now (Settings' About and the Inbox).
#[tauri::command]
pub fn app_update_status<R: Runtime>(app: AppHandle<R>) -> UpdateStatus {
    status(&app)
}

/// About's Check now: the launch check, now, without waiting for a daemon
/// that doesn't answer. Only for a build that updates by itself.
#[tauri::command]
pub async fn app_update_check<R: Runtime>(app: AppHandle<R>) -> Result<UpdateStatus, String> {
    if !checks_at_launch(&version(&app)) {
        return Err("this build doesn't update by itself".into());
    }
    check(&app, &crate::daemon::brindle_home(), || false).await?;
    Ok(status(&app))
}

/// The person has read "Updated to brindle X": it goes.
#[tauri::command]
pub fn app_update_dismiss<R: Runtime>(app: AppHandle<R>) {
    app.state::<Seen>().state().updated = None;
    announce(&app);
}

/// Settings' staff-only Update (owner decision; how an rc build updates):
/// check this build's feed now and install an update found, answering its
/// version, or none when this is the latest. Like the launch check off when
/// updates are, and refused unless the backend calls the account staff
/// (`staff_gate`); one install at a time (`exclusive`).
#[tauri::command]
pub async fn app_update_install<R: Runtime>(app: AppHandle<R>) -> Result<Option<String>, String> {
    staff_gate_now(&app).await?;
    exclusive(&app.state::<Installs>(), || install(&app)).await
}

/// Restart the app into the update `app_update_install` or
/// `app_release_build_install` installed.
#[tauri::command]
pub fn app_restart<R: Runtime>(app: AppHandle<R>) {
    app.restart();
}

/// Whether this machine lets staff install an update from Settings now (the
/// Update and the release-branch install): the public key,
/// `~/.brindle/config.json` (its text, if any), the environment's
/// `BRINDLE_AIRGAP` and the engine (asked through `get`: `engine_says`, which
/// must answer On) all leave updates on, and only then, the one call that
/// leaves the machine, the backend calls the account staff
/// (`engine_says_staff`). An air-gapped machine asks no backend. Fails
/// closed, with the reason.
pub fn staff_gate(
    get: impl Fn(&str) -> Result<(u16, String), String>,
    config: Option<&str>,
    airgap_env: Option<&str>,
    pubkey: bool,
) -> Result<(), String> {
    if !pubkey || !allowed(config, airgap_env) || engine_says(&get) != EngineSays::On {
        return Err(UPDATES_OFF.into());
    }
    if !engine_says_staff(&get) {
        return Err("only brindle staff can update this build from here".into());
    }
    Ok(())
}

/// `staff_gate` on this machine as it is now (the engine asked off the
/// async runtime).
async fn staff_gate_now<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let engine = app.state::<Engine>().inner().clone();
    let config = std::fs::read_to_string(crate::daemon::brindle_home().join("config.json")).ok();
    let airgap = std::env::var(AIRGAP_ENV).ok();
    let pubkey = has_pubkey(app);
    tauri::async_runtime::spawn_blocking(move || {
        staff_gate(|p| engine.get(p), config.as_deref(), airgap.as_deref(), pubkey)
    })
    .await
    .map_err(|e| e.to_string())?
}

/// How a release-branch install goes, for Settings: the build found, the
/// bytes downloaded so far (of `total`, when the server says), installing.
#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum ReleaseProgress {
    Found { version: String },
    Downloading { downloaded: u64, total: Option<u64> },
    Installing,
}

/// The release feed as `install_release_build` uses it: the updater plugin
/// in the app (`PluginFeed`), a fake in tests.
trait ReleaseFeed {
    /// The build the feed names for this machine, if any.
    async fn check(&mut self) -> Result<Option<String>, String>;
    /// That build's bundle, checked against its signature; `progress` gets
    /// each chunk's size and the total, when known.
    async fn download(
        &mut self,
        progress: impl FnMut(u64, Option<u64>) + Send,
    ) -> Result<Vec<u8>, String>;
    /// Install the downloaded bundle (its signature checked again).
    fn install(&mut self, bytes: &[u8]) -> Result<(), String>;
}

/// Install the release feed's build when `release_build_wanted` says so over
/// this one at `current`, answering its version, or none. `gate` (staff, and
/// updates on) runs before the feed is read at all and again right before
/// installing, so staff revoked or air-gap armed during the download stops
/// it. Past the first gate it is the only install (`exclusive`): another
/// under way refuses it, and one waiting for the restart answers instead.
async fn install_release_build<G: Future<Output = Result<(), String>>>(
    installs: &Installs,
    gate: impl Fn() -> G,
    current: &Version,
    feed: &mut impl ReleaseFeed,
    report: impl FnMut(ReleaseProgress) + Send,
) -> Result<Option<String>, String> {
    install_build(installs, gate, current, feed, release_build_wanted, report).await
}

/// `install_release_build` with the decision (`wanted`, given this build and
/// the feed's version) left to the caller: "Return to stable" uses
/// `stable_return_wanted`.
async fn install_build<G: Future<Output = Result<(), String>>>(
    installs: &Installs,
    gate: impl Fn() -> G,
    current: &Version,
    feed: &mut impl ReleaseFeed,
    wanted: impl Fn(&Version, &str) -> Result<bool, String>,
    mut report: impl FnMut(ReleaseProgress) + Send,
) -> Result<Option<String>, String> {
    gate().await?;
    exclusive(installs, || async {
        let Some(version) = feed.check().await? else {
            return Ok(None);
        };
        if !wanted(current, &version)? {
            return Ok(None);
        }
        report(ReleaseProgress::Found { version: version.clone() });
        let mut downloaded = 0;
        let bytes = feed
            .download(|chunk, total| {
                downloaded += chunk;
                report(ReleaseProgress::Downloading { downloaded, total });
            })
            .await?;
        gate().await?;
        report(ReleaseProgress::Installing);
        feed.install(&bytes)?;
        Ok(Some(version))
    })
    .await
}

/// The release feed through the updater plugin: the compiled stable feed's
/// `release_endpoint`, with the staff public key (`release_updater`).
struct PluginFeed<R: Runtime> {
    app: AppHandle<R>,
    /// Read the stable feed with the production key ("Return to stable")
    /// instead of the release feed with the staff key.
    stable: bool,
    update: Option<tauri_plugin_updater::Update>,
}

impl<R: Runtime> ReleaseFeed for PluginFeed<R> {
    async fn check(&mut self) -> Result<Option<String>, String> {
        let stable = stable_endpoint(&self.app).ok_or("this build names no update feed")?;
        let updater = if self.stable {
            stable_updater(&self.app, &stable)?
        } else {
            let feed = release_endpoint(&stable).ok_or("this build's update feed has no release feed")?;
            release_updater(&self.app, &feed)?
        };
        self.update = updater.check().await.map_err(|e| e.to_string())?;
        Ok(self.update.as_ref().map(|update| update.version.clone()))
    }

    async fn download(
        &mut self,
        mut progress: impl FnMut(u64, Option<u64>) + Send,
    ) -> Result<Vec<u8>, String> {
        let update = self.update.as_ref().ok_or("no release-branch build to download")?;
        update
            .download(|chunk, total| progress(chunk as u64, total), || {})
            .await
            .map_err(|e| e.to_string())
    }

    fn install(&mut self, bytes: &[u8]) -> Result<(), String> {
        let update = self.update.as_ref().ok_or("no release-branch build to install")?;
        update.install(bytes).map_err(|e| e.to_string())
    }
}

/// Settings' staff-only "Install latest release-branch build": install the
/// release feed's build (`install_release_build`), reporting how it goes on
/// `on_progress`, answering its version, or none when this build is it or
/// newer. Refused unless the engine calls the account staff, and off when
/// updates are; it takes no feed or version from the webview.
#[tauri::command]
pub async fn app_release_build_install<R: Runtime>(
    app: AppHandle<R>,
    on_progress: Channel<ReleaseProgress>,
) -> Result<Option<String>, String> {
    let current = app.package_info().version.clone();
    let mut feed = PluginFeed { app: app.clone(), stable: false, update: None };
    let report = |progress| {
        let _ = on_progress.send(progress);
    };
    let installs = app.state::<Installs>();
    install_release_build(&installs, || staff_gate_now(&app), &current, &mut feed, report).await
}

/// Settings' staff-only "Return to stable": install the stable feed's build
/// over this release-branch build even at the same version
/// (`stable_return_wanted`), its bundle checked against the production key.
/// Same gate and one-install rule as the release-branch install.
/// It installs only `version`, the one `app_stable_check` showed the person
/// to confirm: a feed naming another by now is an error, so they confirm again.
#[tauri::command]
pub async fn app_return_to_stable<R: Runtime>(
    app: AppHandle<R>,
    version: String,
    on_progress: Channel<ReleaseProgress>,
) -> Result<Option<String>, String> {
    let current = app.package_info().version.clone();
    let mut feed = PluginFeed { app: app.clone(), stable: true, update: None };
    let report = |progress| {
        let _ = on_progress.send(progress);
    };
    let installs = app.state::<Installs>();
    let wanted = |current: &Version, found: &str| {
        if found != version {
            return Err(format!("the stable release is now {found}, not {version}; check again"));
        }
        stable_return_wanted(current, found)
    };
    install_build(&installs, || staff_gate_now(&app), &current, &mut feed, wanted, report).await
}

/// Settings' "Return to stable", first step: the stable version
/// `stable_return_wanted` would install over this build, or none. Same staff
/// gate, same production-key feed; nothing is downloaded.
#[tauri::command]
pub async fn app_stable_check<R: Runtime>(app: AppHandle<R>) -> Result<Option<String>, String> {
    staff_gate_now(&app).await?;
    let current = app.package_info().version.clone();
    let mut feed = PluginFeed { app: app.clone(), stable: true, update: None };
    let Some(found) = feed.check().await? else { return Ok(None) };
    Ok(stable_return_wanted(&current, &found)?.then_some(found))
}

/// Check this build's feed (`endpoint`) and install an update found
/// (`newer`), answering its version.
async fn install<R: Runtime>(app: &AppHandle<R>) -> Result<Option<String>, String> {
    let found = app_updater(app)?
        .check()
        .await
        .map_err(|e| e.to_string())?;
    let Some(update) = found else { return Ok(None) };
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|e| e.to_string())?;
    Ok(Some(update.version))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};
    use std::task::Poll;

    #[test]
    fn config_and_environment_can_turn_checks_off() {
        for (config, env, on) in [
            (None, None, true),
            (Some("{}"), None, true),
            (Some("not json"), None, true),
            (Some(r#"{"app_updates": true}"#), None, true),
            (Some(r#"{"app_updates": false}"#), None, false),
            (Some(r#"{"airgap": true}"#), None, false),
            (Some(r#"{"airgap": false}"#), Some("0"), true),
            (None, Some("1"), false),
            (None, Some(" Yes "), false),
        ] {
            assert_eq!(allowed(config, env), on, "{config:?} {env:?}");
        }
    }

    #[test]
    fn a_release_candidate_reads_the_rc_feed_and_a_release_the_stable_one() {
        let stable = "https://pawdelta.com/brindle/downloads/desktop/latest.json";
        let rc = "https://pawdelta.com/brindle/downloads/desktop/rc/latest.json";
        for (version, feed) in [
            ("0.1.0", stable),
            ("1.2.3", stable),
            ("0.2.0-beta.1", stable),
            ("0.1.0-rc.7", rc),
            ("0.2.0-rc1", rc),
            ("0.2.0-rc.1+build.5", rc),
        ] {
            assert_eq!(endpoint(stable, version), feed, "{version}");
        }
        // A config endpoint that isn't a latest.json is left alone.
        assert_eq!(endpoint("https://x/feed", "0.1.0-rc.1"), "https://x/feed");
    }

    #[test]
    fn only_a_release_installs_updates_by_itself_and_says_once_it_was_updated() {
        for (version, automatic) in [
            ("0.1.0", true),
            ("0.2.0-beta.1", true),
            ("0.1.0-rc.7", false),
            // A staff release-branch build: staff move it on from Settings.
            ("0.6.0+release.42.0123456789ab", false),
        ] {
            assert_eq!(checks_at_launch(version), automatic, "{version}");
        }
        let now = Version::parse("0.6.1").expect("version");
        for (last, updated) in [(Some("0.6.0"), Some("0.6.1")), (Some("0.6.1"), None), (Some("0.7.0"), None), (None, None)] {
            assert_eq!(updated_to(last, &now).as_deref(), updated, "{last:?}");
        }
    }

    #[test]
    fn only_an_engine_calling_the_account_staff_allows_a_manual_update() {
        for (answer, staff) in [
            (Ok((200, r#"{"staff": true, "enabled": false}"#.to_string())), true),
            (Ok((200, r#"{"staff": false, "enabled": true}"#.to_string())), false),
            (Ok((200, r#"{"staff": "yes"}"#.to_string())), false),
            (Ok((404, r#"{"staff": true}"#.to_string())), false),
            (Ok((200, "not json".to_string())), false),
            (Err("refused".to_string()), false),
        ] {
            let asked = RefCell::new(vec![]);
            let get = |p: &str| {
                asked.borrow_mut().push(p.to_string());
                answer.clone()
            };
            assert_eq!(engine_says_staff(get), staff, "{answer:?}");
            assert_eq!(*asked.borrow(), ["/v1/debug?fresh=1"]);
        }
    }

    #[test]
    fn the_configured_endpoint_is_the_stable_feed() {
        let conf: Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).expect("tauri.conf.json");
        let stable = conf.pointer("/plugins/updater/endpoints/0").and_then(Value::as_str);
        assert_eq!(stable, Some("https://pawdelta.com/brindle/downloads/desktop/latest.json"));
    }

    /// A minisign signature naming the key `pubkey` (as tauri.conf.json holds
    /// one: base64 of the key file), base64 as a manifest holds it, that
    /// matches no bytes: checking it fails, on the key ID first.
    fn signature_by(pubkey: &str) -> String {
        use base64::Engine;
        let b64 = base64::engine::general_purpose::STANDARD;
        let file = String::from_utf8(b64.decode(pubkey).expect("key")).expect("key");
        let key = b64.decode(file.lines().nth(1).expect("key line")).expect("key");
        let mut sig = b"ED".to_vec();
        sig.extend_from_slice(&key[2..10]);
        sig.extend_from_slice(&[0; 64]);
        let text = format!(
            "untrusted comment: test\n{}\ntrusted comment: version:999.0.0\n{}\n",
            b64.encode(sig),
            b64.encode([0; 64])
        );
        b64.encode(text)
    }

    #[test]
    fn only_a_release_build_install_checks_bundles_with_the_staff_updater_key() {
        let conf: Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).expect("tauri.conf.json");
        let mut context = tauri::test::mock_context(tauri::test::noop_assets());
        context.config_mut().plugins.0.insert("updater".into(), conf["plugins"]["updater"].clone());
        let app = tauri::test::mock_builder()
            .plugin(tauri_plugin_updater::Builder::new().build())
            .build(context)
            .expect("app");
        let production = conf["plugins"]["updater"]["pubkey"].as_str().expect("pubkey");
        let staff = include_str!("../staff-updater.pub").trim();
        assert_ne!(production, staff);
        let release = release_updater(app.handle(), "https://example.invalid/latest.json").unwrap();
        let update = app_updater(app.handle()).unwrap();
        for (what, updater, trusted) in [("release build", &release, staff), ("update", &update, production)] {
            for key in [staff, production] {
                let manifest = serde_json::json!({
                    "version": "999.0.0",
                    "url": "https://example.invalid/bundle.app.tar.gz",
                    "signature": signature_by(key),
                });
                let found = updater.restore_update(manifest).unwrap().expect(what);
                let wrong_key = matches!(
                    found.verify(b"bundle"),
                    Err(tauri_plugin_updater::Error::Minisign(minisign_verify::Error::UnexpectedKeyId))
                );
                assert_eq!(wrong_key, key != trusted, "{what}");
            }
        }
    }

    /// A fresh daemon as the engine answers: meta says what is armed, which
    /// is nothing until a repo's config with `"airgap": true` is loaded.
    struct FakeEngine {
        repos: &'static str,
        configs: Vec<(&'static str, u16, &'static str)>,
        env_airgap: bool,
        armed: RefCell<bool>,
        asked: RefCell<Vec<String>>,
        /// `GET /v1/debug?fresh=1`'s answer: whether the backend calls the
        /// account staff now. Plain `/v1/debug` answers the daemon's cache,
        /// which still says staff.
        debug: RefCell<(u16, &'static str)>,
    }

    impl FakeEngine {
        fn get(&self, path: &str) -> Result<(u16, String), String> {
            self.asked.borrow_mut().push(path.to_string());
            if path == "/v1/debug" {
                return Ok((200, STAFF.to_string()));
            }
            if path == "/v1/debug?fresh=1" {
                let (status, body) = *self.debug.borrow();
                return Ok((status, body.to_string()));
            }
            if path == "/v1/meta" {
                let on = self.env_airgap || *self.armed.borrow();
                return Ok((200, format!(r#"{{"api_version": "1.0", "airgap": {on}}}"#)));
            }
            if path == "/v1/repos" {
                return Ok((200, self.repos.to_string()));
            }
            for (repo, status, body) in &self.configs {
                if path == format!("/v1/config?repo={repo}") {
                    if body.contains(r#""airgap": true"#) {
                        *self.armed.borrow_mut() = true;
                    }
                    return Ok((*status, body.to_string()));
                }
            }
            Err(format!("unreachable: {path}"))
        }
    }

    fn engine(repos: &'static str, configs: Vec<(&'static str, u16, &'static str)>) -> FakeEngine {
        FakeEngine {
            repos,
            configs,
            env_airgap: false,
            armed: RefCell::new(false),
            asked: RefCell::new(vec![]),
            debug: RefCell::new((200, STAFF)),
        }
    }

    const STAFF: &str = r#"{"staff": true, "enabled": false}"#;
    const OFF: &str = r#"{"user": {}, "repo": {"airgap": false}, "keys": []}"#;
    const ON: &str = r#"{"user": {}, "repo": {"airgap": true}, "keys": []}"#;

    #[test]
    fn a_cold_daemon_with_an_air_gapped_repo_allows_no_check() {
        let repos = r#"[{"repo_root": "/w/open"}, {"repo_root": "/w/closed off"}]"#;
        let fake = engine(repos, vec![("/w/open", 200, OFF), ("/w/closed%20off", 200, ON)]);
        assert_eq!(engine_says(|p| fake.get(p)), EngineSays::Off);
        assert!(fake.asked.borrow().contains(&"/v1/config?repo=/w/closed%20off".to_string()));
        // Nor does staff's install from Settings: the backend is never asked who the account is.
        let fresh = engine(repos, vec![("/w/open", 200, OFF), ("/w/closed%20off", 200, ON)]);
        assert!(staff_gate(|p| fresh.get(p), Some("{}"), None, true).is_err());
        assert!(!fresh.asked.borrow().iter().any(|p| p.starts_with("/v1/debug")));
    }

    #[test]
    fn a_daemon_not_ready_at_launch_still_gets_the_check() {
        let fake = engine(r#"[{"repo_root": "/w/open"}]"#, vec![("/w/open", 200, OFF)]);
        let starting = Cell::new(2);
        let get = |p: &str| match starting.get() {
            0 => fake.get(p),
            n => {
                starting.set(n - 1);
                Err(format!("{}: still listening for the first time", crate::daemon::ENGINE_STARTING))
            }
        };
        let waits = Cell::new(0);
        let said = wait_for_engine(get, || {
            waits.set(waits.get() + 1);
            true
        });
        assert_eq!((said, waits.get()), (EngineSays::On, 2));
        // A daemon that never answers: no check, once the patience is spent.
        assert_eq!(wait_for_engine(|_| Err("refused".to_string()), || false), EngineSays::NoAnswer);
    }

    #[test]
    fn only_an_explicit_air_gap_turns_the_engine_against_updates() {
        let one = r#"[{"repo_root": "/w/open"}]"#;
        for (repos, configs, env_airgap, said) in [
            (one, vec![("/w/open", 200, OFF)], false, EngineSays::On),
            ("[]", vec![], true, EngineSays::Off),
            // A repo config, or the repo list, that can't be read is passed over (and logged).
            (one, vec![("/w/open", 500, OFF)], false, EngineSays::On),
            (r#"{"error": "agent_caller"}"#, vec![], false, EngineSays::On),
            (r#"[{"name": "x"}]"#, vec![], false, EngineSays::On),
        ] {
            let fake = FakeEngine { env_airgap, ..engine(repos, configs.clone()) };
            assert_eq!(engine_says(|p| fake.get(p)), said, "{repos} {configs:?} {env_airgap}");
        }
    }

    #[test]
    fn the_launch_check_never_takes_a_staff_build_back_to_its_own_version() {
        let v = |s: &str| Version::parse(s).expect("version");
        for (current, remote, wanted) in [
            ("0.6.0+release.42.0123456789ab", "0.6.0", false),
            ("0.6.0+release.42.0123456789ab", "0.6.1", true),
            ("0.1.0", "0.1.0-rc.10", false),
        ] {
            assert_eq!(newer(&v(current), &v(remote)), wanted, "{current} -> {remote}");
        }
    }

    #[test]
    fn a_release_build_installs_only_over_an_older_version_or_an_earlier_run() {
        for (current, remote, wanted) in [
            // The same version: a later run (a new commit) only, never the same or an earlier one.
            ("0.1.0-rc.9+release.41.0123456789ab", "0.1.0-rc.9+release.42.fedcba987654", Ok(true)),
            ("0.1.0-rc.9+release.42.0123456789ab", "0.1.0-rc.9+release.42.0123456789ab", Ok(false)),
            ("0.1.0-rc.9+release.42.0123456789ab", "0.1.0-rc.9+release.9.fedcba987654", Ok(false)),
            // The same version, and this isn't a release-branch build.
            ("0.1.0-rc.9", "0.1.0-rc.9+release.1.0123456789ab", Ok(true)),
            // Precedence first, pre-release included, whatever the run.
            ("0.1.0-rc.9+release.42.0123456789ab", "0.1.0+release.1.0123456789ab", Ok(true)),
            ("0.1.0", "0.1.0-rc.9+release.99.0123456789ab", Ok(false)),
            // Anything but a release-branch build in the feed.
            ("0.1.0", "0.2.0", Err(())),
            ("0.1.0", "0.2.0+release.1.0123456789", Err(())),
            ("0.1.0", "0.2.0+release.x.0123456789ab", Err(())),
            ("0.1.0", "not a version", Err(())),
        ] {
            let current = Version::parse(current).expect("version");
            let got = release_build_wanted(&current, remote).map_err(|_| ());
            assert_eq!(got, wanted, "{current} -> {remote}");
        }
    }

    #[test]
    fn only_a_release_build_returns_to_a_stable_at_or_above_its_version() {
        for (current, remote, wanted) in [
            ("0.2.4+release.7.0123456789ab", "0.2.4", Ok(true)),
            ("0.2.4+release.7.0123456789ab", "0.2.5", Ok(true)),
            ("0.2.4+release.7.0123456789ab", "0.2.3", Ok(false)),
            // Not a release-branch build: never.
            ("0.2.4", "0.2.4", Ok(false)),
            ("0.2.4", "0.2.5", Ok(false)),
            // The feed must name a plain release.
            ("0.2.4+release.7.0123456789ab", "0.2.5-rc.1", Err(())),
            ("0.2.4+release.7.0123456789ab", "0.2.5+release.8.0123456789ab", Err(())),
        ] {
            let current = Version::parse(current).expect("version");
            let got = stable_return_wanted(&current, remote).map_err(|_| ());
            assert_eq!(got, wanted, "{current} -> {remote}");
        }
    }

    /// A release feed naming `version`, recording what was asked of it;
    /// `meanwhile` runs halfway through the download, which then waits
    /// (pending, never woken: the test polls it again) while `paused` holds.
    struct FakeFeed<'a> {
        version: Option<&'static str>,
        calls: Vec<&'static str>,
        meanwhile: Box<dyn Fn() + 'a>,
        paused: Option<&'a Cell<bool>>,
    }

    impl ReleaseFeed for FakeFeed<'_> {
        async fn check(&mut self) -> Result<Option<String>, String> {
            self.calls.push("check");
            Ok(self.version.map(str::to_string))
        }

        async fn download(
            &mut self,
            mut progress: impl FnMut(u64, Option<u64>) + Send,
        ) -> Result<Vec<u8>, String> {
            self.calls.push("download");
            progress(6, Some(10));
            (self.meanwhile)();
            let paused = self.paused;
            std::future::poll_fn(|_| match paused.is_some_and(Cell::get) {
                true => Poll::Pending,
                false => Poll::Ready(()),
            })
            .await;
            progress(4, Some(10));
            Ok(vec![0; 10])
        }

        fn install(&mut self, _: &[u8]) -> Result<(), String> {
            self.calls.push("install");
            Ok(())
        }
    }

    const THIS: &str = "0.1.0-rc.9+release.41.0123456789ab";
    const NEXT: &str = "0.1.0-rc.9+release.42.fedcba987654";

    #[test]
    fn only_staff_with_updates_on_install_a_release_build_gated_again_before_installing() {
        type Meanwhile = fn(&FakeEngine, &RefCell<&'static str>);
        let nothing: Meanwhile = |_, _| {};
        let revoke: Meanwhile = |fake, _| *fake.debug.borrow_mut() = (200, r#"{"staff": false}"#);
        let air_gap: Meanwhile = |_, config| *config.borrow_mut() = r#"{"airgap": true}"#;
        let all: &[&str] = &["check", "download", "install"];
        // The last column: how often the backend was asked whether the account is staff.
        for (debug, config, env_airgap, names, meanwhile, installed, calls, staff_asks) in [
            // Staff, updates on: a later run of this same version installs.
            ((200, STAFF), "{}", false, NEXT, nothing, Ok(Some(NEXT)), all, 2),
            // Not staff, or no answer saying so: the feed is never read.
            ((200, r#"{"staff": false, "enabled": true}"#), "{}", false, NEXT, nothing, Err(()), &[][..], 1),
            ((404, r#"{"error": "not found"}"#), "{}", false, NEXT, nothing, Err(()), &[], 1),
            ((200, "not json"), "{}", false, NEXT, nothing, Err(()), &[], 1),
            // Updates off by the config or by the engine: neither the feed nor the backend asked.
            ((200, STAFF), r#"{"app_updates": false}"#, false, NEXT, nothing, Err(()), &[], 0),
            ((200, STAFF), "{}", true, NEXT, nothing, Err(()), &[], 0),
            // Staff revoked, or air-gap armed, during the download: nothing installs,
            // and once air-gapped the backend isn't asked again.
            ((200, STAFF), "{}", false, NEXT, revoke, Err(()), &["check", "download"], 2),
            ((200, STAFF), "{}", false, NEXT, air_gap, Err(()), &["check", "download"], 1),
            // This build already: nothing downloads; a feed naming no release build is refused.
            ((200, STAFF), "{}", false, THIS, nothing, Ok(None), &["check"], 1),
            ((200, STAFF), "{}", false, "0.2.0", nothing, Err(()), &["check"], 1),
        ] {
            let fake = FakeEngine { env_airgap, debug: RefCell::new(debug), ..engine("[]", vec![]) };
            let config = RefCell::new(config);
            let mut feed = FakeFeed {
                version: Some(names),
                calls: vec![],
                meanwhile: Box::new(|| meanwhile(&fake, &config)),
                paused: None,
            };
            // The gate as the command builds it, on this fake machine.
            let gate = || std::future::ready(staff_gate(|p| fake.get(p), Some(*config.borrow()), None, true));
            let current = Version::parse(THIS).expect("version");
            let mut seen = vec![];
            let got = tauri::async_runtime::block_on(install_release_build(
                &Installs::default(),
                gate,
                &current,
                &mut feed,
                |p| seen.push(p),
            ));
            let what = format!("{debug:?} {config:?} {env_airgap} {names}");
            assert_eq!(got.map_err(|_| ()), installed.map(|v| v.map(str::to_string)), "{what}");
            assert_eq!(feed.calls, calls, "{what}");
            let asked = fake.asked.borrow().iter().filter(|p| p.starts_with("/v1/debug")).count();
            assert_eq!(asked, staff_asks, "{what}");
            if feed.calls == all {
                assert_eq!(
                    seen,
                    [
                        ReleaseProgress::Found { version: NEXT.into() },
                        ReleaseProgress::Downloading { downloaded: 6, total: Some(10) },
                        ReleaseProgress::Downloading { downloaded: 10, total: Some(10) },
                        ReleaseProgress::Installing,
                    ]
                );
            }
        }
    }

    #[test]
    fn no_install_starts_while_one_downloads_or_awaits_the_restart() {
        let fake = engine("[]", vec![]);
        let gate = || std::future::ready(staff_gate(|p| fake.get(p), Some("{}"), None, true));
        let current = Version::parse(THIS).expect("version");
        let installs = Installs::default();
        let paused = Cell::new(true);
        let feed = |paused| FakeFeed { version: Some(NEXT), calls: vec![], meanwhile: Box::new(|| {}), paused };
        let (mut first, mut second, mut third) = (feed(Some(&paused)), feed(None), feed(None));
        let (got, again, after) = tauri::async_runtime::block_on(async {
            let mut installing =
                std::pin::pin!(install_release_build(&installs, gate, &current, &mut first, |_| {}));
            // The first install runs into its download, and waits there.
            std::future::poll_fn(|cx| Poll::Ready(installing.as_mut().poll(cx).is_pending())).await;
            // Meanwhile Settings asks again: refused, its feed never read; the launch check stands aside.
            let again = install_release_build(&installs, gate, &current, &mut second, |_| {}).await;
            assert!(matches!(installs.begin(), Begin::Busy));
            paused.set(false);
            let got = installing.await;
            // Installed and waiting for the restart: neither install path starts another.
            let after = install_release_build(&installs, gate, &current, &mut third, |_| {}).await;
            assert_eq!(exclusive(&installs, || async { unreachable!("an install over one awaiting the restart") }).await, Ok(Some(NEXT.to_string())));
            (got, again, after)
        });
        assert_eq!(got, Ok(Some(NEXT.to_string())));
        assert_eq!(first.calls, ["check", "download", "install"]);
        assert!(again.is_err());
        assert_eq!(after, Ok(Some(NEXT.to_string())));
        assert!(second.calls.is_empty() && third.calls.is_empty());
        assert!(matches!(installs.begin(), Begin::Installed(v) if v == NEXT));
    }

    #[test]
    fn a_failed_install_leaves_the_next_free_to_start() {
        let installs = Installs::default();
        let failed = tauri::async_runtime::block_on(exclusive(&installs, || async { Err("offline".to_string()) }));
        assert!(failed.is_err());
        assert!(matches!(installs.begin(), Begin::Go(_)));
    }
}
