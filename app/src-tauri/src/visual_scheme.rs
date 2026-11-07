//! The visual sandbox: where an agent's HTML (a `publish_visual` page) renders
//! in the app without reaching anything but the shell.
//!
//! Each open visual gets a child webview of the main window (Tauri's
//! multiwebview), never an iframe of the app's page, so a visual that hangs
//! (a busy loop) can't freeze the app's own webview. The child loads its page
//! from the `brindle-visual://` scheme, which this module serves: the shell
//! fetches the revision from the daemon with its token (the page never holds
//! one) and answers it under a Content Security Policy of `default-src 'none'`
//! with inline script and style only, so the page itself can make no request.
//! What CSP doesn't cover, the shell denies elsewhere:
//!
//! - navigation (`location`, a link, a form, a meta refresh, `window.open`):
//!   the webview's navigation hook allows only `about:blank` and the visual's
//!   own URL, and the new-window hook denies everything;
//! - any other request (a leak CSP missed, a bug): a platform request blocker,
//!   deny by default: a WebKit content rule list on macOS and WebKitGTK, a
//!   WebView2 resource filter on Windows;
//! - WebRTC, which no header reaches: the injected prelude deletes
//!   `RTCPeerConnection` and its kin in every frame before page script runs;
//! - the app's own IPC: every command is refused unless the asking webview is
//!   `main` (lib.rs `only_main`, before dispatch: Tauri skips its ACL on the
//!   app's own commands for local origins, and a custom scheme is one), and
//!   the child's label matches no capability (capabilities/default.json names
//!   the `main` webview, not the window), so plugin commands fail the ACL too;
//! - another visual's page, or the daemon: a request on the scheme is served
//!   only when the asking webview is bound to exactly the id and revision in
//!   the path, with no path beyond them, and the daemon is reachable only
//!   through this handler, by the shell.
//!
//! What a visual says comes back through one bridge: the prelude forwards the
//! page's `parent.postMessage({type: "choice", option})` (a top-level webview
//! is its own parent, so the message lands on the page itself), `brindle.esc`
//! and `brindle.snapshot` as a POST to `<own URL>/message`, which this handler
//! relays to the app's main webview as a `visual:message` event. The host
//! talks to the page with [`post`], a `message` event whose origin is
//! `brindle://host`. The hostile pages in tests/visual_probes prove all of the
//! above against a listener that must see nothing.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::http::{header, Request, Response, StatusCode};
use tauri::webview::{NewWindowResponse, PageLoadEvent, WebviewBuilder};
use tauri::{AppHandle, Emitter, EventTarget, LogicalPosition, LogicalSize, Manager, Runtime, State, Url, Webview, WebviewUrl, Window};

/// The scheme. On Windows a custom scheme is `http://brindle-visual.localhost`.
pub const SCHEME: &str = "brindle-visual";

/// The most a revision (or a snapshot posted back) may be: the engine's cap.
pub const MAX_BYTES: usize = 2 * 1024 * 1024;

/// The message types a page may send the host. Anything else is dropped.
const FORWARDED: [&str; 3] = ["choice", "brindle.esc", "brindle.snapshot"];

/// The event the main webview gets for each message a visual sends.
pub const MESSAGE_EVENT: &str = "visual:message";
/// The event for a navigation or window the sandbox denied: the visual is marked failed.
pub const BLOCKED_EVENT: &str = "visual:blocked";
/// The event for a visual's page having loaded (or failed to).
pub const LOADED_EVENT: &str = "visual:loaded";
/// The event for a visual whose request blocker couldn't be put in place: its
/// page is never loaded (the webview stays blank), and the reader shows why.
pub const FAILED_EVENT: &str = "visual:failed";

/// The origin a page sees itself at, as the platform's webview spells it:
/// what the page's URL, its policy and the request blocker use.
pub fn page_origin() -> String {
    if cfg!(windows) {
        format!("http://{SCHEME}.localhost")
    } else {
        handler_origin()
    }
}

/// The origin requests reach the scheme handler at, on every platform: wry
/// undoes Windows' `http://<scheme>.localhost` spelling before the handler.
pub fn handler_origin() -> String {
    format!("{SCHEME}://localhost")
}

/// What a visual webview shows: a revision of a session's visual, or a kept one.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum VisualRef {
    Revision { id: String, rev: u32 },
    Kept { id: String },
}

impl VisualRef {
    /// The daemon route holding the HTML (the wave-1 contract).
    pub fn daemon_route(&self) -> String {
        match self {
            VisualRef::Revision { id, rev } => format!("/v1/visuals/{id}/r/{rev}"),
            VisualRef::Kept { id } => format!("/v1/kept-visuals/{id}/html"),
        }
    }

    /// The path under the scheme's origin.
    pub fn path(&self) -> String {
        match self {
            VisualRef::Revision { id, rev } => format!("/{id}/r{rev}"),
            VisualRef::Kept { id } => format!("/kept/{id}"),
        }
    }

    /// The page's URL in the child webview.
    pub fn url(&self) -> Url {
        Url::parse(&format!("{}{}", page_origin(), self.path())).expect("a visual URL")
    }

    /// The child webview's label: one per open revision.
    pub fn label(&self) -> String {
        match self {
            VisualRef::Revision { id, rev } => format!("visual:{id}:r{rev}"),
            VisualRef::Kept { id } => format!("visual:kept:{id}"),
        }
    }

    pub fn id(&self) -> &str {
        match self {
            VisualRef::Revision { id, .. } | VisualRef::Kept { id } => id,
        }
    }

    /// An id as the engine mints them: never a path, a dot or a space.
    pub fn valid_id(id: &str) -> bool {
        !id.is_empty()
            && id.len() <= 64
            && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
            && id != "kept"
    }

    /// The reference, checked: the id's charset and a revision of at most nine digits.
    pub fn checked(self) -> Result<Self, String> {
        if !Self::valid_id(self.id()) {
            return Err(format!("not a visual id: {:?}", self.id()));
        }
        if let VisualRef::Revision { rev, .. } = &self {
            if *rev == 0 || *rev > 999_999_999 {
                return Err(format!("not a revision: {rev}"));
            }
        }
        Ok(self)
    }
}

/// What a request on the scheme asks for.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Ask {
    /// `GET <path>`: the page.
    Page(VisualRef),
    /// `POST <path>/message`: a message from the page to the host.
    Message(VisualRef),
}

/// Parse a request's method and URL as the handler receives it: the path is
/// `/<id>/r<n>` or `/kept/<id>`, optionally followed by `/message` for a
/// POST, with nothing else: no `..`, no further segment, no query. Only the
/// handler's origin (Windows' page spelling never reaches it, see
/// [`handler_origin`]; arriving unreverted it would be a spoof).
pub fn parse_request(method: &str, url: &Url) -> Result<Ask, String> {
    if origin_of(url) != handler_origin() || !url.username().is_empty() || url.password().is_some() {
        return Err(format!("not the scheme's origin: {}", origin_of(url)));
    }
    if url.query().is_some() {
        return Err("a query is never part of a visual's path".into());
    }
    let segments: Vec<&str> = url.path().trim_start_matches('/').split('/').collect();
    let (target, rest) = match segments.as_slice() {
        ["kept", id, rest @ ..] => (VisualRef::Kept { id: (*id).to_string() }, rest),
        [id, rev, rest @ ..] => {
            let digits = rev.strip_prefix('r').ok_or_else(|| format!("not a revision: {rev:?}"))?;
            if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) || digits.starts_with('0') {
                return Err(format!("not a revision: {rev:?}"));
            }
            let rev: u32 = digits.parse().map_err(|_| format!("not a revision: {rev:?}"))?;
            (VisualRef::Revision { id: (*id).to_string(), rev }, rest)
        }
        _ => return Err(format!("not a visual path: {:?}", url.path())),
    };
    let target = target.checked()?;
    match (method, rest) {
        ("GET", []) => Ok(Ask::Page(target)),
        ("POST", ["message"]) => Ok(Ask::Message(target)),
        _ => Err(format!("not a visual request: {method} {:?}", url.path())),
    }
}

/// A URL's scheme and host (and port, if any), spelled as [`origin`] is. The
/// `url` crate calls a custom scheme's origin opaque, so this compares the parts.
pub fn origin_of(url: &Url) -> String {
    let mut out = format!("{}://{}", url.scheme(), url.host_str().unwrap_or(""));
    if let Some(port) = url.port() {
        out.push_str(&format!(":{port}"));
    }
    out
}

/// Whether the webview showing `own` may navigate to `url`: only its blank
/// start and its own page (a fragment of it included). Everything else, a
/// link, a form, a meta refresh, `location`, a redirect, is denied.
pub fn allow_navigation(url: &Url, own: &VisualRef) -> bool {
    if url.as_str() == "about:blank" {
        return true;
    }
    let mut bare = url.clone();
    bare.set_fragment(None);
    bare == own.url()
}

