use serde::Deserialize;
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    net::{Ipv4Addr, TcpListener},
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    time::Duration,
};
use tauri::{AppHandle, Manager, Url};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tokio::sync::{mpsc::Receiver, oneshot};
use tokio::time::{sleep, timeout};
type BackendResult<T> = Result<T, Box<dyn std::error::Error>>;

#[derive(Clone)]
pub struct Endpoint {
    origin: Url,
    token: String,
}

struct Control {
    http: reqwest::Client,
    endpoint: Mutex<Option<Endpoint>>,
    stop: Mutex<Option<oneshot::Sender<()>>>,
    exiting: AtomicBool,
}

#[derive(Clone, Copy, Deserialize)]
pub struct Status {
    pub running_tasks: usize,
    pub pending_requests: usize,
}

impl Endpoint {
    pub fn get(&self, http: &reqwest::Client, path: &str) -> reqwest::RequestBuilder {
        http.get(self.origin.join(path).expect("local API URL")).bearer_auth(&self.token)
    }

    pub async fn status(&self, http: &reqwest::Client) -> reqwest::Result<Status> {
        let response = self.get(http, "api/desktop/status").timeout(Duration::from_secs(2)).send().await?;
        response.error_for_status()?.json().await
    }
}

struct Backend {
    port: u16,
    code: String,
    endpoint: Endpoint,
    child: CommandChild,
    events: Receiver<CommandEvent>,
}

fn secret() -> std::io::Result<String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(std::io::Error::other)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn ready_line(line: &[u8], port: u16) -> bool {
    let expected = format!("CONTRIBOS_READY port={port}");
    matches!(std::str::from_utf8(line), Ok(text) if text.trim_end_matches(['\r', '\n']) == expected)
}

impl Backend {
    fn launch(app: &AppHandle) -> BackendResult<Self> {
        let port = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))?.local_addr()?.port();
        let code = secret()?;
        let token = secret()?;
        let origin = Url::parse(&format!("http://127.0.0.1:{port}/"))?;
        let shell = app.shell();
        let command = if cfg!(debug_assertions) {
            let python = PathBuf::from(
                std::env::var_os("CONTRIBOS_DEV_PYTHON")
                    .ok_or("Set CONTRIBOS_DEV_PYTHON to the development venv interpreter.")?,
            );
            if !python.is_absolute() {
                return Err("CONTRIBOS_DEV_PYTHON must be an absolute path.".into());
            }
            shell
                .command(python)
                .current_dir(Path::new(env!("CARGO_MANIFEST_DIR")).join(".."))
                .args(["-m", "contribos"])
        } else {
            shell.sidecar("contribos-backend")?
        };
        let (events, child) = command
            .args(["serve", "--desktop", "--port", &port.to_string()])
            .env("CONTRIBOS_STARTUP_CODE", &code)
            .env("CONTRIBOS_SHELL_TOKEN", &token)
            .env("CONTRIBOS_DESKTOP_SMOKE", if cfg!(debug_assertions) { "0" } else { "1" })
            .set_process_group(true)
            .kill_on_drop(true)
            .spawn()?;
        Ok(Self { port, code, endpoint: Endpoint { origin, token }, child, events })
    }

    fn record(&self, event: &CommandEvent, log: &mut File) {
        if let CommandEvent::Stdout(line) | CommandEvent::Stderr(line) = event {
            let text = String::from_utf8_lossy(line)
                .replace(&self.code, "[REDACTED]")
                .replace(&self.endpoint.token, "[REDACTED]");
            let _ = writeln!(log, "{}", text.trim_end());
        }
    }

    async fn wait(&mut self, log: &mut File, until_ready: bool) -> bool {
        while let Some(event) = self.events.recv().await {
            self.record(&event, log);
            match event {
                CommandEvent::Stdout(line) if until_ready && ready_line(&line, self.port) => return true,
                CommandEvent::Terminated(_) => break,
                _ => {}
            }
        }
        false
    }

    async fn shutdown(mut self, log: &mut File) {
        let _ = writeln!(log, "Stopping backend.");
        // The PyInstaller launcher forwards SIGTERM to its Python child.
        unsafe { libc::kill(self.child.pid() as libc::pid_t, libc::SIGTERM) };
        if timeout(Duration::from_secs(15), self.wait(log, false)).await.is_err() {
            let _ = writeln!(log, "Backend shutdown timed out. Killing process group.");
            let _ = self.child.kill();
        }
    }
}

pub fn install(app: &AppHandle) -> reqwest::Result<oneshot::Receiver<()>> {
    let http = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(2))
        .read_timeout(Duration::from_secs(35))
        .build()?;
    let (sender, receiver) = oneshot::channel();
    app.manage(Control {
        http,
        endpoint: Mutex::new(None),
        stop: Mutex::new(Some(sender)),
        exiting: AtomicBool::new(false),
    });
    Ok(receiver)
}

