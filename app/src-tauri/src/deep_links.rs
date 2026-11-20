//! `brindle://` links from outside the app: a shared plan's "Open in brindle"
//! (brindle://plan/<org>/<plan>/<token>, brindle://plan/b/<token>; the URL
//! format is docs/deep-links.md).
//!
//! The scheme is registered with the system by the bundle (tauri.conf.json's
//! `plugins > deep-link`: the macOS Info.plist, the Windows installers, the
//! Linux .desktop file) and at run time on Windows and Linux (`register`).
//! The deep-link plugin hands over every link the app is opened with: macOS
//! sends it to the running app; on Windows and Linux a second launch passes
//! it to the first (the single-instance plugin) and exits.
//!
//! The shell never acts on a link. It keeps the ones with the `brindle`
//! scheme, up to a size, in a short queue and tells the app's page there are
//! some (`EVENT`); the page takes them (`deep_link_take`), checks each one
//! (app/src/lib/deeplink.ts) and opens what it names through the daemon, as
//! the account signed in. The queue holds a link the page isn't there for
//! yet (a cold start, a signed-out app at its sign-in) until it asks.

use std::sync::Mutex;

use tauri::{AppHandle, Emitter, Manager, Runtime};

/// What the page hears when links are waiting: no payload, it takes them.
pub const EVENT: &str = "deep-link";
/// The only scheme passed on.
const SCHEME: &str = "brindle";
/// The longest link kept: a plan link is well under it.
const MAX_LEN: usize = 512;
/// The most links kept until the page takes them: older ones go first.
const MAX_WAITING: usize = 8;

/// Links waiting for the page.
#[derive(Default)]
pub struct DeepLinks(Mutex<Vec<String>>);

impl DeepLinks {
    /// Keep the `brindle://` links among `urls`; true when any was kept.
    pub fn keep(&self, urls: impl IntoIterator<Item = String>) -> bool {
        let mut waiting = self.0.lock().unwrap_or_else(|e| e.into_inner());
        let mut kept = 0;
        for url in urls {
            if url.len() <= MAX_LEN && url.split_once("://").is_some_and(|(scheme, _)| scheme == SCHEME) {
                waiting.push(url);
                kept += 1;
            }
        }
        let extra = waiting.len().saturating_sub(MAX_WAITING);
        waiting.drain(..extra);
        kept > 0
    }

    /// Every link waiting, oldest first; the queue is left empty.
    pub fn take(&self) -> Vec<String> {
        std::mem::take(&mut *self.0.lock().unwrap_or_else(|e| e.into_inner()))
    }
}

/// Links arrived: kept for the page, which is told and brought to the front.
pub fn arrived<R: Runtime>(app: &AppHandle<R>, urls: impl IntoIterator<Item = String>) {
    if !app.state::<DeepLinks>().keep(urls) {
        return;
    }
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
    // The app's page only: a visual's child webview never hears of a link.
    let _ = app.emit_to("main", EVENT, ());
}

/// The links waiting, for the app's page (only_main guards it like every command).
#[tauri::command]
pub fn deep_link_take(links: tauri::State<'_, DeepLinks>) -> Vec<String> {
    links.take()
}

/// Wire the deep-link plugin up: the link the app was launched with, and every one after.
pub fn listen<R: Runtime>(app: &tauri::App<R>) {
    use tauri_plugin_deep_link::DeepLinkExt;

    // An AppImage or a portable copy has no installer to register the scheme: the running app does.
    #[cfg(any(target_os = "windows", target_os = "linux"))]
    let _ = app.deep_link().register_all();
    if let Ok(Some(urls)) = app.deep_link().get_current() {
        arrived(app.handle(), urls.into_iter().map(String::from));
    }
    let handle = app.handle().clone();
    app.deep_link().on_open_url(move |event| arrived(&handle, event.urls().into_iter().map(String::from)));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_brindle_links_of_a_plain_size_are_kept_and_taken_once() {
        let links = DeepLinks::default();
        let plan = "brindle://plan/b/dsl_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA".to_string();
        let long = format!("brindle://plan/b/{}", "a".repeat(MAX_LEN));
        let kept = links.keep([
            "https://plans.pawdelta.com/b/dsl_x".to_string(),
            "file:///etc/passwd".to_string(),
            "brindle-visual://localhost/x".to_string(),
            long,
            plan.clone(),
        ]);
        assert!(kept);
        assert_eq!(links.take(), [plan]);
        assert!(links.take().is_empty());
        assert!(!links.keep(["javascript:alert(1)".to_string()]));
    }

    #[test]
    fn a_page_that_never_asks_keeps_only_the_newest() {
        let links = DeepLinks::default();
        links.keep((0..MAX_WAITING + 3).map(|n| format!("brindle://plan/b/{n}")));
        let waiting = links.take();
        assert_eq!(waiting.len(), MAX_WAITING);
        assert_eq!(waiting.last().unwrap(), &format!("brindle://plan/b/{}", MAX_WAITING + 2));
    }
}