/// The policy every page is served under. Inline script and style run; no
/// request of any kind leaves the page but a POST to its own path (the
/// bridge, which this module answers). `sandbox` gives the page an opaque
/// origin: no storage, and no reading another page under the same origin.
pub fn csp() -> String {
    format!(
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; \
         img-src data: blob:; font-src data:; media-src data: blob:; connect-src {origin}; \
         form-action 'none'; base-uri 'none'; frame-ancestors 'none'; frame-src 'none'; \
         child-src 'none'; worker-src 'none'; object-src 'none'; sandbox allow-scripts",
        origin = page_origin()
    )
}

/// The rules the platform blocker enforces beneath CSP, in WebKit's content
/// blocker format (both WebKits take it; WebView2 gets [`request_allowed`]):
/// block every URL, then allow the scheme's own origin and inline data. One
/// rule per allowed prefix: WebKit's filter regex has no alternation.
pub fn content_rules() -> String {
    let block = r#"{"trigger":{"url-filter":".*"},"action":{"type":"block"}}"#.to_string();
    let allow = |prefix: &str| {
        format!(r#"{{"trigger":{{"url-filter":"^{prefix}"}},"action":{{"type":"ignore-previous-rules"}}}}"#)
    };
    let own = format!("{}/", regex_escape(&page_origin()));
    let rules = [block, allow(&own), allow("data:"), allow("blob:"), allow("about:")];
    format!("[{}]", rules.join(",\n"))
}

fn regex_escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        if "\\.+*?()|[]{}^$".contains(c) {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

/// Whether the blocker lets a request through: the scheme's own origin and
/// inline data only. What [`content_rules`] says, for WebView2's filter.
pub fn request_allowed(url: &str) -> bool {
    let own = format!("{}/", page_origin());
    url.starts_with(&own) || url.starts_with("data:") || url.starts_with("blob:") || url.starts_with("about:")
}

/// The script run in every frame of a visual before its own: no WebRTC, and
/// in the top frame the bridge to the host.
pub fn prelude(own: &VisualRef) -> String {
    let message_url = serde_json::to_string(&format!("{}/message", own.url())).expect("a JSON string");
    format!(
        r#"(() => {{
  "use strict";
  // WebRTC is beyond CSP's reach: gone before any page script runs, in every frame.
  for (const name of ["RTCPeerConnection", "webkitRTCPeerConnection", "mozRTCPeerConnection", "RTCDataChannel",
                      "RTCIceCandidate", "RTCSessionDescription", "RTCIceTransport", "RTCDtlsTransport",
                      "RTCSctpTransport", "RTCRtpSender", "RTCRtpReceiver", "RTCRtpTransceiver", "RTCCertificate"]) {{
    try {{ Object.defineProperty(window, name, {{ value: undefined, writable: false, configurable: false, enumerable: false }}); }} catch (_) {{}}
  }}
  if (window !== window.top) return;
  const MESSAGE = {message_url};
  const FORWARD = new Set(["choice", "brindle.esc", "brindle.snapshot"]);
  const send = (payload) => {{
    try {{ fetch(MESSAGE, {{ method: "POST", mode: "no-cors", keepalive: true, body: JSON.stringify(payload) }}).catch(() => {{}}); }} catch (_) {{}}
  }};
  // A visual answers as an iframe would, parent.postMessage({{type: "choice", option}}, "*"):
  // top-level, its parent is itself, so the message lands here. The host's own
  // deliveries (post) have no source and aren't echoed back.
  window.addEventListener("message", (event) => {{
    if (event.source !== window) return;
    const data = event.data;
    if (!data || typeof data !== "object" || !FORWARD.has(data.type)) return;
    send(data);
  }});
  // Esc in a separate webview never reaches the host: forward it.
  document.addEventListener("keydown", (event) => {{ if (event.key === "Escape") send({{ type: "brindle.esc" }}); }}, true);
  // The host asks for the rendered DOM; the daemon sanitises what comes back.
  window.addEventListener("message", (event) => {{
    if (event.origin !== "brindle://host" || !event.data || event.data.type !== "brindle.snapshot.request") return;
    send({{ type: "brindle.snapshot", html: document.documentElement.outerHTML }});
  }});
}})();"#
    )
}

/// What the daemon (or, in the probe suite, a fixture) answered for a page.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Upstream {
    pub status: u16,
    pub body: Vec<u8>,
}

/// The source of pages: the daemon route for a reference, as the shell asks it.
pub type Fetch<R> = dyn Fn(&AppHandle<R>, &VisualRef) -> Result<Upstream, String> + Send + Sync;

/// What the sandbox tells the app about a visual (the app relays it to the
/// main webview as events; the probe suite reads it directly).
#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "event", rename_all = "lowercase")]
pub enum Delivery {
    /// The page sent one of the forwarded message types.
    Message { label: String, visual: VisualRef, message: Value },
    /// A navigation or window the hooks denied. `target` is the origin only: a
    /// denied URL may carry what the page tried to leak.
    Blocked { label: String, visual: VisualRef, target: String },
    /// The page finished loading `url` (the own page, or `about:blank` first).
    Loaded { label: String, visual: VisualRef, url: String },
    /// The platform's request blocker couldn't be armed: the page stays blank.
    Failed { label: String, visual: VisualRef, why: String },
}

type Sink = Arc<dyn Fn(Delivery) + Send + Sync>;

/// The open visuals: which webview label shows which reference, and the sink
/// their deliveries go to.
pub struct Sandbox {
    bound: Mutex<HashMap<String, Bound>>,
    sink: Sink,
}

struct Bound {
    visual: VisualRef,
    /// A snapshot the host asked for and hasn't received yet. Unasked
    /// snapshots (a page dispatching the request to itself) are dropped.
    snapshot_wanted: bool,
}

impl Sandbox {
    pub fn new(sink: impl Fn(Delivery) + Send + Sync + 'static) -> Self {
        Sandbox { bound: Mutex::new(HashMap::new()), sink: Arc::new(sink) }
    }

    fn bind(&self, label: &str, visual: &VisualRef) -> Result<(), String> {
        let mut bound = self.bound.lock().map_err(|e| e.to_string())?;
        if bound.contains_key(label) {
            return Err(format!("{} is already open", visual.path()));
        }
        bound.insert(label.to_string(), Bound { visual: visual.clone(), snapshot_wanted: false });
        Ok(())
    }

    fn unbind(&self, label: &str) -> Option<VisualRef> {
        self.bound.lock().ok()?.remove(label).map(|b| b.visual)
    }

    /// The reference the webview `label` shows, if it is an open visual.
    pub fn visual_of(&self, label: &str) -> Option<VisualRef> {
        self.bound.lock().ok()?.get(label).map(|b| b.visual.clone())
    }

    fn deliver(&self, delivery: Delivery) {
        (self.sink)(delivery);
    }

    /// Answer a request the webview `label` made on the scheme. A page is
    /// served only to the webview bound to exactly that reference, from
    /// `fetch`; a message is relayed only from the same.
    pub fn handle<R: Runtime>(
        &self,
        app: &AppHandle<R>,
        label: &str,
        request: Request<Vec<u8>>,
        fetch: &Fetch<R>,
    ) -> Response<Vec<u8>> {
        let url = match Url::parse(&request.uri().to_string()) {
            Ok(url) => url,
            Err(e) => return refuse(StatusCode::BAD_REQUEST, &format!("not a URL: {e}")),
        };
        let ask = match parse_request(request.method().as_str(), &url) {
            Ok(ask) => ask,
            Err(why) => return refuse(StatusCode::FORBIDDEN, &why),
        };
        let target = match self.visual_of(label) {
            Some(open) => open,
            None => return refuse(StatusCode::FORBIDDEN, "not an open visual's webview"),
        };
        let asked = match &ask {
            Ask::Page(v) | Ask::Message(v) => v,
        };
        if *asked != target {
            return refuse(StatusCode::FORBIDDEN, "not this visual's page");
        }
        match ask {
            Ask::Page(_) => match fetch(app, &target) {
                Ok(upstream) => page_response(upstream),
                Err(e) => refuse(StatusCode::BAD_GATEWAY, &format!("the brindle daemon: {e}")),
            },
            Ask::Message(_) => {
                let body = request.body();
                if body.len() > MAX_BYTES {
                    return refuse(StatusCode::PAYLOAD_TOO_LARGE, "a message is at most 2 MB");
                }
                let message: Value = match serde_json::from_slice(body) {
                    Ok(Value::Object(map)) => Value::Object(map),
                    _ => return refuse(StatusCode::BAD_REQUEST, "a message is a JSON object"),
                };
                let kind = message.get("type").and_then(Value::as_str).unwrap_or("");
                if !FORWARDED.contains(&kind) {
                    return refuse(StatusCode::BAD_REQUEST, "not a message type the host takes");
                }
                // Only a valid snapshot consumes the host's ask: an answer or an Esc
                // arriving meanwhile (each request has its own thread) leaves it standing.
                if kind == "brindle.snapshot" && !self.take_snapshot_ask(label) {
                    return refuse(StatusCode::FORBIDDEN, "the host asked for no snapshot");
                }
                self.deliver(Delivery::Message { label: label.to_string(), visual: target, message });
                hardened(Response::builder().status(StatusCode::NO_CONTENT)).body(Vec::new()).expect("a response")
            }
        }
    }

