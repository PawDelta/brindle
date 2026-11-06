//! The pty view on Windows (app/docs/terminal-architecture.md, step 4). The
//! agents' tmux lives inside WSL, where the shell can't open a pty, so the
//! engine's daemon there runs the view's tmux client (`pty_view` in the
//! engine) and the app relays it over the daemon's WebSocket
//! `/v1/pty_view`, on the port and token daemon.json names (`wsl`, `daemon`).
//! The webview sees the same `PtyViewEvent`s as on macOS and Linux, so
//! api/ptyView.ts's `tauriPtyView` is the same on every OS.
//!
//! The socket: the token first (as every daemon stream sends it), then
//! `{"server", "target", "cols", "rows"}`; after that the frames are
//! examples/pty_ws.rs's. Down: binary, the client's bytes; text
//! `{"crop": {cols, left, width}}`, `{"exit": code}` or `{"error": message}`.
//! Up: text `{"type": "input", "data"}`, `{"type": "resize", "cols", "rows"}`
//! or `{"type": "focus"}`.
//!
//! The engine says it serves views with `"pty_view": true` in `/v1/meta`
//! (`status_of`). What builds, parses and relays is compiled everywhere, so
//! its tests run on any OS; finding the daemon to connect to is Windows-only.
#![cfg_attr(not(windows), allow(dead_code))]

use std::io::{self, Read, Write};
use std::net::{Shutdown, TcpStream};
use std::sync::{mpsc, Arc, Mutex};

use serde::{Deserialize, Serialize};
use tungstenite::protocol::Role;
use tungstenite::{Message, WebSocket};

use crate::daemon::{base64, DaemonInfo};
use crate::pty_view::{Crop, PtyStatus, PtyViewEvent};

/// The daemon's route for a view.
pub const ROUTE: &str = "/v1/pty_view";

/// What a Windows build says when the engine in WSL has no `/v1/pty_view`.
pub const ENGINE_TOO_OLD: &str = "Sessions can't be shown in the app with the brindle installed in WSL: \
     upgrade it there (uv tool upgrade brindle, then brindle daemon stop; the app starts the new one) \
     and check again.";

/// Whether the webview can show panes, from the engine's `GET /v1/meta`
/// (status and body, or why it couldn't be asked).
pub fn status_of(meta: Result<(u16, String), String>) -> PtyStatus {
    let body = match meta {
        Ok((200, body)) => body,
        Ok((status, body)) => {
            return PtyStatus::Unusable { message: format!("the brindle daemon answered {status} to /v1/meta: {body}") }
        }
        Err(message) => return PtyStatus::Unusable { message },
    };
    let serves = serde_json::from_str::<serde_json::Value>(&body).is_ok_and(|meta| meta["pty_view"] == true);
    if serves {
        PtyStatus::Ready { tmux: "the engine's, in WSL".into() }
    } else {
        PtyStatus::Unusable { message: ENGINE_TOO_OLD.into() }
    }
}

/// The frame after the token: the pane to attach to, at the view's size.
pub fn open_frame(server: Option<&str>, target: &str, cols: u16, rows: u16) -> String {
    serde_json::json!({ "server": server, "target": target, "cols": cols, "rows": rows }).to_string()
}

/// A frame up, as `pty_view_write`, `pty_view_resize` and `pty_view_focus` take them.
#[derive(Serialize)]
#[serde(tag = "type", rename_all = "lowercase")]
enum Up {
    Input { data: String },
    Resize { cols: u16, rows: u16 },
    Focus,
    /// Not a frame: the reader has ended, and everything accepted before it (`Intake`) has been handled.
    #[serde(skip)]
    ReaderEnded,
}

impl Up {
    fn frame(&self) -> Message {
        Message::text(serde_json::to_string(self).expect("plain JSON"))
    }
}

