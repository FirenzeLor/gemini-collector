use tauri::Manager;
use std::path::PathBuf;

const PYTHON3: &str = "/usr/local/bin/python3";
// Dev-time script path derived from Cargo.toml location at compile time
const SCRIPT_PATH_DEV: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../scripts/gemini_export.py");

fn find_script(app: &tauri::AppHandle) -> PathBuf {
    let dev = PathBuf::from(SCRIPT_PATH_DEV);
    if dev.exists() {
        return dev;
    }
    // Production: bundled as resource
    app.path()
        .resource_dir()
        .unwrap_or_default()
        .join("gemini_export.py")
}

/// Read accounts.json + each account's meta.json from app data dir.
/// Returns a JSON array of Account objects (matches AccountMeta schema), or "[]".
#[tauri::command]
fn load_accounts(app: tauri::AppHandle) -> Result<String, String> {
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let accounts_file = data_dir.join("accounts.json");

    if !accounts_file.exists() {
        return Ok("[]".to_string());
    }

    let registry: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(&accounts_file).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;

    let entries = match registry.get("accounts").and_then(|v| v.as_array()) {
        Some(a) => a.clone(),
        None => return Ok("[]".to_string()),
    };

    let mut result: Vec<serde_json::Value> = Vec::new();
    for entry in &entries {
        let data_dir_rel = entry
            .get("dataDir")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let meta_file = data_dir.join(data_dir_rel).join("meta.json");

        if meta_file.exists() {
            if let Ok(s) = std::fs::read_to_string(&meta_file) {
                if let Ok(v) = serde_json::from_str::<serde_json::Value>(&s) {
                    result.push(v);
                    continue;
                }
            }
        }

        // meta.json missing — build minimal entry from registry
        let id = entry
            .get("id")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown");
        let email = entry.get("email").and_then(|v| v.as_str()).unwrap_or("");
        let authuser = entry.get("authuser").and_then(|v| v.as_str());
        let name = email.split('@').next().unwrap_or(id);
        let avatar = name
            .chars()
            .next()
            .map(|c| c.to_uppercase().to_string())
            .unwrap_or_else(|| "?".to_string());
        result.push(serde_json::json!({
            "id": id,
            "name": name,
            "email": email,
            "avatarText": avatar,
            "avatarColor": "#667eea",
            "conversationCount": 0,
            "remoteConversationCount": null,
            "lastSyncAt": null,
            "lastSyncResult": null,
            "authuser": authuser,
        }));
    }

    serde_json::to_string(&result).map_err(|e| e.to_string())
}

/// Run `python3 gemini_export.py --accounts-only --output <appDataDir>`.
/// Returns stdout on success, or an error string.
#[tauri::command]
async fn run_accounts_import(app: tauri::AppHandle) -> Result<String, String> {
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let script = find_script(&app);

    if !script.exists() {
        return Err(format!("脚本未找到: {}", script.display()));
    }

    let python = if std::path::Path::new(PYTHON3).exists() {
        PYTHON3.to_string()
    } else {
        "python3".to_string()
    };
    let data_dir_str = data_dir.to_str().unwrap_or("").to_string();
    let script_str = script.to_str().unwrap_or("").to_string();
    let script_dir = script
        .parent()
        .unwrap_or(std::path::Path::new("."))
        .to_path_buf();

    let result = tauri::async_runtime::spawn_blocking(move || {
        std::process::Command::new(&python)
            .current_dir(&script_dir)  // ensure cdp_mode.py is resolvable
            .arg(&script_str)
            .arg("--accounts-only")
            .arg("--output")
            .arg(&data_dir_str)
            .output()
    })
    .await
    .map_err(|e| e.to_string())?
    .map_err(|e| e.to_string())?;

    if result.status.success() {
        Ok(String::from_utf8_lossy(&result.stdout).to_string())
    } else {
        Err(String::from_utf8_lossy(&result.stderr).to_string())
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![load_accounts, run_accounts_import])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