    /// Note that the host wants one snapshot from the webview `label`.
    pub fn want_snapshot(&self, label: &str) -> Result<(), String> {
        let mut bound = self.bound.lock().map_err(|e| e.to_string())?;
        let open = bound.get_mut(label).ok_or_else(|| format!("{label} is not an open visual"))?;
        open.snapshot_wanted = true;
        Ok(())
    }

    /// Consume the host's ask for a snapshot, if one stands.
    fn take_snapshot_ask(&self, label: &str) -> bool {
        let Ok(mut bound) = self.bound.lock() else { return false };
        match bound.get_mut(label) {
            Some(open) => std::mem::replace(&mut open.snapshot_wanted, false),
            None => false,
        }
    }
}

/// A page as the webview gets it: the daemon's HTML under the sandbox's
/// headers. A redirect is never followed or passed on (a `Location` could
/// point anywhere); an error status is shown as text, under the same policy.
pub fn page_response(upstream: Upstream) -> Response<Vec<u8>> {
    if (300..400).contains(&upstream.status) {
        return refuse(StatusCode::BAD_GATEWAY, "the daemon redirected a visual; refused");
    }
    if upstream.body.len() > MAX_BYTES {
        return refuse(StatusCode::PAYLOAD_TOO_LARGE, "a visual is at most 2 MB");
    }
    let status = StatusCode::from_u16(upstream.status).unwrap_or(StatusCode::BAD_GATEWAY);
    let content_type = if status.is_success() { "text/html; charset=utf-8" } else { "text/plain; charset=utf-8" };
    hardened(Response::builder().status(status))
        .header(header::CONTENT_TYPE, content_type)
        .body(upstream.body)
        .expect("a response")
}

fn refuse(status: StatusCode, why: &str) -> Response<Vec<u8>> {
    hardened(Response::builder().status(status))
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(why.as_bytes().to_vec())
        .expect("a response")
}

/// Every answer on the scheme carries the policy, whatever it says.
fn hardened(builder: tauri::http::response::Builder) -> tauri::http::response::Builder {
    builder
        .header(header::CONTENT_SECURITY_POLICY, csp())
        .header(header::X_CONTENT_TYPE_OPTIONS, "nosniff")
        .header(header::REFERRER_POLICY, "no-referrer")
        .header(header::CACHE_CONTROL, "no-store")
        // A DNS lookup is outside CSP: no prefetching a hostname that spells a secret.
        .header("X-DNS-Prefetch-Control", "off")
        .header("Cross-Origin-Resource-Policy", "same-origin")
        .header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), display-capture=(), payment=(), usb=(), bluetooth=(), serial=(), hid=()",
        )
        // The bridge POSTs from an opaque origin: the answer is read by no one, but WebKit wants the grant.
        .header(header::ACCESS_CONTROL_ALLOW_ORIGIN, "*")
}

/// Register the scheme on the app and keep `sandbox` as its state. `fetch`
/// gets a page's HTML from the daemon (the app) or a fixture (the probes).
pub fn register<R: Runtime>(
    builder: tauri::Builder<R>,
    sandbox: Sandbox,
    fetch: impl Fn(&AppHandle<R>, &VisualRef) -> Result<Upstream, String> + Send + Sync + 'static,
) -> tauri::Builder<R> {
    let fetch: Arc<Fetch<R>> = Arc::new(fetch);
    builder
        .manage(sandbox)
        .register_asynchronous_uri_scheme_protocol(SCHEME, move |ctx, request, responder| {
            // Called on the webview's thread: the daemon round trip goes on its own.
            let app = ctx.app_handle().clone();
            let label = ctx.webview_label().to_string();
            let fetch = fetch.clone();
            std::thread::spawn(move || {
                responder.respond(app.state::<Sandbox>().handle(&app, &label, request, &*fetch));
            });
        })
}

