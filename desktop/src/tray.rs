use crate::backend::{self, Status};
use tauri::menu::{CheckMenuItem, Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, Wry};
use tauri_plugin_autostart::ManagerExt;

const ID: &str = "contribos";
struct Counts {
    running: MenuItem<Wry>,
    pending: MenuItem<Wry>,
}

pub fn install(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open ContribOS", true, None::<&str>)?;
    let running = MenuItem::new(app, "Running tasks: …", false, None::<&str>)?;
    let pending = MenuItem::new(app, "Pending requests: …", false, None::<&str>)?;
    let enabled = app.autolaunch().is_enabled().unwrap_or(false);
    let startup = CheckMenuItem::with_id(app, "startup", "Launch at login", true, enabled, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit ContribOS", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &running, &pending, &startup, &quit])?;
    TrayIconBuilder::with_id(ID)
        .icon(app.default_window_icon().expect("bundled icon").clone())
        .icon_as_template(true)
        .tooltip("ContribOS")
        .menu(&menu)
        .on_menu_event(move |app, event| match event.id().as_ref() {
            "open" => crate::window::show(app),
            "quit" => backend::request_exit(app),
            "startup" => {
                let autostart = app.autolaunch();
                let enabled = autostart.is_enabled().unwrap_or(false);
                let changed = if enabled { autostart.disable() } else { autostart.enable() };
                let _ = startup.set_checked(autostart.is_enabled().unwrap_or(enabled));
                if changed.is_err() {
                    let message = "Could not change launch at login.";
                    let _ = rfd::MessageDialog::new().set_title("ContribOS").set_description(message).show();
                }
            }
            _ => {}
        })
        .build(app)?;
    app.manage(Counts { running, pending });
    Ok(())
}

pub fn update(app: &AppHandle, status: Status) {
    let counts = app.state::<Counts>();
    let _ = counts.running.set_text(format!("Running tasks: {}", status.running_tasks));
    let _ = counts.pending.set_text(format!("Pending requests: {}", status.pending_requests));
    if let Some(icon) = app.tray_by_id(ID) {
        let title = (status.running_tasks + status.pending_requests > 0)
            .then(|| format!("{} / {}", status.running_tasks, status.pending_requests));
        let _ = icon.set_title(title);
    }
}
