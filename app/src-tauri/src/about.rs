//! Settings' About: the app's version and the license files the bundle carries
//! as resources (tauri.conf.json's bundle.resources): brindle's LICENSE and
//! SCHEDULE-A, and the third-party notices scripts/notices.mjs generates. A
//! file the build doesn't have (a dev build's resource dir) comes back as
//! None, for the page to say so.

use serde::Serialize;
use tauri::{AppHandle, Manager, Runtime};

const FILES: [&str; 3] = ["LICENSE", "SCHEDULE-A", "THIRD_PARTY_NOTICES.txt"];

#[derive(Serialize)]
pub struct AboutFile {
    name: &'static str,
    text: Option<String>,
}

#[derive(Serialize)]
pub struct About {
    version: String,
    files: Vec<AboutFile>,
}

#[tauri::command]
pub fn about_read<R: Runtime>(app: AppHandle<R>) -> About {
    let dir = app.path().resource_dir().ok();
    About {
        version: app.package_info().version.to_string(),
        files: FILES
            .iter()
            .map(|&name| AboutFile {
                name,
                text: dir.as_ref().and_then(|d| std::fs::read_to_string(d.join(name)).ok()),
            })
            .collect(),
    }
}