pub fn request_exit(app: &AppHandle) {
    if app.state::<Control>().exiting.swap(true, Ordering::AcqRel) {
        return;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let control = app.state::<Control>();
        let endpoint = control.endpoint.lock().unwrap().clone();
        let confirm = match endpoint {
            Some(endpoint) => endpoint.status(&control.http).await.map_or(true, |status| status.running_tasks > 0),
            None => false,
        };
        if confirm && !confirm_stop(&app).await {
            control.exiting.store(false, Ordering::Release);
        } else if let Some(stop) = control.stop.lock().unwrap().take() {
            let _ = stop.send(());
        }
    });
}

async fn confirm_stop(app: &AppHandle) -> bool {
    use rfd::{AsyncMessageDialog, MessageButtons, MessageDialogResult};
    let mut dialog = AsyncMessageDialog::new()
        .set_title("Quit ContribOS?")
        .set_description("Running turns will stop. You can continue them next time.")
        .set_buttons(MessageButtons::OkCancelCustom("Stop and Quit".into(), "Cancel".into()));
    if let Some(window) = app.get_webview_window(crate::window::MAIN) {
        crate::window::show(app);
        dialog = dialog.set_parent(&window);
    }
    dialog.show().await == MessageDialogResult::Custom("Stop and Quit".into())
}

pub async fn run(app: AppHandle, stop: oneshot::Receiver<()>) {
    let logs = app.path().app_log_dir().unwrap_or_else(|_| std::env::temp_dir().join("contribos"));
    let path = logs.join("backend.log");
    match run_backend(&app, &path, stop).await {
        Ok(()) => app.exit(0),
        Err(error) => {
            eprintln!("ContribOS: {error}");
            let handle = app.clone();
            let _ = app.run_on_main_thread(move || {
                let message = format!("The local backend is unavailable.\n\nLog: {}", path.display());
                let _ = rfd::MessageDialog::new().set_title("ContribOS").set_description(message).show();
                handle.exit(1);
            });
        }
    }
}

async fn run_backend(app: &AppHandle, path: &Path, mut stop: oneshot::Receiver<()>) -> BackendResult<()> {
    fs::create_dir_all(path.parent().expect("log parent"))?;
    let mut options = OpenOptions::new();
    let mut log =
        options.create(true).write(true).truncate(true).mode(0o600).custom_flags(libc::O_NOFOLLOW).open(path)?;
    let control = app.state::<Control>();
    for attempt in 0..=1 {
        let mut backend = Backend::launch(app).inspect_err(|error| {
            let _ = writeln!(log, "{error}");
        })?;
        let ready = tokio::select! {
            _ = &mut stop => {
                backend.shutdown(&mut log).await;
                return Ok(());
            }
            ready = timeout(Duration::from_secs(20), async {
                let url = backend.endpoint.origin.join("api/health").expect("health URL");
                backend.wait(&mut log, true).await
                    && control.http.get(url).send().await.is_ok_and(|response| response.status().is_success())
            }) => ready.unwrap_or(false),
        };
        if !ready {
            let _ = backend.child.kill();
            let _ = writeln!(log, "Backend readiness check failed.");
            return Err("Backend readiness check failed.".into());
        }
        let _ = writeln!(log, "Backend health check passed.");
        let mut url = backend.endpoint.origin.join("desktop/bootstrap")?;
        url.query_pairs_mut().append_pair("code", &backend.code);
        let port = backend.port;
        let handle = app.clone();
        let (loaded, window) = oneshot::channel();
        app.run_on_main_thread(move || {
            let _ = loaded.send(crate::window::load(&handle, port, url));
        })?;
        window.await??;
        let endpoint = backend.endpoint.clone();
        *control.endpoint.lock().unwrap() = Some(endpoint.clone());
        let requested = tokio::select! {
            biased;
            _ = &mut stop => true,
            _ = backend.wait(&mut log, false) => false,
            _ = poll_status(app, &endpoint, &control.http) => unreachable!(),
            _ = crate::notifications::listen(app, &endpoint, &control.http) => unreachable!(),
        };
        *control.endpoint.lock().unwrap() = None;
        if requested {
            backend.shutdown(&mut log).await;
            return Ok(());
        }
        if attempt == 0 {
            let _ = writeln!(log, "Backend exited unexpectedly. Restarting once.");
            crate::notifications::show(app, "The local backend stopped. ContribOS is restarting it.", None);
        }
    }
    Err("Backend exited unexpectedly.".into())
}

async fn poll_status(app: &AppHandle, endpoint: &Endpoint, http: &reqwest::Client) {
    loop {
        if let Ok(status) = endpoint.status(http).await {
            crate::tray::update(app, status);
        }
        sleep(Duration::from_secs(2)).await;
    }
}

#[cfg(test)]
#[path = "backend_tests.rs"]
mod tests;
