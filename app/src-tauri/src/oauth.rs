//! The native half of an in-app sign-in (Docs export to Google Docs or
//! SharePoint, app/src/screens/Knowledge/ExportSession.ts).
//!
//! The page opens the system browser on the provider's sign-in, and the
//! provider redirects it to `http://127.0.0.1:<port>/callback`, where
//! `oauth_listen` / `oauth_wait` catch that one request and hand the query
//! string back. `provider_http` is the only way the page talks to the
//! providers (the webview's CSP allows no outside host, and a native client's
//! token exchange isn't a cross-origin browser call): it sends one request to
//! a fixed list of Google and Microsoft hosts and answers the status and text.
//! The page keeps the tokens, in memory; nothing here writes to disk.

use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Mutex;
use std::time::Duration;

#[derive(Default)]
struct Waits {
    /// Listening, nobody waiting yet.
    pending: HashMap<u16, TcpListener>,
    /// A wait is running on the port.
    active: HashSet<u16>,
    /// A running wait that was told to stop.
    cancelled: HashSet<u16>,
}

static WAITS: Mutex<Option<Waits>> = Mutex::new(None);

fn waits<T>(f: impl FnOnce(&mut Waits) -> T) -> T {
    let mut guard = WAITS.lock().unwrap_or_else(|e| e.into_inner());
    f(guard.get_or_insert_with(Waits::default))
}

const DONE_PAGE: &str = "<!doctype html><meta charset=utf-8><title>brindle</title>\
<body style=\"font-family:system-ui;margin:3rem\"><p>You're signed in. You can close this tab and go back to brindle.</p>";

/// Bind a free loopback port and remember the listener; the port is what the
/// redirect URI names.
#[tauri::command]
pub fn oauth_listen() -> Result<u16, String> {
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    waits(|w| w.pending.insert(port, listener));
    Ok(port)
}

/// The query string of the first `GET /callback` on `port` (without the `?`).
/// Other requests (a favicon, a probe) are answered 404 and ignored.
#[tauri::command]
pub async fn oauth_wait(port: u16) -> Result<String, String> {
    let listener = waits(|w| {
        let l = w.pending.remove(&port);
        if l.is_some() {
            w.active.insert(port);
        }
        l
    })
    .ok_or_else(|| "no sign-in is waiting on that port".to_string())?;
    tauri::async_runtime::spawn_blocking(move || wait_for_callback(port, listener))
        .await
        .map_err(|e| e.to_string())?
}

/// Stop the sign-in on `port`: close it if nobody is waiting yet, else wake
/// the waiter so it ends. Does nothing for a port that is already done.
#[tauri::command]
pub fn oauth_cancel(port: u16) {
    let wake = waits(|w| {
        if w.pending.remove(&port).is_some() {
            return false;
        }
        if w.active.contains(&port) {
            w.cancelled.insert(port);
            return true;
        }
        false
    });
    if wake {
        let _ = TcpStream::connect(("127.0.0.1", port));
    }
}

fn is_cancelled(port: u16) -> bool {
    waits(|w| w.cancelled.contains(&port))
}

fn wait_for_callback(port: u16, listener: TcpListener) -> Result<String, String> {
    let outcome = (|| loop {
        let (mut stream, _) = listener.accept().map_err(|e| e.to_string())?;
        if is_cancelled(port) {
            return Err("the sign-in was cancelled".to_string());
        }
        // A client that connects and says nothing mustn't hold the wait.
        let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
        let mut buf = [0u8; 8192];
        let n = stream.read(&mut buf).unwrap_or(0);
        if is_cancelled(port) {
            return Err("the sign-in was cancelled".to_string());
        }
        let head = String::from_utf8_lossy(&buf[..n]);
        match callback_query(head.lines().next().unwrap_or("")) {
            Some(Some(query)) => {
                respond(&mut stream, "200 OK", DONE_PAGE);
                return Ok(query);
            }
            Some(None) => return Err("the sign-in came back without an answer".to_string()),
            None => respond(&mut stream, "404 Not Found", ""),
        }
    })();
    waits(|w| {
        w.active.remove(&port);
        w.cancelled.remove(&port);
    });
    outcome
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

/// The only hosts `provider_http` will talk to.
const HOSTS: [&str; 4] = [
    "oauth2.googleapis.com",
    "www.googleapis.com",
    "login.microsoftonline.com",
    "graph.microsoft.com",
];

fn allowed(url: &str) -> bool {
    url.strip_prefix("https://")
        .and_then(|rest| rest.split(['/', '?', '#']).next())
        .is_some_and(|host| HOSTS.contains(&host))
}

/// Never follows a redirect: the allow-list covers the address the page asked
/// for, and a redirect could name another host (or replay a body there).
fn provider_client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|e| e.to_string())
}

