//! App updates: the Tauri updater plugin against the static manifest the
//! release workflow publishes (`latest.json` under
//! pawdelta.com/brindle/downloads/desktop/), each bundle checked against the
//! minisign public key built into the app. A release-candidate build reads
//! the rc channel's `rc/latest.json` there instead (`endpoint`), and never by
//! itself: only brindle staff update it, with Settings' Update
//! (`app_update_install`, then `app_restart`).
//!
//! One check a launch, and none at all when brindle is air-gapped or the
//! person turned them off: `"app_updates": false` or `"airgap": true` in
//! `~/.brindle/config.json`, `BRINDLE_AIRGAP` in the environment, or the
//! engine saying so (`engine_allows`). A build without a public key (a local
//! `tauri build`) never checks.

use std::path::Path;

use serde_json::Value;
use tauri::{AppHandle, Manager, Runtime};
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

/// Whether the engine, asked through `get` (a daemon GET: status and body),
/// lets this launch check. Air-gap can be armed by any repo's own
/// `.brindle/config.json`, which a freshly started daemon hasn't read, so its
/// `GET /v1/meta` alone proves nothing: every repo brindle knows
/// (`GET /v1/repos`) has its config loaded (`GET /v1/config?repo=`, which also
/// arms the daemon's air-gap when that config asks) and must say
/// `"airgap": false`; then the daemon's own flag (the environment it runs
/// with, anything armed) must be off too. Fails closed: a request that fails
/// or answers anything but a 200 of the expected shape means no check.
pub fn engine_allows(get: impl Fn(&str) -> Result<(u16, String), String>) -> bool {
    let json = |path: &str| match get(path) {
        Ok((200, body)) => serde_json::from_str::<Value>(&body).ok(),
        _ => None,
    };
    let Some(repos) = json("/v1/repos").and_then(|v| v.as_array().cloned()) else {
        return false;
    };
    for repo in &repos {
        let Some(root) = repo.get("repo_root").and_then(Value::as_str) else {
            return false;
        };
        let config = json(&format!("/v1/config?repo={}", query_value(root)));
        if config.and_then(|c| c.pointer("/repo/airgap").cloned()) != Some(Value::Bool(false)) {
            return false;
        }
    }
    json("/v1/meta").and_then(|meta| meta.get("airgap").cloned()) == Some(Value::Bool(false))
}

/// Whether `version` is a release candidate's (`X.Y.Z-rc…`, as
/// app-release.yml tells them apart).
fn is_rc(version: &str) -> bool {
    version.split_once('-').is_some_and(|(_, pre)| pre.starts_with("rc"))
}

