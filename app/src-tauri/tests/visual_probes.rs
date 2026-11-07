//! The visual sandbox's probe suite: the ship gate for in-app visuals.
//!
//! A real app window (the shell's own config), a listener on 127.0.0.1 that
//! stands for anything a visual could leak to (and for the daemon), and one
//! hostile page per probe in tests/visual_probes/pages, each opened as a
//! visual exactly as the app opens one (`visual_scheme::open`). A page tries
//! every way out it knows and then says it's done through the bridge, with
//! the list of attempts that succeeded. The suite passes when the listener saw
//! nothing from any page, no page reports a leak, a redirect from the source
//! is refused, and the app's own webview still answers while a visual spins.
//!
//! Not under libtest: a webview wants the main thread, so this is its own
//! program (`harness = false`), run by `cargo test --test visual_probes`.

use std::io::Read;
use std::net::{TcpListener, TcpStream, UdpSocket};
use std::path::Path;
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use brindle_desktop_lib::visual_scheme::{self, Bounds, Delivery, Sandbox, Upstream, VisualRef};
use tauri::Manager;

/// How long one probe may take to report. A page that never does fails.
const DEADLINE: Duration = Duration::from_secs(20);

/// How long one step of the run (opening a probe, waiting for its page, the
/// exit) may take before the watchdog fails the run: the probe's deadline,
/// the question to a silent page, and room for a slow runner.
const STEP_ALLOWANCE: Duration = Duration::from_secs(60);

/// Where the driver tells the watchdog which step it is on.
static STEPS: Mutex<Option<Sender<String>>> = Mutex::new(None);

/// The driver has moved on to the step `name`; the watchdog's clock restarts.
fn step(name: impl Into<String>) {
    if let Some(steps) = STEPS.lock().unwrap().as_ref() {
        let _ = steps.send(name.into());
    }
}

/// Fail the run, naming the step, when one outlasts its allowance. On its own
/// thread, with its own exit: a webview that never comes up holds the main
/// thread, and Tauri's exit needs the main thread, so neither the deadline in
/// `collect` nor `AppHandle::exit` could end the run from there. A probe that
/// hangs is then a failure naming the probe, not a job that runs to its limit.
fn watchdog() {
    let (tx, rx) = mpsc::channel::<String>();
    *STEPS.lock().unwrap() = Some(tx);
    std::thread::spawn(move || {
        let mut current = "starting".to_string();
        loop {
            match rx.recv_timeout(STEP_ALLOWANCE) {
                Ok(next) => current = next,
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    eprintln!("visual sandbox: {current} did not finish within {}s; the run is stuck there", STEP_ALLOWANCE.as_secs());
                    std::process::exit(1);
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => return,
            }
        }
    });
}

/// Where the visual sits in the window.
const BOUNDS: Bounds = Bounds { x: 24.0, y: 24.0, width: 720.0, height: 480.0 };

/// The pages, in revision order (page i is revision i + 1 of visual `probe`).
const PAGES: &[&str] = &[
    "fetch_xhr_beacon",
    "markup_loads",
    "websocket",
    "webrtc",
    "navigation",
    "meta_refresh",
    "inline_handlers",
    "srcdoc",
    "cross_visual",
    "daemon_api",
    "host_ipc",
];
/// The revision the source answers with a redirect.
const REDIRECT_REV: u32 = PAGES.len() as u32 + 1;
/// The revision that spins forever.
const BUSY_REV: u32 = PAGES.len() as u32 + 2;

/// What arrived at the listener: one line per connection or datagram.
type Hits = Arc<Mutex<Vec<String>>>;

struct Listener {
    port: u16,
    hits: Hits,
}

/// A TCP and a UDP listener on one loopback port (TCP for every request and
/// socket a page could open, UDP for WebRTC's STUN), recording each arrival.
fn listen() -> Listener {
    let hits: Hits = Default::default();
    let (tcp, udp) = (1..=20)
        .find_map(|_| {
            let tcp = TcpListener::bind("127.0.0.1:0").ok()?;
            let port = tcp.local_addr().ok()?.port();
            let udp = UdpSocket::bind(("127.0.0.1", port)).ok()?;
            Some((tcp, udp))
        })
        .expect("a loopback port free for both TCP and UDP");
    let port = tcp.local_addr().unwrap().port();
    let tcp_hits = hits.clone();
    std::thread::spawn(move || {
        for stream in tcp.incoming().flatten() {
            let hits = tcp_hits.clone();
            std::thread::spawn(move || record_tcp(stream, &hits));
        }
    });
    let udp_hits = hits.clone();
    std::thread::spawn(move || {
        let mut buf = [0u8; 2048];
        while let Ok((n, from)) = udp.recv_from(&mut buf) {
            udp_hits.lock().unwrap().push(format!("udp {n} bytes from {from}"));
        }
    });
    Listener { port, hits }
}

