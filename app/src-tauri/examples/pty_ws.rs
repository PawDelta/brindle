//! The pty view (src/pty_view.rs) served over a local WebSocket, so a browser
//! Playwright drives (its WebKit, not the Tauri app) runs the same Rust
//! between the pty and the page: the e2e tests' bridge (e2e/pty/rig.ts) and
//! the measuring rig's (docs/terminal-architecture.md):
//!
//!     cargo run --release --example pty_ws -- <page origin> <the rig's tmux -L name>
//!
//! It listens on a free localhost port and prints
//! `pty_ws listening on 127.0.0.1:<port> token <hex>`. Only a page from
//! `<page origin>` (the rig's Vite, e.g. `http://127.0.0.1:4182`) may
//! connect, its first text frame must carry the token, and it may only name
//! the rig's own tmux server: no other page in a browser can type into a
//! pane through it, and no page into anyone else's. That frame says what to
//! attach to:
//! `{"token": "<hex>", "server": "<-L name or socket path>", "target": "<window>", "cols": c, "rows": r}`.
//! Then the client's bytes come down as binary frames, a redraw each
//! (`pty_view::forward`, as `pty_view_open`'s channel sends them), the window's crop as
//! `{"crop": {"cols": c, "left": l, "width": w}}` (before the first bytes, and on each
//! change), and `{"exit": code}` last;
//! text frames up are `{"type": "input", "data": "..."}` or
//! `{"type": "resize", "cols": c, "rows": r}` or `{"type": "focus"}` (as
//! `pty_view_write`, `pty_view_resize` and `pty_view_focus` take them).

use std::io::Read;
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};

use brindle_desktop_lib::pty_view::{tmux_binary, PtyView};
use serde::Deserialize;
use tungstenite::handshake::server::{ErrorResponse, Request, Response};
use tungstenite::http::StatusCode;
use tungstenite::protocol::Role;
use tungstenite::{Message, WebSocket};

#[derive(Deserialize)]
struct Open {
    token: String,
    server: Option<String>,
    target: String,
    cols: u16,
    rows: u16,
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "lowercase")]
enum Up {
    Input { data: String },
    Resize { cols: u16, rows: u16 },
    Focus,
}

/// Who may use the bridge: the rig's page (its origin), holding this run's
/// token, on the rig's own tmux server and nothing else.
struct Guard {
    origin: String,
    token: String,
    server: String,
}

impl Guard {
    fn origin_ok(&self, origin: Option<&str>) -> bool {
        origin == Some(self.origin.as_str())
    }

    fn admit(&self, open: &Open) -> Result<(), String> {
        if open.token != self.token {
            return Err("wrong token".into());
        }
        if open.server.as_deref() != Some(self.server.as_str()) {
            return Err(format!("only the rig's tmux server ({}) is served", self.server));
        }
        Ok(())
    }
}

