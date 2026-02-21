use tauri::Manager;
use std::path::{Path, PathBuf};

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

fn pick_python_bin() -> String {
    if Path::new(PYTHON3).exists() {
        PYTHON3.to_string()
    } else {
        "python3".to_string()
    }
}

fn value_to_non_empty_string(v: Option<&serde_json::Value>) -> Option<String> {
    match v {
        Some(serde_json::Value::String(s)) => {
            let trimmed = s.trim();
            if trimmed.is_empty() {
                None
            } else {
                Some(trimmed.to_string())
            }
        }
        Some(serde_json::Value::Number(n)) => Some(n.to_string()),
        _ => None,
    }
}

fn read_account_registry_entry(data_dir: &Path, account_id: &str) -> Result<serde_json::Value, String> {
    let accounts_file = data_dir.join("accounts.json");
    if !accounts_file.exists() {
        return Err("accounts.json 不存在".to_string());
    }

    let registry: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(&accounts_file).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;

    let entries = registry
        .get("accounts")
        .and_then(|v| v.as_array())
        .ok_or_else(|| "accounts.json 缺少 accounts 字段".to_string())?;

    for entry in entries {
        if entry
            .get("id")
            .and_then(|v| v.as_str())
            .map(|s| s == account_id)
            .unwrap_or(false)
        {
            return Ok(entry.clone());
        }
    }

    Err(format!("未找到账号: {}", account_id))
}

fn is_list_sync_pending(data_dir: &Path, data_dir_rel: &str) -> bool {
    let sync_file = data_dir.join(data_dir_rel).join("sync_state.json");
    if !sync_file.exists() {
        return false;
    }

    let content = match std::fs::read_to_string(&sync_file) {
        Ok(s) => s,
        Err(_) => return false,
    };

    let state: serde_json::Value = match serde_json::from_str(&content) {
        Ok(v) => v,
        Err(_) => return false,
    };

    let phase = state
        .get("fullSync")
        .and_then(|v| v.get("phase"))
        .and_then(|v| v.as_str());

    matches!(phase, Some(p) if p != "done")
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
        let list_sync_pending = is_list_sync_pending(&data_dir, data_dir_rel);
        let meta_file = data_dir.join(data_dir_rel).join("meta.json");

        if meta_file.exists() {
            if let Ok(s) = std::fs::read_to_string(&meta_file) {
                if let Ok(mut v) = serde_json::from_str::<serde_json::Value>(&s) {
                    if let Some(obj) = v.as_object_mut() {
                        obj.insert(
                            "listSyncPending".to_string(),
                            serde_json::Value::Bool(list_sync_pending),
                        );
                    }
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
            "listSyncPending": list_sync_pending,
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

    let python = pick_python_bin();
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

/// Run `python3 gemini_export.py --sync-list-only --user <authuser> --output <appDataDir>`.
#[tauri::command]
async fn run_list_sync(app: tauri::AppHandle, account_id: String) -> Result<String, String> {
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let script = find_script(&app);

    if !script.exists() {
        return Err(format!("脚本未找到: {}", script.display()));
    }

    let entry = read_account_registry_entry(&data_dir, &account_id)?;
    let user_spec = value_to_non_empty_string(entry.get("authuser"))
        .filter(|s| s.chars().all(|c| c.is_ascii_digit()))
        .ok_or_else(|| format!("账号 {} 缺少有效 authuser，请先重新导入账号映射", account_id))?;
    let account_email = value_to_non_empty_string(entry.get("email"));

    let python = pick_python_bin();
    let data_dir_str = data_dir.to_str().unwrap_or("").to_string();
    let script_str = script.to_str().unwrap_or("").to_string();
    let account_id_for_script = account_id.clone();
    let script_dir = script
        .parent()
        .unwrap_or(std::path::Path::new("."))
        .to_path_buf();

    let result = tauri::async_runtime::spawn_blocking(move || {
        let mut cmd = std::process::Command::new(&python);
        cmd.current_dir(&script_dir)
            .arg(&script_str)
            .arg("--sync-list-only")
            .arg("--user")
            .arg(&user_spec)
            .arg("--account-id")
            .arg(&account_id_for_script);
        if let Some(email) = account_email {
            cmd.arg("--account-email").arg(email);
        }
        cmd.arg("--output").arg(&data_dir_str).output()
    })
    .await
    .map_err(|e| e.to_string())?
    .map_err(|e| e.to_string())?;

    if result.status.success() {
        Ok(String::from_utf8_lossy(&result.stdout).to_string())
    } else {
        let stderr = String::from_utf8_lossy(&result.stderr).to_string();
        let stdout = String::from_utf8_lossy(&result.stdout).to_string();
        if stderr.trim().is_empty() {
            Err(stdout)
        } else if stdout.trim().is_empty() {
            Err(stderr)
        } else {
            Err(format!("{}\n{}", stderr, stdout))
        }
    }
}

/// Read `accounts/{id}/conversations.json` and return the `items` array as JSON string.
#[tauri::command]
fn load_conversation_summaries(app: tauri::AppHandle, account_id: String) -> Result<String, String> {
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let conv_file = data_dir
        .join("accounts")
        .join(&account_id)
        .join("conversations.json");

    if !conv_file.exists() {
        return Ok("[]".to_string());
    }

    let raw = std::fs::read_to_string(&conv_file).map_err(|e| e.to_string())?;
    let parsed: serde_json::Value = serde_json::from_str(&raw).map_err(|e| e.to_string())?;
    let items = parsed
        .get("items")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    serde_json::to_string(&items).map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            load_accounts,
            run_accounts_import,
            run_list_sync,
            load_conversation_summaries
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