/// The connection counts the moment it arrives (a preconnect sends nothing
/// and closes only with its webview); what it then says is added when it comes.
fn record_tcp(mut stream: TcpStream, hits: &Hits) {
    let peer = stream.peer_addr().map(|a| a.to_string()).unwrap_or_default();
    hits.lock().unwrap().push(format!("tcp connection from {peer}"));
    let mut buf = [0u8; 1024];
    let n = stream.read(&mut buf).unwrap_or(0);
    if n > 0 {
        let head = String::from_utf8_lossy(&buf[..n]);
        let line = head.lines().next().unwrap_or("").to_string();
        hits.lock().unwrap().push(format!("tcp {peer} sent {line:?}"));
    }
}

/// A probe page with the listener's port, the scheme's origin and the shared
/// snippet filled in.
fn page(dir: &Path, name: &str, port: u16, probe_js: &str) -> String {
    let html = std::fs::read_to_string(dir.join(format!("{name}.html"))).unwrap_or_else(|e| panic!("{name}.html: {e}"));
    html.replace("{{PROBE}}", probe_js)
        .replace("{{PORT}}", &port.to_string())
        .replace("{{ORIGIN}}", &visual_scheme::page_origin())
}

/// What one probe told the harness.
#[derive(Default)]
struct Report {
    /// The page said it's done, with the attempts that succeeded.
    leaks: Option<Vec<String>>,
    /// The page said it started (the busy loop).
    started: bool,
    /// The own page loaded.
    loaded: bool,
    /// Navigations and windows the hooks denied (expected of several pages).
    denied: usize,
    /// Messages the bridge relayed that no probe sends: a forgery got through.
    unexpected: Vec<String>,
    /// Script errors the page saw (shown; a page's own bug, not a leak).
    errors: Vec<String>,
    /// The request blocker couldn't be armed, or the own page failed to
    /// load: the page is not showing, and this is why.
    failed: Option<String>,
}

#[derive(Clone, Copy, PartialEq)]
enum Until {
    Done,
    Loaded,
    Started,
}

/// What a silent page can still tell: its script errors and attempt count,
/// and the start of its text (a webview's own error page says why it's there).
const DIAGNOSTIC: &str = "JSON.stringify({ ready: document.readyState, href: location.href, \
    errors: typeof errors === 'undefined' ? 'no _probe.js ran' : errors, \
    attempts: typeof attempts === 'undefined' ? -1 : attempts.length, \
    leaks: typeof leaks === 'undefined' ? [] : leaks, \
    text: document.body ? document.body.innerText.slice(0, 200) : null })";

/// Collect the deliveries for `label` until `until` is met or the deadline passes.
fn collect(rx: &Receiver<Delivery>, label: &str, own_url: &str, until: Until) -> Report {
    let mut report = Report::default();
    let start = Instant::now();
    loop {
        let met = match until {
            Until::Done => report.leaks.is_some(),
            Until::Loaded => report.loaded,
            Until::Started => report.started,
        };
        if met || report.failed.is_some() {
            return report;
        }
        let Some(remaining) = DEADLINE.checked_sub(start.elapsed()) else { return report };
        let Ok(delivery) = rx.recv_timeout(remaining) else { return report };
        match delivery {
            Delivery::Message { label: from, message, .. } if from == label => {
                let option = message.get("option").and_then(|o| o.as_str()).unwrap_or("");
                match option {
                    "probe-done" => {
                        let strings = |key: &str| -> Vec<String> {
                            message
                                .get(key)
                                .and_then(|l| l.as_array())
                                .map(|l| l.iter().map(|v| v.as_str().unwrap_or("?").to_string()).collect())
                                .unwrap_or_default()
                        };
                        report.errors = strings("errors");
                        report.leaks = Some(strings("leaks"));
                    }
                    "probe-started" => report.started = true,
                    _ => report.unexpected.push(message.to_string()),
                }
            }
            Delivery::Loaded { label: from, url, .. } if from == label && url == own_url => report.loaded = true,
            Delivery::Loaded { .. } => {}
            Delivery::Blocked { label: from, .. } if from == label => report.denied += 1,
            Delivery::Failed { label: from, why, .. } if from == label => report.failed = Some(why),
            other => report.unexpected.push(format!("{other:?}")),
        }
    }
}