/// Where a visual sits in the window, in logical pixels.
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq)]
pub struct Bounds {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

impl Bounds {
    fn rect(&self) -> tauri::Rect {
        tauri::Rect {
            position: LogicalPosition::new(self.x, self.y).into(),
            size: LogicalSize::new(self.width.max(1.0), self.height.max(1.0)).into(),
        }
    }
}

/// Open `visual` as a child webview of `window` at `bounds`. The webview
/// starts blank; its page loads once the platform's request blocker is in
/// place (see [`arm`]). Deliveries go to the sandbox's sink.
pub fn open<R: Runtime>(
    window: &Window<R>,
    sandbox: &Sandbox,
    visual: VisualRef,
    bounds: Bounds,
) -> Result<Webview<R>, String> {
    let visual = visual.checked()?;
    let label = visual.label();
    sandbox.bind(&label, &visual)?;
    // Built on the main thread: the platform's webview configuration (on
    // macOS, the visual's own process pool) exists only there, and that is
    // where Tauri adds a child anyway.
    let (tx, rx) = std::sync::mpsc::channel();
    let (on_main, for_main, sink) = (window.clone(), visual.clone(), sandbox.sink.clone());
    let dispatched = window
        .run_on_main_thread(move || {
            let _ = tx.send(build_child(&on_main, &for_main, bounds, sink));
        })
        .map_err(|e| e.to_string());
    let built = dispatched.and_then(|()| rx.recv().map_err(|_| "the main thread never answered".to_string()));
    match built {
        Ok(Ok(webview)) => Ok(webview),
        Ok(Err(e)) | Err(e) => {
            sandbox.unbind(&label);
            Err(format!("couldn't open the visual: {e}"))
        }
    }
}

/// On the main thread: the child webview with its hooks and the platform's
/// configuration, added to the window, then armed.
fn build_child<R: Runtime>(window: &Window<R>, visual: &VisualRef, bounds: Bounds, sink: Sink) -> Result<Webview<R>, String> {
    let label = visual.label();
    let blank = Url::parse("about:blank").expect("about:blank");
    let for_navigation = (visual.clone(), sink.clone());
    let for_window = (visual.clone(), sink.clone());
    let for_loaded = visual.clone();
    let builder = WebviewBuilder::new(&label, WebviewUrl::External(blank))
        .initialization_script_for_all_frames(prelude(&visual))
        // No storage: a non-persistent data store on macOS (set in
        // platform::configure) and an in-memory profile on Windows. Not on
        // WebKitGTK: there wry answers `incognito` with a fresh ephemeral
        // WebContext, and Tauri registers each scheme once per context it
        // made (the app's), so that fresh context serves no `brindle-visual`
        // and the own page fails to load (WebKit's "unsupported URL" error
        // page, at about:blank). The child shares the app's context instead;
        // the page's `sandbox` policy gives it an opaque origin, so it has no
        // storage there either, and WebKitGTK still gives it its own web process.
        .incognito(!cfg!(target_os = "linux"))
        .devtools(false)
        .zoom_hotkeys_enabled(false)
        .disable_drag_drop_handler()
        .on_navigation(move |url| {
            let allowed = allow_navigation(url, &for_navigation.0);
            if !allowed {
                report_blocked(&for_navigation.1, &for_navigation.0, url);
            }
            allowed
        })
        .on_new_window(move |url, _features| {
            report_blocked(&for_window.1, &for_window.0, &url);
            NewWindowResponse::Deny
        })
        .on_download(|_, _| false)
        .on_page_load(move |webview, payload| {
            if payload.event() == PageLoadEvent::Finished {
                let sandbox = webview.state::<Sandbox>();
                sandbox.deliver(Delivery::Loaded {
                    label: webview.label().to_string(),
                    visual: for_loaded.clone(),
                    url: payload.url().to_string(),
                });
            }
        });
    let builder = platform::configure(builder)?;
    let webview = window
        .add_child(builder, LogicalPosition::new(bounds.x, bounds.y), LogicalSize::new(bounds.width, bounds.height))
        .map_err(|e| e.to_string())?;
    platform::watch_load(&webview, visual, sink.clone());
    arm(&webview, sink, visual, bounds);
    Ok(webview)
}

/// A denied navigation or window: told as a delivery, with the target's origin only.
fn report_blocked(sink: &Sink, visual: &VisualRef, url: &Url) {
    sink(Delivery::Blocked { label: visual.label(), visual: visual.clone(), target: origin_of(url) });
}

/// Close the visual shown by `label`.
pub fn close<R: Runtime>(app: &AppHandle<R>, sandbox: &Sandbox, label: &str) -> Result<(), String> {
    let Some(_visual) = sandbox.unbind(label) else {
        return Err(format!("{label} is not an open visual"));
    };
    match app.get_webview(label) {
        Some(webview) => webview.close().map_err(|e| e.to_string()),
        None => Ok(()),
    }
}

/// Move or resize the visual shown by `label`.
pub fn set_bounds<R: Runtime>(app: &AppHandle<R>, sandbox: &Sandbox, label: &str, bounds: Bounds) -> Result<(), String> {
    if sandbox.visual_of(label).is_none() {
        return Err(format!("{label} is not an open visual"));
    }
    let webview = app.get_webview(label).ok_or_else(|| format!("{label} has no webview"))?;
    platform::place(&webview, bounds);
    webview.set_bounds(bounds.rect()).map_err(|e| e.to_string())
}

/// Send the page a message: a `message` event from origin `brindle://host`
/// (`{type: "brindle.theme", vars}` and the like). The prelude never echoes it.
pub fn post<R: Runtime>(app: &AppHandle<R>, sandbox: &Sandbox, label: &str, message: &Value) -> Result<(), String> {
    if sandbox.visual_of(label).is_none() {
        return Err(format!("{label} is not an open visual"));
    }
    let webview = app.get_webview(label).ok_or_else(|| format!("{label} has no webview"))?;
    let data = serde_json::to_string(message).map_err(|e| e.to_string())?;
    webview
        .eval(format!(
            "window.dispatchEvent(new MessageEvent('message', {{ data: {data}, origin: 'brindle://host' }}));"
        ))
        .map_err(|e| e.to_string())
}

/// Ask the page for its rendered DOM: one `brindle.snapshot` message will be
/// relayed; any other is dropped.
pub fn request_snapshot<R: Runtime>(app: &AppHandle<R>, sandbox: &Sandbox, label: &str) -> Result<(), String> {
    sandbox.want_snapshot(label)?;
    post(app, sandbox, label, &serde_json::json!({ "type": "brindle.snapshot.request" }))
}

/// What arming the blocker came to: the page may load, or it must not.
pub type Armed = Box<dyn FnOnce(Result<(), String>) + Send>;

/// WebKit's switch for `<link rel=preconnect>`, behind a seam: the webview's
/// preferences in the app (WKPreferences on macOS, WebKitSettings on Linux),
/// a stand-in in tests. A preconnect opens a
/// connection with no request, so neither CSP nor the rule list sees it;
/// WebKit keeps it behind the `LinkPreconnect` feature flag, on by default.
pub trait PreconnectSwitch {
    /// Whether this WebKit exposes its feature list and the setter for it.
    fn has_feature_list(&self) -> bool;
    /// The keys of the features it lists.
    fn feature_keys(&self) -> Vec<String>;
    /// Switch the feature `key` off for these preferences.
    fn switch_off(&self, key: &str);
}

/// The feature WebKit keeps link preconnect behind.
pub const LINK_PRECONNECT: &str = "LinkPreconnect";

/// Switch link preconnect off through `switch`, or say why it can't be: a
/// visual is never loaded then.
pub fn disable_preconnect(switch: &dyn PreconnectSwitch) -> Result<(), String> {
    if !switch.has_feature_list() {
        return Err("this WebKit has no feature list to switch link preconnect off".into());
    }
    if switch.feature_keys().iter().any(|key| key == LINK_PRECONNECT) {
        switch.switch_off(LINK_PRECONNECT);
        return Ok(());
    }
    Err("this WebKit lists no LinkPreconnect feature to switch off".into())
}

/// Put the platform's request blocker on the webview, then load the page.
/// Nothing but `about:blank` is shown until the blocker is in place; when it
/// can't be (a WebKit without the preconnect switch, a refused rule list),
/// the page is never loaded and the host is told (`Delivery::Failed`).
fn arm<R: Runtime>(webview: &Webview<R>, sink: Sink, visual: &VisualRef, bounds: Bounds) {
    let url = visual.url();
    let target = webview.clone();
    arm_with(
        sink,
        visual,
        move || {
            let _ = target.navigate(url);
        },
        |armed| platform::arm(webview, bounds, armed),
    );
}

/// Arm through `platform`, then `load` the page on success. On any failure,
/// the platform's or arming's own, nothing loads and the host is told once.
fn arm_with(
    sink: Sink,
    visual: &VisualRef,
    load: impl FnOnce() + Send + 'static,
    platform: impl FnOnce(Armed) -> Result<(), String>,
) {
    let visual = visual.clone();
    let armed: Armed = Box::new(move |outcome| match outcome {
        Ok(()) => load(),
        Err(why) => {
            eprintln!("visual {}: no request blocker, the page stays blank: {why}", visual.path());
            sink(Delivery::Failed { label: visual.label(), visual, why });
        }
    });
    // Called once: by the platform, or here when the platform never got to.
    let once = Arc::new(Mutex::new(Some(armed)));
    let inside = once.clone();
    let outcome = platform(Box::new(move |result| {
        if let Some(armed) = inside.lock().ok().and_then(|mut a| a.take()) {
            armed(result);
        }
    }));
    if let Err(e) = outcome {
        if let Some(armed) = once.lock().ok().and_then(|mut a| a.take()) {
            armed(Err(format!("the webview handle couldn't be reached: {e}")));
        }
    }
}

/// The app's side: relay each delivery to the main webview as an event. The
/// sandbox is made before the app, so the handle arrives later (setup fills it).
pub fn deliver_to_main<R: Runtime>(app: Arc<OnceLock<AppHandle<R>>>) -> impl Fn(Delivery) + Send + Sync + 'static {
    move |delivery| {
        let event = match &delivery {
            Delivery::Message { .. } => MESSAGE_EVENT,
            Delivery::Blocked { .. } => BLOCKED_EVENT,
            Delivery::Loaded { .. } => LOADED_EVENT,
            Delivery::Failed { .. } => FAILED_EVENT,
        };
        if let Some(app) = app.get() {
            let _ = app.emit_to(EventTarget::labeled("main"), event, &delivery);
        }
    }
}

// ---- the commands the reader (app/src) calls ----

/// Open a visual (`rev` given) or a kept visual (`rev` absent) in the window
/// that asked, at `bounds`. Answers the child's label. Async: creating a
/// webview from a synchronous command deadlocks on Windows.
#[tauri::command]
pub async fn visual_open<R: Runtime>(
    window: Window<R>,
    sandbox: State<'_, Sandbox>,
    id: String,
    rev: Option<u32>,
    bounds: Bounds,
) -> Result<String, String> {
    let visual = match rev {
        Some(rev) => VisualRef::Revision { id, rev },
        None => VisualRef::Kept { id },
    };
    let webview = open(&window, &sandbox, visual, bounds)?;
    Ok(webview.label().to_string())
}

#[tauri::command]
pub fn visual_close<R: Runtime>(app: AppHandle<R>, sandbox: State<'_, Sandbox>, label: String) -> Result<(), String> {
    close(&app, &sandbox, &label)
}

#[tauri::command]
pub fn visual_set_bounds<R: Runtime>(
    app: AppHandle<R>,
    sandbox: State<'_, Sandbox>,
    label: String,
    bounds: Bounds,
) -> Result<(), String> {
    set_bounds(&app, &sandbox, &label, bounds)
}

#[tauri::command]
pub fn visual_post<R: Runtime>(app: AppHandle<R>, sandbox: State<'_, Sandbox>, label: String, message: Value) -> Result<(), String> {
    post(&app, &sandbox, &label, &message)
}

#[tauri::command]
pub fn visual_snapshot<R: Runtime>(app: AppHandle<R>, sandbox: State<'_, Sandbox>, label: String) -> Result<(), String> {
    request_snapshot(&app, &sandbox, &label)
}

// ---- the platform blockers ----

/// WebKit (macOS): a content rule list on the webview's user content
/// controller, compiled by WebKit's store, then the page loads.
#[cfg(target_os = "macos")]
mod platform {
    use super::{content_rules, Armed, Bounds};
    use block2::RcBlock;
    use objc2::rc::Retained;
    use objc2::runtime::AnyObject;
    use objc2::{msg_send, sel, ClassType, MainThreadMarker};
    use objc2_foundation::{NSArray, NSError, NSString};
    use objc2_web_kit::{
        WKContentRuleList, WKContentRuleListStore, WKPreferences, WKProcessPool, WKUserContentController, WKWebView,
        WKWebViewConfiguration, WKWebsiteDataStore,
    };
    use std::cell::RefCell;
    use std::rc::Rc;
    use tauri::webview::WebviewBuilder;
    use tauri::{Runtime, Webview};