/// What goes up to the writer. Once the reader has ended nothing more is
/// accepted, and the writer is told after all that was: every input accepted
/// is sent, or told lost, before the writer lets go of who it tells.
struct Intake {
    up: mpsc::Sender<Up>,
    reading: Mutex<bool>,
}

impl Intake {
    fn send(&self, frame: Up) -> bool {
        let reading = self.reading.lock().unwrap_or_else(|e| e.into_inner());
        *reading && self.up.send(frame).is_ok()
    }

    fn reading(&self) -> bool {
        *self.reading.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn reader_ended(&self) {
        let mut reading = self.reading.lock().unwrap_or_else(|e| e.into_inner());
        *reading = false;
        let _ = self.up.send(Up::ReaderEnded);
    }
}

/// Frames up, to `send`, until the reader has ended. A send that fails ends them
/// all (the socket has gone): `on_lost` is told the bytes of each input that didn't go.
fn send_frames(frames: mpsc::Receiver<Up>, mut send: impl FnMut(Message) -> Result<(), String>, on_lost: impl Fn(usize, String)) {
    let mut failed: Option<String> = None;
    for frame in frames {
        if matches!(frame, Up::ReaderEnded) {
            return;
        }
        if failed.is_none() {
            let Err(reason) = send(frame.frame()) else { continue };
            failed = Some(reason);
        }
        if let (Up::Input { data }, Some(reason)) = (&frame, &failed) {
            on_lost(data.len(), reason.clone());
        }
    }
}

/// The reader's side of a view: who it tells, held until it ends.
struct Reader<F> {
    intake: Arc<Intake>,
    on_event: Arc<Mutex<F>>,
}

impl<F: Fn(PtyViewEvent)> Reader<F> {
    fn tell(&self, event: PtyViewEvent) {
        (self.on_event.lock().unwrap_or_else(|e| e.into_inner()))(event);
    }

    /// Nothing more is accepted; the writer goes on holding `on_event` for what was.
    fn ended(self) {
        self.intake.reader_ended();
    }
}

/// The writer's thread, frames up going to `send`, and what the view and its
/// reader hold. The writer holds `on_event` too, until it has handled all that
/// the intake accepted before the reader ended: a frame it fails after that is
/// still told.
fn start_writer<F: Fn(PtyViewEvent) + Send + 'static>(
    on_event: F,
    mut send: impl FnMut(Message) -> Result<(), String> + Send + 'static,
) -> (Arc<Intake>, Reader<F>) {
    let (up, frames) = mpsc::channel::<Up>();
    let intake = Arc::new(Intake { up, reading: Mutex::new(true) });
    let on_event = Arc::new(Mutex::new(on_event));
    let (told, writing) = (on_event.clone(), intake.clone());
    std::thread::spawn(move || {
        send_frames(
            frames,
            |frame| if writing.reading() { send(frame) } else { Err("the socket has gone".to_string()) },
            |bytes, reason| (told.lock().unwrap_or_else(|e| e.into_inner()))(PtyViewEvent::InputLost { bytes, reason }),
        );
    });
    (intake.clone(), Reader { intake, on_event })
}

/// Input the engine's client didn't take: its size and why.
#[derive(Deserialize)]
struct InputLost {
    bytes: usize,
    reason: String,
}

/// A text frame down.
#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum Down {
    Crop(Crop),
    Exit(i32),
    Error(String),
    #[serde(rename = "input_lost")]
    InputLost(InputLost),
}

/// What a frame from the daemon tells the webview, if anything.
pub fn event_of(message: Message) -> Option<PtyViewEvent> {
    match message {
        Message::Binary(bytes) => Some(PtyViewEvent::Data { data: base64(&bytes) }),
        Message::Text(text) => match serde_json::from_str(&text).ok()? {
            Down::Crop(Crop { cols, left, width }) => Some(PtyViewEvent::Crop { cols, left, width }),
            Down::Exit(code) => Some(PtyViewEvent::Exit { code }),
            Down::Error(message) => Some(PtyViewEvent::Error { message }),
            Down::InputLost(InputLost { bytes, reason }) => Some(PtyViewEvent::InputLost { bytes, reason }),
        },
        _ => None,
    }
}