/// Run every probe in order; the failures, as lines.
fn run_probes(app: tauri::AppHandle, rx: Receiver<Delivery>, listener: Listener) -> Vec<String> {
    let mut failures = Vec::new();
    step("blanking the main webview");
    let window = app.get_window("main").expect("the main window");
    let main = app.get_webview("main").expect("the main webview");
    // The app's page is a build stub: a blank page keeps the harness's own traffic out of the count.
    let m = main.clone();
    window
        .run_on_main_thread(move || {
            eprintln!("DIAG main navigate: before");
            let _ = m.navigate("about:blank".parse().unwrap());
            eprintln!("DIAG main navigate: after");
        })
        .unwrap();

    // The listener must be real for "zero hits" to mean anything.
    step("checking the listener");
    TcpStream::connect(("127.0.0.1", listener.port)).expect("connect to the listener");
    let seen = Instant::now();
    while listener.hits.lock().unwrap().is_empty() {
        assert!(seen.elapsed() < DEADLINE, "the listener recorded nothing from a direct connection");
        std::thread::yield_now();
    }
    listener.hits.lock().unwrap().clear();

    // DIAG: where does a child webview hang on Windows?
    for (what, incognito) in [("bare", false), ("incognito", true)] {
        step(format!("diag: {what} child webview"));
        let (tx, built) = mpsc::channel();
        let w = window.clone();
        let label = format!("diag-{what}");
        window
            .run_on_main_thread(move || {
                eprintln!("diag {label}: on the main thread");
                let builder = tauri::webview::WebviewBuilder::new(&label, tauri::WebviewUrl::External("about:blank".parse().unwrap()))
                    .incognito(incognito);
                let r = w.add_child(builder, tauri::LogicalPosition::new(0.0, 0.0), tauri::LogicalSize::new(100.0, 100.0));
                eprintln!("diag {label}: add_child returned {:?}", r.as_ref().map(|_| ()).map_err(|e| e.to_string()));
                let _ = tx.send(r.map(|_| ()).map_err(|e| e.to_string()));
            })
            .unwrap();
        println!("diag {what}: {:?}", built.recv());
    }

    let sandbox = app.state::<Sandbox>();
    let probe = |rev: u32, name: &str, until: Until, failures: &mut Vec<String>| -> Report {
        let visual = VisualRef::Revision { id: "probe".into(), rev };
        let label = visual.label();
        let before = listener.hits.lock().unwrap().len();
        step(format!("{name}: opening the visual"));
        if let Err(e) = visual_scheme::open(&window, &sandbox, visual.clone(), BOUNDS) {
            failures.push(format!("{name}: couldn't open: {e}"));
            return Report::default();
        }
        step(format!("{name}: waiting for the page"));
        let report = collect(&rx, &label, visual.url().as_str(), until);
        if until == Until::Done && report.leaks.is_none() {
            // The page never spoke: ask it what happened before it goes.
            step(format!("{name}: asking the silent page"));
            if let Some(child) = app.get_webview(&label) {
                let (tx, told) = mpsc::channel();
                let _ = child.eval_with_callback(DIAGNOSTIC, move |value| {
                    let _ = tx.send(value);
                });
                match told.recv_timeout(Duration::from_secs(5)) {
                    Ok(value) => println!("      the silent page says: {value}"),
                    Err(_) => println!("      the silent page answers no eval either"),
                }
            }
        }
        let hits: Vec<String> = listener.hits.lock().unwrap()[before..].to_vec();
        let leaks = report.leaks.clone().unwrap_or_default();
        println!(
            "{:<5} {name}: {} hits, {} leaks, {} denied navigations{}",
            if hits.is_empty() && leaks.is_empty() && report.unexpected.is_empty() { "ok" } else { "FAIL" },
            hits.len(),
            leaks.len(),
            report.denied,
            if report.leaks.is_none() && report.loaded { " (page loaded; no done)" } else { "" },
        );
        for hit in &hits {
            println!("      the listener saw {hit}");
            failures.push(format!("{name}: the listener saw {hit}"));
        }
        for leak in &leaks {
            println!("      the page got through: {leak}");
            failures.push(format!("{name}: the page got through: {leak}"));
        }
        for message in &report.unexpected {
            println!("      an unexpected delivery: {message}");
            failures.push(format!("{name}: an unexpected delivery: {message}"));
        }
        for error in &report.errors {
            println!("      the page reported an error: {error}");
        }
        if let Some(why) = &report.failed {
            println!("      the page is not showing: {why}");
            failures.push(format!("{name}: the page is not showing: {why}"));
        }
        report
    };

    for (i, name) in PAGES.iter().enumerate() {
        let rev = i as u32 + 1;
        let report = probe(rev, name, Until::Done, &mut failures);
        if report.leaks.is_none() {
            failures.push(format!("{name}: the page never said it was done"));
        }
        step(format!("{name}: closing the visual"));
        let _ = visual_scheme::close(&app, &sandbox, &VisualRef::Revision { id: "probe".into(), rev }.label());
    }

    // The source answers a redirect: the page is a refusal, loaded at the own URL, and nothing follows it.
    let report = probe(REDIRECT_REV, "redirect", Until::Loaded, &mut failures);
    if !report.loaded {
        failures.push("redirect: the refusal never loaded".into());
    }
    step("redirect: closing the visual");
    let _ = visual_scheme::close(&app, &sandbox, &VisualRef::Revision { id: "probe".into(), rev: REDIRECT_REV }.label());

    // A visual that never yields: first the visual itself must stop answering
    // (the loop has taken hold), then the app's own webview must still answer.
    let busy = VisualRef::Revision { id: "probe".into(), rev: BUSY_REV };
    let report = probe(BUSY_REV, "busy_loop", Until::Started, &mut failures);
    if !report.started {
        failures.push("busy_loop: the page never said it started".into());
    } else if let Some(child) = app.get_webview(&busy.label()) {
        step("busy_loop: waiting for the visual to stop answering");
        let since = Instant::now();
        let mut hung = false;
        while since.elapsed() < DEADLINE {
            let (tx, answered) = mpsc::channel();
            if child.eval_with_callback("1 + 1", move |value| {
                let _ = tx.send(value);
            }).is_err() {
                break;
            }
            if answered.recv_timeout(Duration::from_secs(3)).is_err() {
                hung = true;
                break;
            }
        }
        if !hung {
            failures.push("busy_loop: the visual kept answering; its busy loop never took hold".into());
        } else {
            step("busy_loop: asking the main webview");
            let (tx, answered) = mpsc::channel();
            main.eval_with_callback("1 + 1", move |value| {
                let _ = tx.send(value);
            })
            .expect("eval on the main webview");
            match answered.recv_timeout(DEADLINE) {
                Ok(value) if value.trim() == "2" => println!("ok    busy_loop: the visual hung and the main webview still answered"),
                Ok(value) => failures.push(format!("busy_loop: the main webview answered {value:?}, not 2")),
                Err(_) => failures.push("busy_loop: the main webview did not answer while the visual spun: it froze".into()),
            }
        }
    } else {
        failures.push("busy_loop: the visual's webview is gone".into());
    }
    step("busy_loop: closing the visual");
    let _ = visual_scheme::close(&app, &sandbox, &busy.label());

    step("placement");
    placement_on_linux(&app, &window, &sandbox, &mut failures);
    failures
}

