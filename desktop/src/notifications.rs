use crate::{backend::Endpoint, tray, window};
use serde::Deserialize;
use std::{sync::Mutex, time::Duration};
use tauri::{AppHandle, Manager};
use tauri_plugin_notification::NotificationExt;

#[derive(Deserialize)]
struct Notice {
    category: String,
    title: String,
    task_id: Option<String>,
}

#[derive(Default)]
struct Recent(Mutex<Option<String>>);

fn valid_task(id: &str) -> bool {
    (1..=80).contains(&id.len()) && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

pub fn install(app: &AppHandle) -> tauri_plugin_notification::Result<()> {
    app.manage(Recent::default());
    let handle = app.clone();
    app.notification().on_action(move |action| {
        let app = handle.clone();
        let target = action.notification().map(|notice| {
            notice
                .extra()
                .get("task_id")
                .and_then(|value| value.as_str())
                .filter(|id| valid_task(id))
                .map(str::to_owned)
        });
        let _ = handle.run_on_main_thread(move || {
            if let Some(target) = target {
                *app.state::<Recent>().0.lock().unwrap() = target;
            }
            window::show(&app);
        });
    })
}

pub fn take_recent(app: &AppHandle) -> Option<String> {
    app.state::<Recent>().0.lock().unwrap().take()
}

pub fn show(app: &AppHandle, body: &str, task: Option<String>) {
    if app.notification().builder().title("ContribOS").body(body).extra("task_id", &task).show().is_ok() {
        *app.state::<Recent>().0.lock().unwrap() = task;
    }
}

fn parse_frame(frame: &[u8]) -> Option<Notice> {
    let frame = frame.strip_prefix(b"event: notification\ndata: ")?;
    serde_json::from_slice(frame).ok()
}

async fn receive(app: &AppHandle, endpoint: &Endpoint, http: &reqwest::Client) -> Option<()> {
    let mut response = endpoint.get(http, "api/notifications/stream").send().await.ok()?.error_for_status().ok()?;
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.ok()? {
        bytes.extend_from_slice(&chunk);
        while let Some(end) = bytes.windows(2).position(|pair| pair == b"\n\n") {
            if let Some(notice) = parse_frame(&bytes[..end]) {
                if notice.category != "turn_completed" || !window::is_focused(app) {
                    show(app, &notice.title, notice.task_id.filter(|id| valid_task(id)));
                }
                if let Ok(status) = endpoint.status(http).await {
                    tray::update(app, status);
                }
            }
            bytes.drain(..end + 2);
        }
        if bytes.len() > 32 * 1024 {
            break;
        }
    }
    Some(())
}

pub async fn listen(app: &AppHandle, endpoint: &Endpoint, http: &reqwest::Client) {
    loop {
        receive(app, endpoint, http).await;
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

#[cfg(test)]
#[path = "notifications_tests.rs"]
mod tests;