/// The view's socket, as each of its two WebSockets (the reader's, which
/// answers the daemon's pings, and the writer's) has it. A write is the whole
/// of what tungstenite hands it (whole frames), under one lock, so the two
/// never interleave and a write never waits on a read.
struct Half {
    stream: TcpStream,
    writing: Arc<Mutex<()>>,
}

impl Read for Half {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.stream.read(buf)
    }
}

impl Write for Half {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let _held = self.writing.lock().unwrap_or_else(|e| e.into_inner());
        self.stream.write_all(buf)?;
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.stream.flush()
    }
}

/// One open view: a socket to the daemon, which runs its tmux client.
pub struct PtyView {
    stream: TcpStream,
    intake: Arc<Intake>,
}

/// What giving the view the window takes (`PtyView::take_window`): a focus frame up.
pub struct WindowTaker {
    intake: Arc<Intake>,
}

impl PtyView {
    /// Connect to `daemon`'s `/v1/pty_view` and ask for a view of `target` on
    /// tmux server `server`, `cols` x `rows`. What it says goes to `on_event`
    /// (crops, bytes, then the exit or why it couldn't attach); a socket that
    /// ends with neither is the client's exit, -1.
    pub fn open(
        daemon: &DaemonInfo,
        server: Option<&str>,
        target: &str,
        cols: u16,
        rows: u16,
        on_event: impl Fn(PtyViewEvent) + Send + 'static,
    ) -> Result<PtyView, String> {
        let port = daemon.port.ok_or("the brindle daemon listens on no TCP port")?;
        let failed = |e: &dyn std::fmt::Display| format!("couldn't reach the brindle daemon for the view: {e}");
        let stream = TcpStream::connect(("127.0.0.1", port)).map_err(|e| failed(&e))?;
        let _ = stream.set_nodelay(true);
        let writing = Arc::new(Mutex::new(()));
        let half = || -> Result<Half, String> {
            Ok(Half { stream: stream.try_clone().map_err(|e| failed(&e))?, writing: writing.clone() })
        };
        let (mut down, _) =
            tungstenite::client::client(format!("ws://localhost{ROUTE}"), half()?).map_err(|e| failed(&e))?;
        // The token goes as the first frame, never in the URL (brindle/daemon/auth.py).
        down.send(Message::text(daemon.token.clone())).map_err(|e| failed(&e))?;
        down.send(Message::text(open_frame(server, target, cols, rows))).map_err(|e| failed(&e))?;
        // Frames up on a thread of their own: a write to a daemon that isn't reading blocks.
        let mut writer = WebSocket::from_raw_socket(half()?, Role::Client, None);
        let (intake, reader) = start_writer(on_event, move |frame| writer.send(frame).map_err(|e| e.to_string()));
        std::thread::spawn(move || {
            let mut ended = false;
            loop {
                match down.read() {
                    Ok(Message::Close(_)) | Err(_) => break,
                    Ok(message) => {
                        if let Some(event) = event_of(message) {
                            ended |= matches!(event, PtyViewEvent::Exit { .. } | PtyViewEvent::Error { .. });
                            reader.tell(event);
                        }
                    }
                }
            }
            if !ended {
                reader.tell(PtyViewEvent::Exit { code: -1 });
            }
            reader.ended();
        });
        Ok(PtyView { stream, intake })
    }

    /// What the person typed (keys, a paste, mouse reports), as xterm.js says it.
    /// False: it can't be sent, the socket having gone.
    pub fn write(&self, data: &[u8]) -> bool {
        self.intake.send(Up::Input { data: String::from_utf8_lossy(data).into_owned() })
    }