#[derive(serde::Serialize)]
pub struct ProviderReply {
    status: u16,
    body: String,
}

/// One request to a Google or Microsoft API, for the sign-in and upload in
/// ExportSession.ts. The answer's text is returned whatever the status.
#[tauri::command]
pub async fn provider_http(
    method: String,
    url: String,
    headers: Vec<(String, String)>,
    body: Option<Vec<u8>>,
) -> Result<ProviderReply, String> {
    if !allowed(&url) {
        return Err(format!("refusing {url}: not a Google or Microsoft address"));
    }
    let method = reqwest::Method::from_bytes(method.as_bytes()).map_err(|e| e.to_string())?;
    let mut request = provider_client()?.request(method, &url);
    for (name, value) in headers {
        request = request.header(name, value);
    }
    if let Some(bytes) = body {
        request = request.body(bytes);
    }
    let response = request.send().await.map_err(|e| e.to_string())?;
    let status = response.status().as_u16();
    let body = response.text().await.map_err(|e| e.to_string())?;
    Ok(ProviderReply { status, body })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_callback_with_a_query_is_the_answer() {
        assert_eq!(callback_query("GET /callback?code=a&state=b HTTP/1.1"), Some(Some("code=a&state=b".into())));
        assert_eq!(callback_query("GET /callback HTTP/1.1"), Some(None));
        assert_eq!(callback_query("GET /favicon.ico HTTP/1.1"), None);
        assert_eq!(callback_query("GET /callbackx?code=a HTTP/1.1"), None);
    }

    #[test]
    fn provider_http_reaches_only_the_provider_hosts() {
        assert!(allowed("https://graph.microsoft.com/v1.0/sites?search=a"));
        assert!(allowed("https://oauth2.googleapis.com/token"));
        assert!(!allowed("http://graph.microsoft.com/v1.0"));
        assert!(!allowed("https://graph.microsoft.com.evil.test/x"));
        assert!(!allowed("https://evil.test/?https://graph.microsoft.com/"));
    }

    #[test]
    fn provider_client_does_not_follow_a_redirect() {
        let target = TcpListener::bind("127.0.0.1:0").unwrap();
        target.set_nonblocking(true).unwrap();
        let target_port = target.local_addr().unwrap().port();
        let front = TcpListener::bind("127.0.0.1:0").unwrap();
        let front_port = front.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let (mut s, _) = front.accept().unwrap();
            let mut buf = [0u8; 1024];
            let _ = s.read(&mut buf);
            let _ = write!(s, "HTTP/1.1 307 Temporary Redirect\r\nLocation: http://127.0.0.1:{target_port}/\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        });
        let status = tauri::async_runtime::block_on(async {
            provider_client().unwrap().post(format!("http://127.0.0.1:{front_port}/")).body("secret").send().await.unwrap().status().as_u16()
        });
        server.join().unwrap();
        assert_eq!(status, 307);
        assert!(target.accept().is_err(), "the redirect target was contacted");
    }

    #[test]
    fn cancelling_before_the_wait_closes_the_listener() {
        let port = oauth_listen().unwrap();
        oauth_cancel(port);
        assert!(waits(|w| !w.pending.contains_key(&port) && !w.active.contains(&port)));
    }

    #[test]
    fn cancelling_during_the_wait_ends_it() {
        let port = oauth_listen().unwrap();
        let listener = waits(|w| {
            w.active.insert(port);
            w.pending.remove(&port)
        })
        .unwrap();
        let waiter = std::thread::spawn(move || wait_for_callback(port, listener));
        oauth_cancel(port);
        assert!(waiter.join().unwrap().is_err());
        assert!(waits(|w| !w.active.contains(&port) && !w.cancelled.contains(&port)));
    }
}