    /// The visual's own WebKit configuration: its own process pool, so its
    /// web content process is nobody else's (a visual that hangs hangs alone,
    /// the app's page and other visuals included), and no storage. wry
    /// leaves a supplied configuration's data store as it is, so the
    /// builder's `incognito` wouldn't reach it: set here.
    pub fn configure<R: Runtime>(builder: WebviewBuilder<R>) -> Result<WebviewBuilder<R>, String> {
        let mtm = MainThreadMarker::new().ok_or("a visual's webview is built on the main thread")?;
        // SAFETY: fresh WebKit objects, made and used on the main thread.
        let configuration = unsafe {
            let configuration = WKWebViewConfiguration::new(mtm);
            configuration.setProcessPool(&WKProcessPool::new(mtm));
            configuration.setWebsiteDataStore(&WKWebsiteDataStore::nonPersistentDataStore(mtm));
            configuration
        };
        Ok(builder.with_webview_configuration(configuration))
    }

    /// WebKit's feature list, through WKPreferences' private API: the class
    /// method `+_features` lists `_WKFeature`s, each with a `key`, and
    /// `-_setEnabled:forFeature:` switches one for these preferences.
    struct WkSwitch {
        preferences: Retained<WKPreferences>,
    }

    impl WkSwitch {
        /// The features, by `key`, paired with the objects the setter wants.
        fn features(&self) -> Vec<(String, Retained<AnyObject>)> {
            // SAFETY: a class method that answers an NSArray of objects answering `key`.
            unsafe {
                let features: Retained<NSArray<AnyObject>> = msg_send![WKPreferences::class(), _features];
                features
                    .iter()
                    .filter_map(|feature| {
                        let key: Option<Retained<NSString>> = msg_send![&*feature, key];
                        key.map(|k| (k.to_string(), feature))
                    })
                    .collect()
            }
        }
    }

    impl super::PreconnectSwitch for WkSwitch {
        fn has_feature_list(&self) -> bool {
            let prefs: &AnyObject = &self.preferences;
            // SAFETY: respondsToSelector: on live objects.
            unsafe {
                let lists: bool = msg_send![WKPreferences::class(), respondsToSelector: sel!(_features)];
                let sets: bool = msg_send![prefs, respondsToSelector: sel!(_setEnabled:forFeature:)];
                lists && sets
            }
        }

        fn feature_keys(&self) -> Vec<String> {
            self.features().into_iter().map(|(key, _)| key).collect()
        }

        fn switch_off(&self, key: &str) {
            let prefs: &AnyObject = &self.preferences;
            for (name, feature) in self.features() {
                if name == key {
                    // SAFETY: the setter the feature list comes with, on these preferences.
                    let _: () = unsafe { msg_send![prefs, _setEnabled: false, forFeature: &*feature] };
                }
            }
        }
    }

    pub fn arm<R: Runtime>(webview: &Webview<R>, _bounds: Bounds, armed: Armed) -> Result<(), String> {
        webview
            .with_webview(move |platform| {
                let Some(mtm) = MainThreadMarker::new() else {
                    return armed(Err("the webview handle came off the main thread".into()));
                };
                // SAFETY: Tauri hands the webview's own WKWebView, alive while the webview is.
                let Some(view) = (unsafe { Retained::retain(platform.inner().cast::<WKWebView>()) }) else {
                    return armed(Err("no WKWebView behind the webview".into()));
                };
                // SAFETY: the configuration and preferences of a live WKWebView.
                let preferences = unsafe { view.configuration().preferences() };
                if let Err(why) = super::disable_preconnect(&WkSwitch { preferences }) {
                    return armed(Err(why));
                }
                // SAFETY: Tauri hands the webview's own WKUserContentController, alive while the webview is.
                let Some(controller) = (unsafe { Retained::retain(platform.controller().cast::<WKUserContentController>()) })
                else {
                    return armed(Err("no user content controller behind the webview".into()));
                };
                // SAFETY: the default store exists on the main thread.
                let Some(store) = (unsafe { WKContentRuleListStore::defaultStore(mtm) }) else {
                    return armed(Err("WebKit has no content rule list store".into()));
                };
                let armed = Rc::new(RefCell::new(Some(armed)));
                let block = RcBlock::new(move |list: *mut WKContentRuleList, error: *mut NSError| {
                    let Some(armed) = armed.borrow_mut().take() else { return };
                    if list.is_null() {
                        // SAFETY: WebKit hands an NSError when it hands no list.
                        let why = unsafe { error.as_ref() }.map(|e| e.localizedDescription().to_string());
                        return armed(Err(format!("WebKit refused the content rules: {why:?}")));
                    }
                    // SAFETY: a non-null list from WebKit, valid for the call.
                    unsafe { controller.addContentRuleList(&*list) };
                    armed(Ok(()));
                });
                // SAFETY: both strings outlive the call; WebKit retains the block.
                unsafe {
                    store.compileContentRuleListForIdentifier_encodedContentRuleList_completionHandler(
                        Some(&NSString::from_str("brindle-visual")),
                        Some(&NSString::from_str(&content_rules())),
                        Some(&block),
                    );
                }
            })
            .map_err(|e| e.to_string())
    }

    pub fn place<R: Runtime>(_webview: &Webview<R>, _bounds: Bounds) {}

    /// WKWebView reports a failed own page through the scheme handler's answer; nothing to watch.
    pub fn watch_load<R: Runtime>(_webview: &Webview<R>, _visual: &super::VisualRef, _sink: super::Sink) {}
}

/// WebView2 (Windows): a resource filter on every request, answering 403 to
/// all but the scheme's own origin and inline data, then the page loads.
#[cfg(target_os = "windows")]
mod platform {
    use super::{request_allowed, Armed, Bounds};
    use tauri::{Runtime, Webview};
    use webview2_com::Microsoft::Web::WebView2::Win32::COREWEBVIEW2_WEB_RESOURCE_CONTEXT_ALL;
    use webview2_com::WebResourceRequestedEventHandler;
    use windows::core::{HSTRING, PWSTR};
    use windows::Win32::System::Com::CoTaskMemFree;

    /// WebView2 gives each webview its own renderer by site; nothing to configure.
    pub fn configure<R: Runtime>(builder: tauri::webview::WebviewBuilder<R>) -> Result<tauri::webview::WebviewBuilder<R>, String> {
        Ok(builder)
    }

    pub fn arm<R: Runtime>(webview: &Webview<R>, _bounds: Bounds, armed: Armed) -> Result<(), String> {
        webview
            .with_webview(move |platform| {
                let environment = platform.environment();
                // SAFETY: COM calls on the controller Tauri hands, on its thread.
                let installed = unsafe {
                    (|| -> windows::core::Result<()> {
                        let core = platform.controller().CoreWebView2()?;
                        core.AddWebResourceRequestedFilter(&HSTRING::from("*"), COREWEBVIEW2_WEB_RESOURCE_CONTEXT_ALL)?;
                        let mut token = 0;
                        core.add_WebResourceRequested(
                            &WebResourceRequestedEventHandler::create(Box::new(move |_, args| {
                                let Some(args) = args else { return Ok(()) };
                                let uri = {
                                    let mut uri = PWSTR::null();
                                    args.Request()?.Uri(&mut uri)?;
                                    let text = uri.to_string().unwrap_or_default();
                                    CoTaskMemFree(Some(uri.0 as *const _));
                                    text
                                };
                                if request_allowed(&uri) {
                                    return Ok(());
                                }
                                let response = environment.CreateWebResourceResponse(
                                    None,
                                    403,
                                    &HSTRING::from("Forbidden"),
                                    &HSTRING::from("Content-Type: text/plain"),
                                )?;
                                args.SetResponse(&response)
                            })),
                            &mut token,
                        )
                    })()
                };
                armed(installed.map_err(|e| format!("WebView2 refused the request filter: {e}")));
            })
            .map_err(|e| e.to_string())
    }

    pub fn place<R: Runtime>(_webview: &Webview<R>, _bounds: Bounds) {}

    /// WebView2 reports a failed own page through the scheme handler's answer; nothing to watch.
    pub fn watch_load<R: Runtime>(_webview: &Webview<R>, _visual: &super::VisualRef, _sink: super::Sink) {}
}

/// WebKitGTK (Linux): the same content rules through WebKit's user content
/// filter store (the Rust binding leaves it out, so the C calls), then the
/// page loads. Tauri packs a child into the window's box under the main
/// webview, so the child is moved onto an overlay at its bounds instead.
#[cfg(target_os = "linux")]
mod platform {
    use super::{content_rules, Armed, Bounds, Delivery, PreconnectSwitch, Sink, VisualRef};
    use glib::translate::ToGlibPtr;
    use gtk::prelude::*;
    use std::ffi::{c_char, c_void, CStr, CString};
    use tauri::{Runtime, Url, Webview};
    use webkit2gtk::WebViewExt;

