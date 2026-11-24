use std::process::Command;
use std::sync::atomic::{AtomicU16, Ordering};
use tauri::webview::NewWindowResponse;
use tauri::{AppHandle, Manager, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};

pub const MAIN: &str = "main";

#[derive(Debug, PartialEq, Eq)]
enum Navigation {
    Allow,
    External,
    Deny,
}

fn navigation(url: &Url, port: u16) -> Navigation {
    if !matches!(url.scheme(), "http" | "https")
        || url.host().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return Navigation::Deny;
    }
    if port != 0
        && url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && url.port_or_known_default() == Some(port)
    {
        Navigation::Allow
    } else if url.path() == "/desktop/bootstrap" {
        Navigation::Deny
    } else {
        Navigation::External
    }
}

fn allow_navigation(app: &AppHandle, url: &Url) -> bool {
    match navigation(url, app.state::<AtomicU16>().load(Ordering::Acquire)) {
        Navigation::Allow => true,
        Navigation::External => {
            let opener = if cfg!(target_os = "macos") { "/usr/bin/open" } else { "xdg-open" };
            let url = url.to_string();
            std::thread::spawn(move || {
                let _ = Command::new(opener).arg(url).status();
            });
            false
        }
        Navigation::Deny => false,
    }
}

pub fn load(app: &AppHandle, port: u16, url: Url) -> tauri::Result<()> {
    app.state::<AtomicU16>().store(port, Ordering::Release);
    if let Some(window) = app.get_webview_window(MAIN) {
        return window.navigate(url);
    }
    let owner = app.clone();
    let popups = app.clone();
    let window = WebviewWindowBuilder::new(app, MAIN, WebviewUrl::External(url))
        .title("ContribOS")
        .inner_size(1200.0, 800.0)
        .on_navigation(move |url| allow_navigation(&owner, url))
        .on_new_window(move |url, _| {
            if allow_navigation(&popups, &url) {
                let _ = popups.get_webview_window(MAIN).map(|window| window.navigate(url));
            }
            NewWindowResponse::Deny
        })
        .build()?;
    let handle = window.clone();
    window.on_window_event(move |event| match event {
        WindowEvent::CloseRequested { api, .. } => {
            api.prevent_close();
            let _ = handle.hide();
        }
        WindowEvent::Focused(true) => restore_task(&handle),
        _ => {}
    });
    Ok(())
}

pub fn show(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN) {
        restore_task(&window);
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

pub fn is_focused(app: &AppHandle) -> bool {
    app.get_webview_window(MAIN).is_some_and(|window| window.is_focused().unwrap_or(false))
}

fn restore_task(window: &WebviewWindow) {
    let Ok(mut url) = window.url() else { return };
    let port = window.app_handle().state::<AtomicU16>().load(Ordering::Acquire);
    if url.path() == "/" && navigation(&url, port) == Navigation::Allow {
        if let Some(task) = crate::notifications::take_recent(window.app_handle()) {
            url.set_fragment(Some(&format!("task/{task}")));
            let _ = window.navigate(url);
        }
    }
}

#[cfg(test)]
#[path = "window_tests.rs"]
mod tests;