/// Print the outcome; the process's exit code.
fn summarize(failures: &[String]) -> i32 {
    if failures.is_empty() {
        println!("visual sandbox: every probe made zero requests");
        return 0;
    }
    eprintln!("visual sandbox: {} failures", failures.len());
    for failure in failures {
        eprintln!("  {failure}");
    }
    1
}

/// Linux lays children out itself (Tauri packs them under the main webview):
/// two visuals open at once, and one reopened after a close, must each sit
/// on the overlay over the main webview at the bounds asked for.
#[cfg(target_os = "linux")]
fn placement_on_linux(app: &tauri::AppHandle, window: &tauri::Window, sandbox: &Sandbox, failures: &mut Vec<String>) {
    use visual_scheme::Placement;
    let first = VisualRef::Revision { id: "probe".into(), rev: 1 };
    let second = VisualRef::Revision { id: "probe".into(), rev: 2 };
    let a = Bounds { x: 10.0, y: 20.0, width: 300.0, height: 200.0 };
    let b = Bounds { x: 400.0, y: 40.0, width: 320.0, height: 240.0 };
    let c = Bounds { x: 30.0, y: 60.0, width: 200.0, height: 100.0 };
    let want = |bounds: Bounds| Placement {
        in_overlay: true,
        x: bounds.x as i32,
        y: bounds.y as i32,
        width: bounds.width as i32,
        height: bounds.height as i32,
    };
    let check = |step: &str, visual: &VisualRef, bounds: Bounds, failures: &mut Vec<String>| {
        let placed = app
            .get_webview(&visual.label())
            .ok_or_else(|| "no webview".to_string())
            .and_then(|webview| visual_scheme::placement(&webview));
        match placed {
            Ok(placement) if placement == want(bounds) => println!("ok    placement, {step}: {placement:?}"),
            Ok(placement) => {
                println!("FAIL  placement, {step}: {placement:?}, wanted {:?}", want(bounds));
                failures.push(format!("placement, {step}: {placement:?}"));
            }
            Err(e) => failures.push(format!("placement, {step}: {e}")),
        }
    };
    let open = |visual: &VisualRef, bounds: Bounds, failures: &mut Vec<String>| {
        if let Err(e) = visual_scheme::open(window, sandbox, visual.clone(), bounds) {
            failures.push(format!("placement: couldn't open {}: {e}", visual.path()));
        }
    };
    open(&first, a, failures);
    check("the first visual", &first, a, failures);
    open(&second, b, failures);
    check("a second visual beside it", &second, b, failures);
    check("the first, with a second beside it", &first, a, failures);
    let _ = visual_scheme::close(app, sandbox, &first.label());
    open(&first, c, failures);
    check("the first, reopened after a close", &first, c, failures);
    let _ = visual_scheme::close(app, sandbox, &first.label());
    let _ = visual_scheme::close(app, sandbox, &second.label());
}