    /// WebKitGTK runs each web view in its own web process by default, and the
    /// child must share the app's WebContext (the one the scheme is registered
    /// on, see `build_child`); nothing to configure.
    pub fn configure<R: Runtime>(builder: tauri::webview::WebviewBuilder<R>) -> Result<tauri::webview::WebviewBuilder<R>, String> {
        Ok(builder)
    }

    /// A failed load of the own page (a context without the scheme, a
    /// refused response) would otherwise show WebKit's own error page, at
    /// about:blank, and look loaded: tell the host it failed, with WebKit's
    /// reason, and show nothing. A cancelled load (a denied navigation, a
    /// load replaced by another) and any other URL keep WebKit's handling.
    pub fn watch_load<R: Runtime>(webview: &Webview<R>, visual: &VisualRef, sink: Sink) {
        let own = visual.url();
        let visual = visual.clone();
        let _ = webview.with_webview(move |platform| {
            platform.inner().connect_load_failed(move |_, _, failing, error| {
                let cancelled = error.matches(webkit2gtk::NetworkError::Cancelled)
                    || error.matches(webkit2gtk::PolicyError::FrameLoadInterruptedByPolicyChange);
                let own_page = Url::parse(failing)
                    .map(|mut url| {
                        url.set_fragment(None);
                        url == own
                    })
                    .unwrap_or(false);
                if cancelled || !own_page {
                    return false;
                }
                let why = format!("WebKitGTK couldn't load the page: {error}");
                eprintln!("visual {}: {why}", visual.path());
                sink(Delivery::Failed { label: visual.label(), visual: visual.clone(), why });
                true
            });
        });
    }

    pub fn arm<R: Runtime>(webview: &Webview<R>, bounds: Bounds, armed: Armed) -> Result<(), String> {
        webview
            .with_webview(move |platform| {
                let view = platform.inner();
                if let Err(why) = overlay(&view, bounds) {
                    return armed(Err(why));
                }
                let Some(settings) = WebViewExt::settings(&view) else {
                    return armed(Err("the webview has no settings".into()));
                };
                let switch = GtkSwitch { settings, api: FeatureApi::lookup() };
                if let Err(why) = super::disable_preconnect(&switch) {
                    let near: Vec<String> =
                        switch.feature_keys().into_iter().filter(|k| k.to_lowercase().contains("preconnect")).collect();
                    return armed(Err(format!("{why} (features named like it: {near:?})")));
                }
                let Some(manager) = view.user_content_manager() else {
                    return armed(Err("the webview has no user content manager".into()));
                };
                let Some(storage) = dirs_cache() else {
                    return armed(Err("no cache directory for the content filter".into()));
                };
                let (Ok(path), Ok(identifier)) = (CString::new(storage), CString::new("brindle-visual")) else {
                    return armed(Err("the content filter's path isn't a C string".into()));
                };
                let rules = glib::Bytes::from_owned(content_rules().into_bytes());
                let done: Box<Done> = Box::new(Done { manager, armed: Some(armed) });
                // SAFETY: the store, rules and identifier live through the call; WebKit copies
                // what it keeps and calls `saved` once, which frees `done`.
                unsafe {
                    let store = webkit2gtk_sys::webkit_user_content_filter_store_new(path.as_ptr());
                    webkit2gtk_sys::webkit_user_content_filter_store_save(
                        store,
                        identifier.as_ptr(),
                        rules.to_glib_none().0,
                        std::ptr::null_mut(),
                        Some(saved),
                        Box::into_raw(done) as *mut c_void,
                    );
                    glib::gobject_ffi::g_object_unref(store as *mut _);
                }
            })
            .map_err(|e| e.to_string())
    }

    /// WebKit's feature list through WebKitSettings (WebKitGTK 2.42: the
    /// same features WKPreferences lists on macOS, by the same keys). The
    /// Rust binding stops short of it, and linking it would refuse an older
    /// WebKitGTK outright, so the entry points are looked up at run time: an
    /// older WebKitGTK has no feature list, and a visual is never loaded there.
    #[derive(Clone, Copy)]
    struct FeatureApi {
        all_features: unsafe extern "C" fn() -> *mut c_void,
        length: unsafe extern "C" fn(*mut c_void) -> usize,
        get: unsafe extern "C" fn(*mut c_void, usize) -> *mut c_void,
        identifier: unsafe extern "C" fn(*mut c_void) -> *const c_char,
        set_enabled: unsafe extern "C" fn(*mut webkit2gtk_sys::WebKitSettings, *mut c_void, glib::ffi::gboolean),
        list_unref: unsafe extern "C" fn(*mut c_void),
    }

    impl FeatureApi {
        fn lookup() -> Option<Self> {
            // SAFETY: a handle on the program itself, and symbol lookups by
            // NUL-terminated names; each symbol is cast to the signature
            // WebKitGTK's headers declare for it.
            unsafe {
                let program = libc::dlopen(std::ptr::null(), libc::RTLD_NOW);
                if program.is_null() {
                    return None;
                }
                let symbol = |name: &[u8]| {
                    let address = libc::dlsym(program, name.as_ptr() as *const c_char);
                    if address.is_null() {
                        None
                    } else {
                        Some(address)
                    }
                };
                let api = (|| {
                    Some(FeatureApi {
                        all_features: std::mem::transmute(symbol(b"webkit_settings_get_all_features\0")?),
                        length: std::mem::transmute(symbol(b"webkit_feature_list_get_length\0")?),
                        get: std::mem::transmute(symbol(b"webkit_feature_list_get\0")?),
                        identifier: std::mem::transmute(symbol(b"webkit_feature_get_identifier\0")?),
                        set_enabled: std::mem::transmute(symbol(b"webkit_settings_set_feature_enabled\0")?),
                        list_unref: std::mem::transmute(symbol(b"webkit_feature_list_unref\0")?),
                    })
                })();
                libc::dlclose(program);
                api
            }
        }

        /// The features, by identifier, handed to `with` while the list is alive.
        fn with_features<T>(&self, with: impl FnOnce(&[(String, *mut c_void)]) -> T) -> T {
            // SAFETY: the list is ours until unref'd, and its features live as long as it.
            unsafe {
                let list = (self.all_features)();
                let mut features = Vec::new();
                if !list.is_null() {
                    for index in 0..(self.length)(list) {
                        let feature = (self.get)(list, index);
                        let identifier = (self.identifier)(feature);
                        if !feature.is_null() && !identifier.is_null() {
                            features.push((CStr::from_ptr(identifier).to_string_lossy().into_owned(), feature));
                        }
                    }
                }
                let out = with(&features);
                if !list.is_null() {
                    (self.list_unref)(list);
                }
                out
            }
        }
    }

    struct GtkSwitch {
        settings: webkit2gtk::Settings,
        api: Option<FeatureApi>,
    }

    impl super::PreconnectSwitch for GtkSwitch {
        fn has_feature_list(&self) -> bool {
            self.api.is_some()
        }

        fn feature_keys(&self) -> Vec<String> {
            match self.api {
                Some(api) => api.with_features(|features| features.iter().map(|(key, _)| key.clone()).collect()),
                None => Vec::new(),
            }
        }

        fn switch_off(&self, key: &str) {
            let Some(api) = self.api else { return };
            api.with_features(|features| {
                for (name, feature) in features {
                    if name.as_str() == key {
                        // SAFETY: a live feature of the list, on the webview's own settings.
                        unsafe { (api.set_enabled)(self.settings.to_glib_none().0, *feature, glib::ffi::GFALSE) };
                    }
                }
            });
        }
    }

    struct Done {
        manager: webkit2gtk::UserContentManager,
        armed: Option<Armed>,
    }

    unsafe extern "C" fn saved(
        store: *mut glib::gobject_ffi::GObject,
        result: *mut gio::ffi::GAsyncResult,
        data: *mut c_void,
    ) {
        let mut done: Box<Done> = Box::from_raw(data as *mut Done);
        let Some(armed) = done.armed.take() else { return };
        let mut error: *mut glib::ffi::GError = std::ptr::null_mut();
        let filter = webkit2gtk_sys::webkit_user_content_filter_store_save_finish(
            store as *mut webkit2gtk_sys::WebKitUserContentFilterStore,
            result,
            &mut error,
        );
        if filter.is_null() {
            let why: glib::Error = glib::translate::from_glib_full(error);
            return armed(Err(format!("WebKitGTK refused the content rules: {why}")));
        }
        webkit2gtk_sys::webkit_user_content_manager_add_filter(done.manager.to_glib_none().0, filter);
        webkit2gtk_sys::webkit_user_content_filter_unref(filter);
        armed(Ok(()));
    }

