mod backend;
#[cfg(target_os = "macos")]
mod macos;
mod notifications;
mod tray;
mod window;

use std::sync::atomic::AtomicU16;
use tauri::RunEvent;

fn main() {
    tauri::Builder::default()
        .manage(AtomicU16::new(0))
        .plugin(tauri_plugin_single_instance::init(|app, _, _| window::show(app)))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_autostart::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .setup(|app| {
            tray::install(app.handle())?;
            notifications::install(app.handle())?;
            let stop = backend::install(app.handle())?;
            #[cfg(target_os = "macos")]
            macos::install_quit_handler(app.handle())?;
            tauri::async_runtime::spawn(backend::run(app.handle().clone(), stop));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("initialize ContribOS")
        .run(|app, event| match event {
            RunEvent::ExitRequested { code: None, api, .. } => {
                api.prevent_exit();
                backend::request_exit(app);
            }
            #[cfg(target_os = "macos")]
            RunEvent::Reopen { .. } => window::show(app),
            _ => {}
        });
}
