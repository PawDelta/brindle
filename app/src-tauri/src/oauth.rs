//! The loopback half of an in-app sign-in (Docs export to Google Docs or
//! SharePoint, app/src/screens/Knowledge/ExportSession.ts): the page opens the
//! system browser on the provider's sign-in, and the provider redirects it to
//! `http://127.0.0.1:<port>/callback`, where this listens for that one request.
//! It only hands the query string back; the code is exchanged, and the tokens
//! kept (in memory), by the page. Nothing is written to disk.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Mutex;

static LISTENERS: Mutex<Option<HashMap<u16, TcpListener>>> = Mutex::new(None);

const DONE_PAGE: &str = "<!doctype html><meta charset=utf-8><title>brindle</title>\
<body style=\"font-family:system-ui;margin:3rem\"><p>You're signed in. You can close this tab and go back to brindle.</p>";

/// Bind a free loopback port and remember the listener; the port is what the
/// redirect URI names.
#[tauri::command]
pub fn oauth_listen() -> Result<u16, String> {
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    LISTENERS
        .lock()
        .map_err(|e| e.to_string())?
        .get_or_insert_with(HashMap::new)
        .insert(port, listener);
    Ok(port)
}

/// The query string of the first `GET /callback` on `port` (without the `?`).
/// Other requests (a favicon, a probe) are answered 404 and ignored.
#[tauri::command]
pub async fn oauth_wait(port: u16) -> Result<String, String> {
    let listener = LISTENERS
        .lock()
        .map_err(|e| e.to_string())?
        .as_mut()
        .and_then(|m| m.remove(&port))
        .ok_or_else(|| "no sign-in is waiting on that port".to_string())?;
    tauri::async_runtime::spawn_blocking(move || loop {
        let (mut stream, _) = listener.accept().map_err(|e| e.to_string())?;
        let mut buf = [0u8; 8192];
        let n = stream.read(&mut buf).unwrap_or(0);
        let head = String::from_utf8_lossy(&buf[..n]);
        match callback_query(head.lines().next().unwrap_or("")) {
            Some(Some(query)) => {
                respond(&mut stream, "200 OK", DONE_PAGE);
                return Ok(query);
            }
            Some(None) => {
                // Our own cancel (below) or an empty callback: stop waiting.
                return Err("the sign-in was cancelled".to_string());
            }
            None => respond(&mut stream, "404 Not Found", ""),
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Stop a wait that is still blocked in `accept`, by knocking on its door.
#[tauri::command]
pub fn oauth_cancel(port: u16) {
    let _ = TcpStream::connect(("127.0.0.1", port));
}

/// For a request line: `None` when it isn't the callback, `Some(None)` when it is
/// but carries no query, else the query.
fn callback_query(line: &str) -> Option<Option<String>> {
    let target = line.strip_prefix("GET ")?.split(' ').next()?;
    let rest = target.strip_prefix("/callback")?;
    match rest.strip_prefix('?') {
        Some(q) if !q.is_empty() => Some(Some(q.to_string())),
        None if rest.is_empty() => Some(None),
        _ => None,
    }
}

fn respond(stream: &mut TcpStream, status: &str, body: &str) {
    let _ = write!(
        stream,
        "HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
}

#[cfg(test)]
mod tests {
    use super::callback_query;

    #[test]
    fn only_the_callback_with_a_query_is_the_answer() {
        assert_eq!(callback_query("GET /callback?code=a&state=b HTTP/1.1"), Some(Some("code=a&state=b".into())));
        assert_eq!(callback_query("GET /callback HTTP/1.1"), Some(None));
        assert_eq!(callback_query("GET /favicon.ico HTTP/1.1"), None);
        assert_eq!(callback_query("GET /callbackx?code=a HTTP/1.1"), None);
    }
}
