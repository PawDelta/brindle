//! One fixed-size JSON line per process, updated in place as steps arrive.
//! Incomplete starts remain useful, including starts with no terminal to attach.
use std::fs::File;
use std::io::{Seek, SeekFrom, Write};
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

pub fn begin() {
    let _ = TIMING.set(Mutex::new(Timing::new()));
}

pub fn open(app: &tauri::AppHandle) {
    let Some(timing) = TIMING.get() else { return };
    let Ok(mut timing) = timing.lock() else { return };
    let Ok(dir) = app.path().app_data_dir() else { return };
    if std::fs::create_dir_all(&dir).is_err() { return; }
    let Ok(mut file) = File::options().create(true).truncate(false).write(true).open(dir.join("app-startup.log")) else { return };
    let Ok(offset) = file.seek(SeekFrom::End(0)) else { return };
    timing.output = Some((file, offset));
    timing.persist();
}

pub fn mark(step: &str) {
    if let Some(timing) = TIMING.get() {
        if let Ok(mut timing) = timing.lock() { timing.mark(step); }
    }
}

#[tauri::command]
pub fn app_startup_step(step: String) {
    mark(&step);
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

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
