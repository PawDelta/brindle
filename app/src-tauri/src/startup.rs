//! Startup marks stay in memory until the engine confirms a staff account.
//! Incomplete starts remain useful, including starts with no terminal to attach.
use std::fs::File;
use std::io::{Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::{Mutex, OnceLock};
use std::time::{Instant, SystemTime, UNIX_EPOCH};
use tauri::Manager;

const STEPS: [&str; 8] = [
    "webview", "daemon", "meta", "snapshot", "sessions_feed",
    "shell_painted", "panes_ready", "terminal_attached",
];

struct Timing<W> {
    since: Instant,
    started: u128,
    steps: [Option<u128>; 8],
    output: Option<(W, u64)>,
}

impl<W: Write + Seek> Timing<W> {
    fn new() -> Self {
        Self {
            since: Instant::now(),
            started: SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis(),
            steps: [None; 8],
            output: None,
        }
    }

    fn line(&self) -> String {
        let mut line = format!("{{\"started_unix_ms\":{},\"elapsed_ms\":{{", self.started);
        for (i, step) in STEPS.iter().enumerate() {
            if i > 0 { line.push(','); }
            let value = self.steps[i].map(|ms| ms.to_string()).unwrap_or_else(|| "null".into());
            // u128 has at most 39 digits: the record never changes length.
            line.push_str(&format!("\"{step}\":{value:>39}"));
        }
        line.push_str("}}\n");
        line
    }

    fn persist(&mut self) {
        let line = self.line();
        if let Some((file, offset)) = self.output.as_mut() {
            // Best effort only: diagnostics never change startup's outcome.
            let _ = file.seek(SeekFrom::Start(*offset))
                .and_then(|_| file.write_all(line.as_bytes()))
                .and_then(|_| file.flush());
        }
    }

    fn mark(&mut self, step: &str) {
        let Some(index) = STEPS.iter().position(|known| *known == step) else { return };
        if self.steps[index].is_some() { return; }
        self.steps[index] = Some(self.since.elapsed().as_millis());
        self.persist();
    }
}

static TIMING: OnceLock<Mutex<Timing<File>>> = OnceLock::new();
static AUTHORIZING: OnceLock<()> = OnceLock::new();

pub fn begin() {
    let _ = TIMING.set(Mutex::new(Timing::new()));
}

pub fn open(app: &tauri::AppHandle) {
    if AUTHORIZING.set(()).is_err() { return; }
    let app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        // Never hold the timing lock across daemon discovery or the backend:
        // marks and startup continue even when the account cannot be checked.
        let staff = observe_after_recovery(
            |path| app.state::<crate::daemon::Engine>().get(path),
            || std::thread::park_timeout(std::time::Duration::from_millis(250)),
        );
        let Ok(dir) = app.path().app_data_dir() else { return };
        let Some(timing) = TIMING.get() else { return };
        let Ok(mut timing) = timing.lock() else { return };
        timing.open_in(&dir, staff);
    });
}

fn observe_after_recovery(
    get: impl Fn(&str) -> Result<(u16, String), String>,
    mut pending: impl FnMut(),
) -> bool {
    loop {
        if let Some(staff) = staff_after_recovery(&get) { return staff; }
        // Only active recovery keeps authorization pending. Failed or unknown
        // answers finish without a file; this observer never holds startup.
        pending();
    }
}

fn staff_after_recovery(get: impl Fn(&str) -> Result<(u16, String), String>) -> Option<bool> {
    let resuming = match get("/v1/meta") {
        Ok((200, body)) => serde_json::from_str::<serde_json::Value>(&body).ok()
            .and_then(|v| v.get("resuming").and_then(serde_json::Value::as_bool)),
        _ => None,
    };
    match resuming {
        Some(true) => None,
        Some(false) => Some(crate::updates::engine_says_staff(get)),
        None => Some(false),
    }
}

impl Timing<File> {
    fn open_in(&mut self, dir: &Path, staff: bool) {
        if !staff { return; }
        if std::fs::create_dir_all(dir).is_err() { return; }
        let Ok(mut file) = File::options().create(true).truncate(false).write(true).open(dir.join("app-startup.log")) else { return };
        let Ok(offset) = file.seek(SeekFrom::End(0)) else { return };
        self.output = Some((file, offset));
        self.persist();
    }
}

pub fn mark(step: &str) {
    if let Some(timing) = TIMING.get() {
        if let Ok(mut timing) = timing.lock() { timing.mark(step); }
    }
}