    fn dirs_cache() -> Option<String> {
        let home = std::env::var_os("XDG_CACHE_HOME")
            .map(std::path::PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|h| std::path::PathBuf::from(h).join(".cache")))?;
        let dir = home.join("brindle").join("visual-filters");
        std::fs::create_dir_all(&dir).ok()?;
        dir.to_str().map(str::to_string)
    }

    /// Lift the child out of the window's box (where Tauri packs every child,
    /// under the main webview) onto a `gtk::Overlay` over the main webview, at
    /// its bounds. The first visual makes the overlay around the main webview;
    /// every later one (and a reopen) joins the overlay already in the box.
    fn overlay(child: &webkit2gtk::WebView, bounds: Bounds) -> Result<(), String> {
        let Some(parent) = child.parent() else {
            return Err("the visual's widget has no parent".into());
        };
        if !parent.is::<gtk::Overlay>() {
            let vbox = parent
                .downcast::<gtk::Box>()
                .map_err(|w| format!("the visual's widget is in a {}, not the window's box", w.type_().name()))?;
            let own: &gtk::Widget = child.upcast_ref();
            let siblings = vbox.children();
            let overlay = match siblings.iter().find_map(|w| w.clone().downcast::<gtk::Overlay>().ok()) {
                Some(overlay) => overlay,
                None => {
                    let main = siblings
                        .into_iter()
                        .find(|w| w.is::<webkit2gtk::WebView>() && w != own)
                        .ok_or("no main webview beside the visual to lay it over")?;
                    vbox.remove(&main);
                    let overlay = gtk::Overlay::new();
                    overlay.add(&main);
                    vbox.pack_start(&overlay, true, true, 0);
                    overlay
                }
            };
            vbox.remove(child);
            overlay.add_overlay(child);
            overlay.show_all();
        }
        child.set_halign(gtk::Align::Start);
        child.set_valign(gtk::Align::Start);
        place_at(child, bounds);
        child.show();
        Ok(())
    }

    fn place_at(child: &webkit2gtk::WebView, bounds: Bounds) {
        child.set_margin_start(bounds.x.max(0.0) as i32);
        child.set_margin_top(bounds.y.max(0.0) as i32);
        child.set_size_request(bounds.width.max(1.0) as i32, bounds.height.max(1.0) as i32);
    }

    pub fn place<R: Runtime>(webview: &Webview<R>, bounds: Bounds) {
        let _ = webview.with_webview(move |platform| place_at(&platform.inner(), bounds));
    }

    /// Where the child's widget sits: on the overlay, at which margins and size.
    pub fn placement_of(child: &webkit2gtk::WebView) -> super::Placement {
        let (width, height) = child.size_request();
        super::Placement {
            in_overlay: child.parent().is_some_and(|p| p.is::<gtk::Overlay>()),
            x: child.margin_start(),
            y: child.margin_top(),
            width,
            height,
        }
    }
}

/// Where a visual's widget sits in the window on Linux (the probe suite
/// checks it): on the overlay over the main webview, at its bounds.
#[cfg(target_os = "linux")]
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Placement {
    pub in_overlay: bool,
    pub x: i32,
    pub y: i32,
    pub width: i32,
    pub height: i32,
}

/// Ask the webview's widget where it sits (Linux).
#[cfg(target_os = "linux")]
pub fn placement<R: Runtime>(webview: &Webview<R>) -> Result<Placement, String> {
    let (tx, rx) = std::sync::mpsc::channel();
    webview
        .with_webview(move |platform| {
            let _ = tx.send(platform::placement_of(&platform.inner()));
        })
        .map_err(|e| e.to_string())?;
    rx.recv_timeout(std::time::Duration::from_secs(10)).map_err(|_| "the widget never answered".to_string())
}

#[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
mod platform {
    use super::{Armed, Bounds};
    use tauri::{Runtime, Webview};

    pub fn configure<R: Runtime>(builder: tauri::webview::WebviewBuilder<R>) -> Result<tauri::webview::WebviewBuilder<R>, String> {
        Ok(builder)
    }

    /// No blocker known for this platform: the page is never loaded.
    pub fn arm<R: Runtime>(_webview: &Webview<R>, _bounds: Bounds, armed: Armed) -> Result<(), String> {
        armed(Err("no request blocker for this platform".into()));
        Ok(())
    }