#[cfg(not(target_os = "linux"))]
fn placement_on_linux(_app: &tauri::AppHandle, _window: &tauri::Window, _sandbox: &Sandbox, _failures: &mut Vec<String>) {
    println!("      placement: the platform lays children out itself; nothing to check here");
}

fn main() {
    let listener = listen();
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/visual_probes/pages");
    let probe_js = std::fs::read_to_string(dir.join("_probe.js")).expect("_probe.js");
    let pages: Arc<Vec<Vec<u8>>> = Arc::new(
        PAGES
            .iter()
            .chain(std::iter::once(&"busy_loop"))
            .map(|name| page(&dir, name, listener.port, &probe_js).into_bytes())
            .collect(),
    );
    let port = listener.port;

    let (tx, rx) = mpsc::channel();
    let sandbox = Sandbox::new(move |delivery| {
        let _ = tx.send(delivery);
    });
    // The app's own commands, under the app's own guard: what a hostile page's
    // invoke runs into (host_ipc.html).
    let builder = tauri::Builder::default().invoke_handler(brindle_desktop_lib::invoke_handler());
    // The source: the probe pages by revision, a redirect for one, and nothing else.
    let builder = visual_scheme::register(builder, sandbox, move |_app, visual| match visual {
        VisualRef::Revision { id, rev } if id == "probe" && *rev == REDIRECT_REV => Ok(Upstream {
            status: 302,
            body: format!("<a href=\"http://127.0.0.1:{port}/redirected\">moved</a>").into_bytes(),
        }),
        VisualRef::Revision { id, rev } if id == "probe" && *rev == BUSY_REV => {
            Ok(Upstream { status: 200, body: pages.last().cloned().unwrap_or_default() })
        }
        VisualRef::Revision { id, rev } if id == "probe" => match pages.get(*rev as usize - 1) {
            Some(html) => Ok(Upstream { status: 200, body: html.clone() }),
            None => Ok(Upstream { status: 404, body: b"no such revision".to_vec() }),
        },
        _ => Ok(Upstream { status: 404, body: b"no such visual".to_vec() }),
    });
    // The exit code, once the probes have run and reported.
    let outcome: Arc<Mutex<Option<i32>>> = Default::default();
    let driver_outcome = outcome.clone();
    let rx = Mutex::new(Some(rx));
    let listener = Mutex::new(Some(listener));
    watchdog();
    step("building the app");
    // The window exists once the app runs; setup is the first hook after it.
    let app = builder
        .setup(move |app| {
            let handle = app.handle().clone();
            let rx = rx.lock().unwrap().take().expect("the deliveries, once");
            let listener = listener.lock().unwrap().take().expect("the listener, once");
            std::thread::spawn(move || {
                let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| run_probes(handle.clone(), rx, listener)));
                let failures = outcome.unwrap_or_else(|_| vec!["the probe driver panicked (see above)".into()]);
                let code = summarize(&failures);
                *driver_outcome.lock().unwrap() = Some(code);
                // Through the event loop: the watchdog ends the run if that never comes back.
                step("exiting");
                handle.exit(code);
            });
            Ok(())
        })
        .build(brindle_desktop_lib::context())
        .expect("the probe app");
    app.run(move |_, event| {
        if let tauri::RunEvent::Exit = event {
            let code = outcome.lock().unwrap().unwrap_or_else(|| {
                eprintln!("visual sandbox: the probes never ran");
                1
            });
            std::process::exit(code);
        }
    });
}