#[tauri::command]
pub fn app_startup_step(app: tauri::AppHandle, step: String) {
    mark(&step);
    if step == "panes_ready" { open(&app); }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn deadline_during_recovery_keeps_startup_authorization_pending() {
        let dir = std::env::current_dir().unwrap().join(format!("target/startup-deadline-{}", std::process::id()));
        let mut timing = Timing::<File>::new();
        timing.mark("panes_ready"); // the launch page's deadline settles while the engine is still resuming
        let resuming = std::cell::Cell::new(true);
        let observations = std::cell::Cell::new(0);
        let staff = observe_after_recovery(|path| {
            if path == "/v1/meta" {
                observations.set(observations.get() + 1);
                Ok((200, format!(r#"{{"resuming":{}}}"#, resuming.get())))
            } else {
                assert!(!resuming.get(), "account check must follow recovery");
                Ok((200, r#"{"staff":true}"#.into()))
            }
        }, || {
            assert!(!dir.exists() && timing.output.is_none());
            resuming.set(false); // the background observer's next turn, without another startup mark
        });
        timing.open_in(&dir, staff);
        assert!(timing.output.is_some(), "completed recovery must publish the buffered staff record");
        assert_eq!(observations.get(), 2);
        drop(timing);
        let record: serde_json::Value = serde_json::from_slice(&std::fs::read(dir.join("app-startup.log")).unwrap()).unwrap();
        assert!(record["elapsed_ms"]["panes_ready"].is_number());
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn non_staff_startup_leaves_no_file() {
        let dir = std::env::current_dir().unwrap().join(format!("target/startup-account-{}", std::process::id()));
        let mut timing = Timing::<File>::new();
        timing.mark("webview");
        let non_staff = observe_after_recovery(|path| Ok((200, if path == "/v1/meta" {
            r#"{"resuming":false}"#.into()
        } else { r#"{"staff":false}"#.into() })), || panic!("non-staff must finish without a file"));
        timing.open_in(&dir, non_staff);
        assert!(!dir.exists() && timing.output.is_none());
        let unknown = observe_after_recovery(|path| if path == "/v1/meta" {
            Ok((200, r#"{"resuming":false}"#.into()))
        } else { Err("offline".into()) }, || panic!("failed staff check must finish without a file"));
        timing.open_in(&dir, unknown);
        assert!(!dir.exists());
        let no_answer = observe_after_recovery(|path| Ok((200, if path == "/v1/meta" {
            r#"{"resuming":false}"#.into()
        } else { r#"{"staff":null}"#.into() })), || panic!("unknown staff must finish without a file"));
        timing.open_in(&dir, no_answer);
        assert!(!dir.exists());
        let recovering = staff_after_recovery(|path| {
            assert_eq!(path, "/v1/meta");   // no backend account request while recovery runs
            Ok((200, r#"{"resuming":true}"#.into()))
        });
        assert_eq!(recovering, None);
        assert!(!dir.exists());
        let staff = staff_after_recovery(|path| Ok((200, if path == "/v1/meta" {
            r#"{"resuming":false}"#.into()
        } else { r#"{"staff":true}"#.into() })));
        timing.open_in(&dir, staff.unwrap());
        drop(timing);
        let record: serde_json::Value = serde_json::from_slice(&std::fs::read(dir.join("app-startup.log")).unwrap()).unwrap();
        assert!(record["elapsed_ms"]["webview"].is_number());
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn startup_record_keeps_one_line_and_first_times() {
        let mut timing = Timing::new();
        let previous = b"previous start\n";
        timing.output = Some((Cursor::new(previous.to_vec()), previous.len() as u64));
        timing.persist();
        let size = timing.output.as_ref().unwrap().0.get_ref().len();
        let partial: serde_json::Value = serde_json::from_slice(&timing.output.as_ref().unwrap().0.get_ref()[previous.len()..]).unwrap();
        assert!(partial["elapsed_ms"]["terminal_attached"].is_null());
        timing.mark("not a milestone: secret");
        for step in STEPS {
            timing.mark(step);
            let first = timing.steps;
            timing.mark(step);
            assert_eq!(timing.steps, first);
            let bytes = timing.output.as_ref().unwrap().0.get_ref();
            assert_eq!(bytes.len(), size);
            assert!(bytes.starts_with(previous));
            assert_eq!(bytes.iter().filter(|b| **b == b'\n').count(), 2);
            let record: serde_json::Value = serde_json::from_slice(&bytes[previous.len()..]).unwrap();
            assert!(record["elapsed_ms"][step].is_number());
            assert!(!String::from_utf8_lossy(bytes).contains("secret"));
        }
    }
}