    /// The view is `cols` x `rows` cells; the daemon adds the crop's columns.
    pub fn resize(&self, cols: u16, rows: u16) -> Result<(), String> {
        self.intake.send(Up::Resize { cols, rows });
        Ok(())
    }

    /// The person is at the view: the daemon gives it the window
    /// (`pty_view::PtyView::take_window` there).
    pub fn take_window(&self) -> Result<(), String> {
        self.window_taker().take()
    }

    pub fn window_taker(&self) -> WindowTaker {
        WindowTaker { intake: self.intake.clone() }
    }
}

impl WindowTaker {
    /// `PtyView::take_window`.
    pub fn take(&self) -> Result<(), String> {
        self.intake.send(Up::Focus);
        Ok(())
    }
}

impl Drop for PtyView {
    /// The socket goes, and with it the daemon's view (its session and client).
    fn drop(&mut self) {
        let _ = self.stream.shutdown(Shutdown::Both);
    }
}

#[cfg(windows)]
mod run {
    use super::*;
    use crate::daemon::Engine;

    /// Whether the engine in WSL serves views (found, or started, first).
    pub fn status(engine: &Engine) -> PtyStatus {
        status_of(engine.get("/v1/meta"))
    }

    /// A view through the engine's daemon in WSL.
    pub fn open(
        engine: &Engine,
        server: Option<&str>,
        target: &str,
        cols: u16,
        rows: u16,
        on_event: impl Fn(PtyViewEvent) + Send + 'static,
    ) -> Result<PtyView, String> {
        PtyView::open(&engine.current()?, server, target, cols, rows, on_event)
    }
}

