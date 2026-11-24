use super::*;
use std::path::Path;

#[test]
fn navigation_is_limited_to_the_selected_loopback_origin() {
    for (url, loads_in_window, opens_in_browser) in [
        ("http://127.0.0.1:8765/#task/abc", true, false),
        ("http://127.0.0.1:8765/desktop/bootstrap?code=x", true, false),
        ("http://127.0.0.1:1234/", false, true),
        ("http://127.0.0.1:1234/desktop/bootstrap?code=x", false, false),
        ("http://localhost:8765/", false, true),
        ("https://127.0.0.1:8765/", false, true),
        ("https://github.com/owner/repo/issues/1", false, true),
        ("http://127.0.0.1:8765@elsewhere.test/", false, false),
        ("file:///tmp/example", false, false),
        ("javascript:alert(1)", false, false),
        ("tauri://localhost/", false, false),
    ] {
        let mut opened = None;
        let allowed = allow_navigation(&Url::parse(url).unwrap(), 8765, |target| opened = Some(target.to_string()));
        assert_eq!(allowed, loads_in_window, "{url}");
        assert_eq!(opened.as_deref(), opens_in_browser.then_some(url), "{url}");
    }
}

#[test]
fn main_webview_has_no_ipc_capabilities() {
    let config: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
    assert_eq!(config["app"]["withGlobalTauri"], false);
    assert!(config["app"]["windows"].as_array().unwrap().is_empty());
    assert!(config["app"]["security"]["capabilities"].as_array().unwrap().is_empty());
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    assert!(root.join("capabilities").read_dir().unwrap().all(|entry| { entry.unwrap().file_name() == ".gitkeep" }));
    assert!(root.read_dir().unwrap().all(|entry| {
        let name = entry.unwrap().file_name();
        !name.to_string_lossy().starts_with("tauri.") || name == "tauri.conf.json"
    }));
}
