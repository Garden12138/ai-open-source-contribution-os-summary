fn main() {
    // Debug runs use the Python interpreter and do not require a bundled binary.
    if std::env::var("PROFILE").as_deref() != Ok("release") {
        let mut config: serde_json::Value =
            serde_json::from_str(&std::env::var("TAURI_CONFIG").unwrap_or_else(|_| "{}".into()))
                .expect("read Tauri config");
        config["bundle"]["externalBin"] = serde_json::Value::Null;
        std::env::set_var("TAURI_CONFIG", config.to_string());
    }
    tauri_build::build();
}