#[cfg(windows)]
pub use run::{open, status};

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use tungstenite::protocol::frame::coding::CloseCode;
    use tungstenite::protocol::CloseFrame;

    #[test]
    fn the_engine_serves_views_when_its_meta_says_so() {
        let ready = status_of(Ok((200, r#"{"engine_version": "0.6.0", "pty_view": true}"#.into())));
        assert!(matches!(ready, PtyStatus::Ready { .. }), "{ready:?}");
        assert_eq!(
            status_of(Ok((200, r#"{"engine_version": "0.5.0"}"#.into()))),
            PtyStatus::Unusable { message: ENGINE_TOO_OLD.into() }
        );
        let refused = status_of(Ok((500, "{}".into())));
        assert!(matches!(&refused, PtyStatus::Unusable { message } if message.contains("500")), "{refused:?}");
        assert_eq!(
            status_of(Err("the brindle daemon is unreachable".into())),
            PtyStatus::Unusable { message: "the brindle daemon is unreachable".into() }
        );
    }

    /// A fake daemon's `/v1/pty_view`: each connection checks the token and
    /// the open frame, reads the frames up `ups` asks for, then sends `downs`
    /// and closes. Answers the frames it read up.
    fn fake_daemon(scripts: Vec<(usize, Vec<Message>)>) -> (u16, mpsc::Receiver<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let (read, got) = mpsc::channel();
        std::thread::spawn(move || {
            for ((ups, downs), stream) in scripts.into_iter().zip(listener.incoming()) {
                let mut ws = tungstenite::accept(stream.unwrap()).unwrap();
                let mut text = || ws.read().unwrap().into_text().unwrap().to_string();
                assert_eq!(text(), "tok");
                let open: serde_json::Value = serde_json::from_str(&text()).unwrap();
                assert_eq!(open, serde_json::json!({ "server": "agents", "target": "s:@1", "cols": 80, "rows": 24 }));
                let frames = (0..ups).map(|_| text()).collect();
                for down in downs {
                    ws.send(down).unwrap();
                }
                let _ = ws.close(Some(CloseFrame { code: CloseCode::Normal, reason: "".into() }));
                let _ = ws.flush();
                read.send(frames).unwrap();
            }
        });
        (port, got)
    }

    #[test]
    fn a_view_relays_the_daemons_frames_both_ways() {
        let (port, got) = fake_daemon(vec![
            (
                3,
                vec![
                    Message::text(r#"{"crop": {"cols": 31, "left": 0, "width": null}}"#),
                    Message::binary(b"hi".to_vec()),
                    Message::text(r#"{"exit": 0}"#),
                ],
            ),
            (0, vec![Message::text(r#"{"error": "can't find window: @1"}"#)]),
            // The daemon went with no word: the client's gone too.
            (0, vec![]),
        ]);
        let daemon = DaemonInfo { socket: None, port: Some(port), token: "tok".into(), version: String::new(), pid: 1 };
        let open = || {
            let (event, events) = mpsc::channel();
            let view = PtyView::open(&daemon, Some("agents"), "s:@1", 80, 24, move |e| event.send(e).unwrap()).unwrap();
            (view, events)
        };

        let (view, events) = open();
        view.write("é\r".as_bytes());
        view.resize(100, 30).unwrap();
        view.window_taker().take().unwrap();
        let ups: Vec<serde_json::Value> = got.recv().unwrap().iter().map(|f| serde_json::from_str(f).unwrap()).collect();
        assert_eq!(
            ups,
            [
                serde_json::json!({ "type": "input", "data": "é\r" }),
                serde_json::json!({ "type": "resize", "cols": 100, "rows": 30 }),
                serde_json::json!({ "type": "focus" }),
            ]
        );
        // Every event until the socket ends, and nothing after the exit.
        assert_eq!(
            events.iter().collect::<Vec<_>>(),
            [
                PtyViewEvent::Crop { cols: 31, left: 0, width: None },
                PtyViewEvent::Data { data: base64(b"hi") },
                PtyViewEvent::Exit { code: 0 },
            ]
        );
        drop(view);

        let (_failed, events) = open();
        assert_eq!(events.iter().collect::<Vec<_>>(), [PtyViewEvent::Error { message: "can't find window: @1".into() }]);
        let (dropped, events) = open();
        assert_eq!(events.iter().collect::<Vec<_>>(), [PtyViewEvent::Exit { code: -1 }]);
        // The socket gone: a write says it can't be sent, for the webview to log.
        assert!(!dropped.write(b"pasted"));
    }

    /// Input that can't be sent up is told by size, and input the engine's
    /// client didn't take comes down the same way.
    #[test]
    fn input_lost_on_the_way_to_the_engine_is_told_by_size() {
        let (told, events) = mpsc::channel();
        let (sending, in_send) = mpsc::channel();
        let (fail, failing) = mpsc::channel::<()>();
        let mut sent = 0;
        let (intake, reader) = start_writer(
            move |event| told.send(event).unwrap(),
            move |_| {
                sent += 1;
                if sent == 1 {
                    return Ok(());
                }
                sending.send(()).unwrap();
                let _ = failing.recv();
                Err("the socket has gone".to_string())
            },
        );
        assert!(intake.send(Up::Input { data: "sent".into() }));
        assert!(intake.send(Up::Input { data: "pasted".into() }));
        in_send.recv().unwrap();
        for frame in [Up::Focus, Up::Input { data: "typed".into() }] {
            assert!(intake.send(frame));
        }
        // The reader ends and lets go of who it tells, with the writer yet to fail what's queued: nothing
        // more is taken, what was is still told, and only then does the writer let go too.
        reader.ended();
        assert!(!intake.send(Up::Input { data: "too late".into() }));
        fail.send(()).unwrap();
        let gone = || "the socket has gone".to_string();
        assert_eq!(
            events.iter().collect::<Vec<_>>(),
            [PtyViewEvent::InputLost { bytes: 6, reason: gone() }, PtyViewEvent::InputLost { bytes: 5, reason: gone() }]
        );
        assert_eq!(
            event_of(Message::text(r#"{"input_lost": {"bytes": 8, "reason": "the view has closed"}}"#)),
            Some(PtyViewEvent::InputLost { bytes: 8, reason: "the view has closed".into() })
        );
    }
}