/// Whether a build at `version` checks for an update at launch by itself. A
/// release candidate (a beta) never does (owner decision): staff update one
/// from Settings (`app_update_install`), and anyone else keeps it as installed.
pub fn checks_at_launch(version: &str) -> bool {
    !is_rc(version)
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

fn version<R: Runtime>(app: &AppHandle<R>) -> String {
    app.package_info().version.to_string()
}

fn has_pubkey<R: Runtime>(app: &AppHandle<R>) -> bool {
    app.config()
        .plugins
        .0
        .get("updater")
        .and_then(|updater| updater.get("pubkey"))
        .and_then(Value::as_str)
        .is_some_and(|key| !key.trim().is_empty())
}

/// Whether the engine, asked through `get` as in `engine_allows`, calls this
/// account brindle staff (`GET /v1/debug`'s `staff`). Fails closed.
pub fn engine_says_staff(get: impl Fn(&str) -> Result<(u16, String), String>) -> bool {
    match get("/v1/debug") {
        Ok((200, body)) => serde_json::from_str::<Value>(&body)
            .ok()
            .and_then(|v| v.get("staff").cloned())
            == Some(Value::Bool(true)),
        _ => false,
    }
}

/// Whether this machine lets the app check at all: a public key, and neither
/// the config, the environment nor the engine (asked off the async runtime)
/// turning updates off.
async fn may_check<R: Runtime>(app: &AppHandle<R>, brindle_home: &Path) -> bool {
    let config = std::fs::read_to_string(brindle_home.join("config.json")).ok();
    let airgap = std::env::var(AIRGAP_ENV).ok();
    if !has_pubkey(app) || !allowed(config.as_deref(), airgap.as_deref()) {
        return false;
    }
    let engine = app.state::<Engine>().inner().clone();
    let asked =
        tauri::async_runtime::spawn_blocking(move || engine_allows(|path| engine.get(path))).await;
    matches!(asked, Ok(true))
}

/// Check once, in the background; install an update found and say it starts
/// with the next launch. A release candidate makes no check (`checks_at_launch`).
pub fn check_in_background<R: Runtime>(app: &AppHandle<R>, brindle_home: &Path) {
    if !checks_at_launch(&version(app)) {
        return;
    }
    let (app, brindle_home) = (app.clone(), brindle_home.to_path_buf());
    tauri::async_runtime::spawn(async move {
        if !may_check(&app, &brindle_home).await {
            return;
        }
        match install(&app).await {
            Ok(Some(version)) => {
                let _ = app
                    .notification()
                    .builder()
                    .title("brindle updated")
                    .body(format!(
                        "brindle {version} is installed and starts the next time you open the app."
                    ))
                    .show();
            }
            Ok(None) => {}
            Err(e) => eprintln!("brindle: update check failed: {e}"),
        }
    });
}

/// Settings' staff-only Update (owner decision; how an rc build updates):
/// check this build's feed now and install an update found, answering its
/// version, or none when this is the latest. Refused unless the engine calls
/// the account staff, and like the launch check off when updates are.
#[tauri::command]
pub async fn app_update_install<R: Runtime>(app: AppHandle<R>) -> Result<Option<String>, String> {
    let engine = app.state::<Engine>().inner().clone();
    let staff = tauri::async_runtime::spawn_blocking(move || engine_says_staff(|p| engine.get(p)))
        .await
        .unwrap_or(false);
    if !staff {
        return Err("only brindle staff can update this build from here".into());
    }
    if !may_check(&app, &crate::daemon::brindle_home()).await {
        return Err("updates are off on this machine (air-gapped or app_updates off)".into());
    }
    install(&app).await
}

/// Restart the app into the update `app_update_install` installed.
#[tauri::command]
pub fn app_restart<R: Runtime>(app: AppHandle<R>) {
    app.restart();
}

/// Check this build's feed (`endpoint`) and install an update found,
/// answering its version.
async fn install<R: Runtime>(app: &AppHandle<R>) -> Result<Option<String>, String> {
    let mut builder = app.updater_builder();
    let stable = app
        .config()
        .plugins
        .0
        .get("updater")
        .and_then(|updater| updater.pointer("/endpoints/0"))
        .and_then(Value::as_str);
    if let Some(stable) = stable {
        let feed = endpoint(stable, &version(app));
        if feed != stable {
            let url = feed.parse::<tauri::Url>().map_err(|e| e.to_string())?;
            builder = builder.endpoints(vec![url]).map_err(|e| e.to_string())?;
        }
    }
    let found = builder
        .build()
        .map_err(|e| e.to_string())?
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
    use std::cell::RefCell;

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
    fn an_rc_build_makes_no_launch_check_and_reads_the_rc_feed() {
        let stable = "https://pawdelta.com/brindle/downloads/desktop/latest.json";
        assert!(!checks_at_launch("0.1.0-rc.7"));
        assert!(endpoint(stable, "0.1.0-rc.7").ends_with("/desktop/rc/latest.json"));
        assert!(checks_at_launch("0.1.0"));
        assert!(checks_at_launch("0.2.0-beta.1"));
        assert_eq!(endpoint(stable, "0.1.0"), stable);
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
            assert_eq!(*asked.borrow(), ["/v1/debug"]);
        }
    }

    #[test]
    fn the_configured_endpoint_is_the_stable_feed() {
        let conf: Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).expect("tauri.conf.json");
        let stable = conf.pointer("/plugins/updater/endpoints/0").and_then(Value::as_str);
        assert_eq!(stable, Some("https://pawdelta.com/brindle/downloads/desktop/latest.json"));
    }

    /// A fresh daemon as the engine answers: meta says what is armed, which
    /// is nothing until a repo's config with `"airgap": true` is loaded.
    struct FakeEngine {
        repos: &'static str,
        configs: Vec<(&'static str, u16, &'static str)>,
        env_airgap: bool,
        armed: RefCell<bool>,
        asked: RefCell<Vec<String>>,
    }

    impl FakeEngine {
        fn get(&self, path: &str) -> Result<(u16, String), String> {
            self.asked.borrow_mut().push(path.to_string());
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
        }
    }

    const OFF: &str = r#"{"user": {}, "repo": {"airgap": false}, "keys": []}"#;
    const ON: &str = r#"{"user": {}, "repo": {"airgap": true}, "keys": []}"#;

    #[test]
    fn a_cold_daemon_with_an_air_gapped_repo_allows_no_check() {
        let repos = r#"[{"repo_root": "/w/open"}, {"repo_root": "/w/closed off"}]"#;
        let fake = engine(repos, vec![("/w/open", 200, OFF), ("/w/closed%20off", 200, ON)]);
        assert!(!engine_allows(|p| fake.get(p)));
        assert!(fake.asked.borrow().contains(&"/v1/config?repo=/w/closed%20off".to_string()));
    }

    #[test]
    fn the_engine_allows_a_check_only_when_every_answer_says_so() {
        let one = r#"[{"repo_root": "/w/open"}]"#;
        for (repos, configs, env_airgap, on) in [
            (one, vec![("/w/open", 200, OFF)], false, true),
            ("[]", vec![], false, true),
            ("[]", vec![], true, false),
            (one, vec![("/w/open", 500, OFF)], false, false),
            (one, vec![("/w/open", 200, r#"{"user": {}}"#)], false, false),
            (one, vec![], false, false),
            (r#"{"error": "agent_caller"}"#, vec![], false, false),
            (r#"[{"name": "x"}]"#, vec![], false, false),
        ] {
            let fake = FakeEngine { env_airgap, ..engine(repos, configs.clone()) };
            assert_eq!(engine_allows(|p| fake.get(p)), on, "{repos} {configs:?} {env_airgap}");
        }
    }
}
