use super::*;
use std::path::Path;

#[test]
fn navigation_is_limited_to_the_selected_loopback_origin() {
    for (url, expected) in [
        ("http://127.0.0.1:8765/#task/abc", Navigation::Allow),
        ("http://127.0.0.1:8765/desktop/bootstrap?code=x", Navigation::Allow),
        ("http://127.0.0.1:1234/", Navigation::External),
        ("http://127.0.0.1:1234/desktop/bootstrap?code=x", Navigation::Deny),
        ("http://localhost:8765/", Navigation::External),
        ("https://127.0.0.1:8765/", Navigation::External),
        ("https://github.com/owner/repo/issues/1", Navigation::External),
        ("http://127.0.0.1:8765@elsewhere.test/", Navigation::Deny),
        ("file:///tmp/example", Navigation::Deny),
        ("javascript:alert(1)", Navigation::Deny),
        ("tauri://localhost/", Navigation::Deny),
    ] {
        assert_eq!(navigation(&Url::parse(url).unwrap(), 8765), expected, "{url}");
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