    pub fn place<R: Runtime>(_webview: &Webview<R>, _bounds: Bounds) {}
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A URL as the handler receives it (the page spelling, where it differs,
    /// is undone by wry before the handler).
    fn url(path: &str) -> Url {
        Url::parse(&format!("{}{path}", handler_origin())).unwrap()
    }

    /// A URL as the page sees it (what the hooks and the blocker get).
    fn page_url(path: &str) -> Url {
        Url::parse(&format!("{}{path}", page_origin())).unwrap()
    }

    #[test]
    fn a_request_names_one_revision_or_kept_visual_and_nothing_beyond() {
        let v = |id: &str, rev| VisualRef::Revision { id: id.into(), rev };
        let accepted = [
            ("GET", "/v_8f2c/r2", Ask::Page(v("v_8f2c", 2))),
            ("GET", "/kept/v_8f2c", Ask::Page(VisualRef::Kept { id: "v_8f2c".into() })),
            ("POST", "/v_8f2c/r2/message", Ask::Message(v("v_8f2c", 2))),
            ("POST", "/kept/v_8f2c/message", Ask::Message(VisualRef::Kept { id: "v_8f2c".into() })),
        ];
        for (method, path, want) in accepted {
            assert_eq!(parse_request(method, &url(path)), Ok(want), "{method} {path}");
        }
        let refused = [
            ("GET", "/v_8f2c/r2/message"),       // a message is a POST
            ("POST", "/v_8f2c/r2"),              // a page is a GET
            ("GET", "/v_8f2c/r2/../other/r1"),   // no path beyond id and revision (the URL normalises it to /v_8f2c/other/r1)
            ("GET", "/v_8f2c/r2/extra"),
            ("GET", "/v_8f2c/2"),                // revisions are r<n>
            ("GET", "/v_8f2c/r0"),
            ("GET", "/v_8f2c/r01"),
            ("GET", "/v_8f2c/r9999999999"),
            ("GET", "/v_8f2c"),                  // no revision
            ("GET", "/kept"),                    // no id
            ("GET", "/kept/v_8f2c/r1"),
            ("GET", "/v.8f2c/r1"),               // an id is [A-Za-z0-9_-]
            ("GET", "/v_8f2c/r2?d=secret"),      // never a query
            ("GET", "/kept/kept"),
            ("DELETE", "/v_8f2c/r2"),
        ];
        for (method, path) in refused {
            assert!(parse_request(method, &url(path)).is_err(), "{method} {path} was accepted");
        }
        // Another host on the scheme.
        let other = Url::parse(&format!("{}/v_8f2c/r2", handler_origin().replace("localhost", "evil.localhost"))).unwrap();
        assert!(parse_request("GET", &other).is_err());
        // Windows' page spelling never reaches the handler (wry undoes it): arriving as is, it's a spoof.
        assert!(parse_request("GET", &Url::parse("http://brindle-visual.localhost/v_8f2c/r2").unwrap()).is_err());
        // The handler's form is the same on every platform, whatever the page's.
        assert_eq!(
            parse_request("GET", &Url::parse("brindle-visual://localhost/v_8f2c/r2").unwrap()),
            Ok(Ask::Page(v("v_8f2c", 2)))
        );
    }

    /// A WebKit as a test sees it: with or without the feature list, and
    /// listing whatever features it is given.
    struct FakeWebKit {
        feature_list: Option<Vec<&'static str>>,
        switched_off: Mutex<Vec<String>>,
    }

    impl PreconnectSwitch for FakeWebKit {
        fn has_feature_list(&self) -> bool {
            self.feature_list.is_some()
        }
        fn feature_keys(&self) -> Vec<String> {
            self.feature_list.iter().flatten().map(|k| k.to_string()).collect()
        }
        fn switch_off(&self, key: &str) {
            self.switched_off.lock().unwrap().push(key.to_string());
        }
    }

    /// Arm a visual through the real completion path, with a WebKit that
    /// does or doesn't offer the preconnect switch: what loaded, what the
    /// host was told, what was switched off.
    fn armed_with(webkit: &FakeWebKit) -> (bool, Vec<Delivery>, Vec<String>) {
        let delivered: Arc<Mutex<Vec<Delivery>>> = Default::default();
        let log = delivered.clone();
        let sandbox = Sandbox::new(move |d| log.lock().unwrap().push(d));
        let visual = VisualRef::Revision { id: "v_1".into(), rev: 1 };
        let loaded = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let did_load = loaded.clone();
        arm_with(
            sandbox.sink.clone(),
            &visual,
            move || did_load.store(true, std::sync::atomic::Ordering::SeqCst),
            // The macOS platform step, up to the rule list: the preconnect switch decides.
            |armed| {
                armed(disable_preconnect(webkit));
                Ok(())
            },
        );
        let off = webkit.switched_off.lock().unwrap().clone();
        let deliveries = delivered.lock().unwrap().clone();
        (loaded.load(std::sync::atomic::Ordering::SeqCst), deliveries, off)
    }

    /// The one delivery a failed arming makes: the host is told once, about
    /// that visual, and nothing else is said.
    fn told_once_of_failure(deliveries: &[Delivery], expected: &VisualRef) {
        assert_eq!(deliveries.len(), 1, "{deliveries:?}");
        match &deliveries[0] {
            Delivery::Failed { label, visual, why } => {
                assert_eq!(visual, expected);
                assert_eq!(label, &expected.label());
                assert!(!why.is_empty());
            }
            other => panic!("not a failure: {other:?}"),
        }
    }

    #[test]
    fn a_visual_never_loads_when_webkit_cannot_switch_preconnect_off() {
        let visual = VisualRef::Revision { id: "v_1".into(), rev: 1 };
        // No feature list (an older or stripped WebKit): told once, nothing loads.
        let no_list = FakeWebKit { feature_list: None, switched_off: Default::default() };
        let (loaded, deliveries, off) = armed_with(&no_list);
        assert!(!loaded);
        assert!(off.is_empty());
        told_once_of_failure(&deliveries, &visual);
        // A feature list without the switch: the same.
        let no_switch = FakeWebKit { feature_list: Some(vec!["WebGL", "LinkPrefetch"]), switched_off: Default::default() };
        let (loaded, deliveries, off) = armed_with(&no_switch);
        assert!(!loaded);
        assert!(off.is_empty());
        told_once_of_failure(&deliveries, &visual);
        // The switch is there: off, and the page loads with nothing to tell.
        let switchable = FakeWebKit { feature_list: Some(vec!["WebGL", LINK_PRECONNECT]), switched_off: Default::default() };
        let (loaded, deliveries, off) = armed_with(&switchable);
        assert!(loaded);
        assert_eq!(off, [LINK_PRECONNECT]);
        assert!(deliveries.is_empty(), "{deliveries:?}");
        // The platform never got to arm at all (no webview handle): told once, nothing loads.
        let delivered: Arc<Mutex<Vec<Delivery>>> = Default::default();
        let log = delivered.clone();
        let sandbox = Sandbox::new(move |d| log.lock().unwrap().push(d));
        let kept = VisualRef::Kept { id: "k_1".into() };
        arm_with(sandbox.sink.clone(), &kept, || panic!("loaded with no blocker"), |_armed| Err("gone".into()));
        told_once_of_failure(&delivered.lock().unwrap(), &kept);
    }

    /// A scheme request through the real handler, as a webview would send it.
    fn request(method: &str, path: &str, body: &str) -> Request<Vec<u8>> {
        Request::builder()
            .method(method)
            .uri(format!("{}{path}", handler_origin()))
            .body(body.as_bytes().to_vec())
            .unwrap()
    }

    #[test]
    fn a_snapshot_is_relayed_once_asked_and_an_answer_in_between_leaves_the_ask_standing() {
        let app = tauri::test::mock_app();
        let delivered: Arc<Mutex<Vec<Delivery>>> = Default::default();
        let log = delivered.clone();
        let sandbox = Sandbox::new(move |d| log.lock().unwrap().push(d));
        let visual = VisualRef::Revision { id: "v_1".into(), rev: 1 };
        let label = visual.label();
        sandbox.bind(&label, &visual).unwrap();
        let no_pages = |_: &AppHandle<tauri::test::MockRuntime>, _: &VisualRef| Err::<Upstream, String>("no pages here".into());
        let post = |body: &str| sandbox.handle(app.handle(), &label, request("POST", "/v_1/r1/message", body), &no_pages).status();

        // Unasked: dropped, whatever the page dispatched to itself.
        assert_eq!(post(r#"{"type":"brindle.snapshot","html":"<p>unasked</p>"}"#), StatusCode::FORBIDDEN);
        sandbox.want_snapshot(&label).unwrap();
        // An answer, an Esc and a malformed POST arrive first: the ask stands.
        assert_eq!(post(r#"{"type":"choice","option":"B"}"#), StatusCode::NO_CONTENT);
        assert_eq!(post(r#"{"type":"brindle.esc"}"#), StatusCode::NO_CONTENT);
        assert_eq!(post("not json"), StatusCode::BAD_REQUEST);
        assert_eq!(post(r#"{"type":"brindle.exec","cmd":"rm"}"#), StatusCode::BAD_REQUEST);
        assert_eq!(post(r#"{"type":"brindle.snapshot","html":"<p>asked</p>"}"#), StatusCode::NO_CONTENT);
        // Once.
        assert_eq!(post(r#"{"type":"brindle.snapshot","html":"<p>again</p>"}"#), StatusCode::FORBIDDEN);
        // Another webview (the app's own) asking for this visual's page, or this webview for another's: refused before any fetch.
        let page = |from: &str, path: &str| sandbox.handle(app.handle(), from, request("GET", path, ""), &no_pages).status();
        assert_eq!(page("main", "/v_1/r1"), StatusCode::FORBIDDEN);
        assert_eq!(page(&label, "/v_2/r1"), StatusCode::FORBIDDEN);
        assert_eq!(page(&label, "/v_1/r2"), StatusCode::FORBIDDEN);
        assert_eq!(page(&label, "/v_1/r1"), StatusCode::BAD_GATEWAY);

        let kinds: Vec<String> = delivered
            .lock()
            .unwrap()
            .iter()
            .map(|d| match d {
                Delivery::Message { message, .. } => message["type"].as_str().unwrap().to_string(),
                other => format!("{other:?}"),
            })
            .collect();
        assert_eq!(kinds, ["choice", "brindle.esc", "brindle.snapshot"]);
    }

    #[test]
    fn navigation_stays_on_the_own_page_and_answers_stay_under_the_policy() {
        let own = VisualRef::Revision { id: "v_8f2c".into(), rev: 2 };
        assert!(allow_navigation(&Url::parse("about:blank").unwrap(), &own));
        assert!(allow_navigation(&own.url(), &own));
        assert!(allow_navigation(&page_url("/v_8f2c/r2#b"), &own));
        for denied in [
            page_url("/v_8f2c/r3"),
            page_url("/other/r2"),
            page_url("/kept/v_8f2c"),
            page_url("/v_8f2c/r2?x=1"),
            Url::parse("http://127.0.0.1:4242/leak").unwrap(),
            Url::parse("tauri://localhost/index.html").unwrap(),
            Url::parse("javascript:fetch('http://x')").unwrap(),
            Url::parse("about:srcdoc").unwrap(),
        ] {
            assert!(!allow_navigation(&denied, &own), "{denied} was allowed");
        }
        // The platform blocker's view of the same.
        assert!(request_allowed(&format!("{}/v_8f2c/r2/message", page_origin())));
        assert!(request_allowed("data:image/png;base64,AA=="));
        assert!(!request_allowed("http://127.0.0.1:4242/leak"));
        assert!(!request_allowed(&format!("{}.evil.com/v_8f2c/r2", page_origin())));

        // A redirect from the daemon is refused, not followed; an error is text; all carry the policy.
        let redirect = page_response(Upstream { status: 302, body: b"<a href=http://x>go</a>".to_vec() });
        assert_eq!(redirect.status(), StatusCode::BAD_GATEWAY);
        let page = page_response(Upstream { status: 200, body: b"<h1>hi</h1>".to_vec() });
        let stale = page_response(Upstream { status: 409, body: b"stale".to_vec() });
        let too_big = page_response(Upstream { status: 200, body: vec![b' '; MAX_BYTES + 1] });
        assert_eq!(too_big.status(), StatusCode::PAYLOAD_TOO_LARGE);
        for response in [&redirect, &page, &stale, &too_big] {
            let csp = response.headers().get(header::CONTENT_SECURITY_POLICY).unwrap().to_str().unwrap();
            assert!(csp.starts_with("default-src 'none'"), "{csp}");
            assert!(csp.contains("sandbox allow-scripts"));
            assert_eq!(response.headers().get(header::X_CONTENT_TYPE_OPTIONS).unwrap(), "nosniff");
        }
        assert_eq!(page.headers().get(header::CONTENT_TYPE).unwrap(), "text/html; charset=utf-8");
        assert_eq!(stale.headers().get(header::CONTENT_TYPE).unwrap(), "text/plain; charset=utf-8");
        // The content rules allow exactly the origin, escaped for WebKit's regex.
        assert!(content_rules().contains(&format!("^{}/", page_origin().replace('.', "\\."))));
    }
}
