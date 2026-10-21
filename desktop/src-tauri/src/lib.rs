//! Desktop shell for the EHAI workbench.
//!
//! The window first shows the bundled launcher page, which asks this process whether the local
//! EHAI daemon answers and, if not, starts it with the user's own command. Once the daemon
//! answers, the launcher navigates to the workbench it serves. The shell holds no business
//! state and no credentials: the daemon is started through the user's login shell, so it
//! inherits the same environment (model credentials included) as `ehai up` typed in a terminal.

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::process::Command;
use std::time::Duration;

const DEFAULT_MANAGER_URL: &str = "http://127.0.0.1:8765";
const DEFAULT_UP_COMMAND: &str = "ehai up";

#[derive(serde::Serialize)]
struct Daemon {
    /// Base URL of the daemon, without a trailing slash.
    url: String,
    /// Whether the daemon answered its workspace listing with HTTP 200.
    ready: bool,
    /// The start command used when the launcher has none remembered.
    default_command: String,
}

#[derive(serde::Serialize)]
struct Started {
    ok: bool,
    command: String,
    output: String,
}

fn manager_url() -> String {
    std::env::var("EHAI_MANAGER_URL")
        .ok()
        .map(|value| value.trim().trim_end_matches('/').to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| DEFAULT_MANAGER_URL.to_string())
}

/// Split `http://host:port` into a host and port; the daemon only listens on plain HTTP.
fn host_port(url: &str) -> Option<(String, u16)> {
    let rest = url.strip_prefix("http://")?;
    let authority = rest.split('/').next()?;
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port)) => (host, port.parse().ok()?),
        None => (authority, 80),
    };
    Some((host.to_string(), port))
}

fn answers(url: &str) -> bool {
    let Some((host, port)) = host_port(url) else {
        return false;
    };
    let Some(address) = (host.as_str(), port).to_socket_addrs().ok().and_then(|mut a| a.next()) else {
        return false;
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(800)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let request = format!("GET /api/v1/workspaces HTTP/1.0\r\nHost: {host}:{port}\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut head = [0u8; 16];
    let Ok(read) = stream.read(&mut head) else {
        return false;
    };
    let line = String::from_utf8_lossy(&head[..read]);
    line.starts_with("HTTP/1.1 200") || line.starts_with("HTTP/1.0 200")
}

fn default_command() -> String {
    std::env::var("EHAI_UP_COMMAND")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_UP_COMMAND.to_string())
}

#[tauri::command]
async fn probe() -> Daemon {
    let url = manager_url();
    let target = url.clone();
    let ready = tauri::async_runtime::spawn_blocking(move || answers(&target))
        .await
        .unwrap_or(false);
    Daemon { url, ready, default_command: default_command() }
}

/// Run the start command the user confirmed on the launcher once and report its output; the
/// launcher keeps probing. Only the bundled launcher page can invoke this.
#[tauri::command]
async fn start_daemon(command: Option<String>) -> Started {
    let command = command
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(default_command);
    let line = command.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        if cfg!(target_os = "windows") {
            Command::new("cmd").args(["/C", &line]).output()
        } else {
            // An interactive login shell reads the same startup files as a new terminal
            // (zsh's .zshrc included), so PATH and credential variables match it.
            let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".to_string());
            Command::new(shell)
                .args(["-ilc", &line])
                .stdin(std::process::Stdio::null())
                .output()
        }
    })
    .await;
    match result {
        Ok(Ok(output)) => {
            let mut text = String::from_utf8_lossy(&output.stdout).into_owned();
            text.push_str(&String::from_utf8_lossy(&output.stderr));
            Started { ok: output.status.success(), command, output: text }
        }
        Ok(Err(error)) => Started { ok: false, command, output: error.to_string() },
        Err(error) => Started { ok: false, command, output: error.to_string() },
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![probe, start_daemon])
        .run(tauri::generate_context!())
        .expect("error while running the EHAI desktop shell");
}