fn serve(stream: TcpStream, guard: &Guard) -> Result<(), String> {
    stream.set_nodelay(true).map_err(|e| e.to_string())?;
    let writer = stream.try_clone().map_err(|e| e.to_string())?;
    // A page from anywhere else is refused before the handshake completes.
    let same_origin = |req: &Request, res: Response| -> Result<Response, ErrorResponse> {
        if guard.origin_ok(req.headers().get("origin").and_then(|o| o.to_str().ok())) {
            Ok(res)
        } else {
            let mut refused = ErrorResponse::new(Some("origin refused".into()));
            *refused.status_mut() = StatusCode::FORBIDDEN;
            Err(refused)
        }
    };
    let mut ws = tungstenite::accept_hdr(stream, same_origin).map_err(|e| e.to_string())?;
    let first = ws.read().map_err(|e| e.to_string())?;
    let open: Open = serde_json::from_str(first.to_text().map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
    if let Err(e) = guard.admit(&open) {
        let _ = ws.close(None);
        return Err(e);
    }
    // Down on a WebSocket of its own over the same socket: the reader below
    // only reads (the page never pings).
    let down = Arc::new(Mutex::new(WebSocket::from_raw_socket(writer, Role::Server, None)));
    let (data, cropped, exit) = (down.clone(), down.clone(), down.clone());
    let view = PtyView::open(
        &tmux_binary(),
        open.server.as_deref(),
        &open.target,
        open.cols,
        open.rows,
        move |bytes| {
            let _ = data.lock().unwrap().send(Message::binary(bytes.to_vec()));
        },
        move |crop| {
            let _ = cropped.lock().unwrap().send(Message::text(serde_json::json!({ "crop": crop }).to_string()));
        },
        move |code| {
            let _ = exit.lock().unwrap().send(Message::text(format!("{{\"exit\":{code}}}")));
        },
    );
    let view = match view {
        Ok(view) => view,
        Err(e) => {
            let _ = down.lock().unwrap().send(Message::text(serde_json::json!({ "error": e }).to_string()));
            return Err(e);
        }
    };
    eprintln!("pty_ws: {} on {}", view.name(), open.target);
    loop {
        match ws.read() {
            Ok(Message::Text(text)) => match serde_json::from_str::<Up>(&text) {
                Ok(Up::Input { data }) => view.write(data.as_bytes()),
                Ok(Up::Resize { cols, rows }) => {
                    let _ = view.resize(cols, rows);
                }
                Ok(Up::Focus) => {
                    if let Err(e) = view.take_window() {
                        eprintln!("pty_ws: {e}");
                    }
                }
                Err(e) => eprintln!("pty_ws: {e}"),
            },
            Ok(Message::Close(_)) | Err(_) => break,
            Ok(_) => {}
        }
    }
    Ok(())
}

/// 16 random bytes, as hex.
fn random_token() -> String {
    let mut bytes = [0u8; 16];
    std::fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut bytes)).expect("/dev/urandom");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn main() {
    let (Some(origin), Some(server)) = (std::env::args().nth(1), std::env::args().nth(2)) else {
        eprintln!("usage: pty_ws <page origin, e.g. http://127.0.0.1:4182> <the rig's tmux -L name>");
        std::process::exit(2);
    };
    let guard: &'static Guard = Box::leak(Box::new(Guard { origin, token: random_token(), server }));
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind");
    let port = listener.local_addr().expect("port").port();
    println!("pty_ws listening on 127.0.0.1:{port} token {}", guard.token);
    for stream in listener.incoming().flatten() {
        std::thread::spawn(move || {
            if let Err(e) = serve(stream, guard) {
                eprintln!("pty_ws: {e}");
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn guard() -> Guard {
        Guard { origin: "http://127.0.0.1:4182".into(), token: random_token(), server: "ptyspike-1".into() }
    }

    fn open(token: &str, server: Option<&str>) -> Open {
        Open { token: token.into(), server: server.map(String::from), target: "s:@1".into(), cols: 80, rows: 24 }
    }

    #[test]
    fn only_the_rigs_page_may_connect() {
        let g = guard();
        assert!(g.origin_ok(Some("http://127.0.0.1:4182")));
        assert!(!g.origin_ok(Some("https://evil.example")));
        assert!(!g.origin_ok(Some("http://127.0.0.1:4183")));
        assert!(!g.origin_ok(None));
    }

    #[test]
    fn only_this_runs_token_and_the_rigs_server_are_served() {
        let g = guard();
        assert!(g.admit(&open(&g.token, Some("ptyspike-1"))).is_ok());
        assert!(g.admit(&open("", Some("ptyspike-1"))).is_err());
        assert!(g.admit(&open(&"0".repeat(32), Some("ptyspike-1"))).is_err());
        assert!(g.admit(&open(&g.token, Some("brindle"))).is_err());
        assert!(g.admit(&open(&g.token, Some("default"))).is_err());
        assert!(g.admit(&open(&g.token, None)).is_err());
        assert_ne!(random_token(), random_token());
        assert_eq!(g.token.len(), 32);
    }
}
