mod worker_host;

use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use tauri::Manager;
use worker_host::EnqueueJobRequest;

const PYTHON3_CANDIDATES: [&str; 3] = [
    "/usr/local/bin/python3",    // Intel Homebrew
    "/opt/homebrew/bin/python3", // Apple Silicon Homebrew
    "/usr/bin/python3",          // macOS system python3
];
// Dev-time script path derived from Cargo.toml location at compile time
const SCRIPT_PATH_DEV: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../scripts/gemini_export.py");
const WORKER_SCRIPT_PATH_DEV: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../scripts/gemini_worker.py");

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

fn find_worker_script(app: &tauri::AppHandle) -> PathBuf {
    let dev = PathBuf::from(WORKER_SCRIPT_PATH_DEV);
    if dev.exists() {
        return dev;
    }
    app.path()
        .resource_dir()
        .unwrap_or_default()
        .join("gemini_worker.py")
}

static PYTHON_BIN: OnceLock<String> = OnceLock::new();

fn init_python_bin() {
    PYTHON_BIN.get_or_init(|| {
        if let Ok(custom) = std::env::var("GEMINI_COLLECTOR_PYTHON") {
            let trimmed = custom.trim();
            if !trimmed.is_empty() {
                return trimmed.to_string();
            }
        }
        for candidate in PYTHON3_CANDIDATES {
            if Path::new(candidate).exists() {
                return candidate.to_string();
            }
        }
        "python3".to_string()
    });
}

fn python_bin() -> &'static str {
    PYTHON_BIN.get().expect("init_python_bin not called")
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

fn normalize_conversation_id(raw: &str) -> String {
    let trimmed = raw.trim();
    if let Some(stripped) = trimmed.strip_prefix("c_") {
        stripped.to_string()
    } else {
        trimmed.to_string()
    }
}

fn conversation_has_failed_data(jsonl_file: &Path) -> bool {
    let raw = match std::fs::read_to_string(jsonl_file) {
        Ok(v) => v,
        Err(_) => return false,
    };
    raw.contains("\"downloadFailed\": true") || raw.contains("\"downloadFailed\":true")
}

#[tauri::command]
fn clear_account_data(
    app: tauri::AppHandle,
    account_id: Option<String>,
    #[allow(non_snake_case)] accountId: Option<String>,
) -> Result<String, String> {
    let account_id = account_id
        .or(accountId)
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "缺少 account_id/accountId 参数".to_string())?;

    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let account_dir = data_dir.join("accounts").join(&account_id);
    let conversations_dir = account_dir.join("conversations");
    let media_dir = account_dir.join("media");
    let conversations_file = account_dir.join("conversations.json");
    let sync_state_file = account_dir.join("sync_state.json");
    let media_manifest_file = account_dir.join("media_manifest.json");
    let meta_file = account_dir.join("meta.json");

    if conversations_dir.exists() {
        std::fs::remove_dir_all(&conversations_dir).map_err(|e| e.to_string())?;
    }
    if media_dir.exists() {
        std::fs::remove_dir_all(&media_dir).map_err(|e| e.to_string())?;
    }
    if conversations_file.exists() {
        std::fs::remove_file(&conversations_file).map_err(|e| e.to_string())?;
    }
    if sync_state_file.exists() {
        std::fs::remove_file(&sync_state_file).map_err(|e| e.to_string())?;
    }
    if media_manifest_file.exists() {
        std::fs::remove_file(&media_manifest_file).map_err(|e| e.to_string())?;
    }

    std::fs::create_dir_all(&conversations_dir).map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&media_dir).map_err(|e| e.to_string())?;

    // Keep account mapping while resetting local sync counters in meta.
    let registry_entry = read_account_registry_entry(&data_dir, &account_id).ok();
    let email_from_registry = registry_entry
        .as_ref()
        .and_then(|v| value_to_non_empty_string(v.get("email")));
    let authuser_from_registry = registry_entry
        .as_ref()
        .and_then(|v| value_to_non_empty_string(v.get("authuser")));

    let mut meta_val = if meta_file.exists() {
        let raw = std::fs::read_to_string(&meta_file).map_err(|e| e.to_string())?;
        serde_json::from_str::<serde_json::Value>(&raw).unwrap_or_else(|_| serde_json::json!({}))
    } else {
        serde_json::json!({})
    };
    if !meta_val.is_object() {
        meta_val = serde_json::json!({});
    }
    let obj = meta_val
        .as_object_mut()
        .ok_or_else(|| "meta.json 格式错误".to_string())?;

    let email = obj
        .get("email")
        .and_then(|v| value_to_non_empty_string(Some(v)))
        .or(email_from_registry)
        .unwrap_or_default();
    let name = obj
        .get("name")
        .and_then(|v| value_to_non_empty_string(Some(v)))
        .unwrap_or_else(|| {
            if email.is_empty() {
                account_id.clone()
            } else {
                email.split('@').next().unwrap_or(&account_id).to_string()
            }
        });
    let avatar_text = obj
        .get("avatarText")
        .and_then(|v| value_to_non_empty_string(Some(v)))
        .unwrap_or_else(|| {
            name.chars()
                .next()
                .map(|c| c.to_uppercase().to_string())
                .unwrap_or_else(|| "?".to_string())
        });
    let avatar_color = obj
        .get("avatarColor")
        .and_then(|v| value_to_non_empty_string(Some(v)))
        .unwrap_or_else(|| "#667eea".to_string());
    let authuser = obj
        .get("authuser")
        .and_then(|v| value_to_non_empty_string(Some(v)))
        .or(authuser_from_registry);

    obj.insert("version".to_string(), serde_json::json!(1));
    obj.insert("id".to_string(), serde_json::json!(account_id));
    obj.insert("name".to_string(), serde_json::json!(name));
    obj.insert("email".to_string(), serde_json::json!(email));
    obj.insert("avatarText".to_string(), serde_json::json!(avatar_text));
    obj.insert("avatarColor".to_string(), serde_json::json!(avatar_color));
    obj.insert("conversationCount".to_string(), serde_json::json!(0));
    obj.insert("remoteConversationCount".to_string(), serde_json::Value::Null);
    obj.insert("lastSyncAt".to_string(), serde_json::Value::Null);
    obj.insert("lastSyncResult".to_string(), serde_json::Value::Null);
    obj.insert(
        "authuser".to_string(),
        authuser
            .map(serde_json::Value::String)
            .unwrap_or(serde_json::Value::Null),
    );

    let serialized = serde_json::to_string_pretty(&meta_val).map_err(|e| e.to_string())?;
    std::fs::write(&meta_file, serialized).map_err(|e| e.to_string())?;

    Ok("{\"status\":\"ok\"}".to_string())
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

    let python = python_bin().to_string();
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

#[tauri::command]
fn enqueue_job(req: EnqueueJobRequest) -> Result<String, String> {
    worker_host::enqueue_job(req)
}

/// Read `accounts/{id}/conversations.json` and return the `items` array as JSON string.
#[tauri::command]
fn load_conversation_summaries(app: tauri::AppHandle, account_id: String) -> Result<String, String> {
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let account_dir = data_dir.join("accounts").join(&account_id);
    let conv_file = data_dir
        .join("accounts")
        .join(&account_id)
        .join("conversations.json");

    if !conv_file.exists() {
        return Ok("[]".to_string());
    }

    let raw = std::fs::read_to_string(&conv_file).map_err(|e| e.to_string())?;
    let parsed: serde_json::Value = serde_json::from_str(&raw).map_err(|e| e.to_string())?;
    let mut items = parsed
        .get("items")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    let conversations_dir = account_dir.join("conversations");
    for item in &mut items {
        let Some(obj) = item.as_object_mut() else {
            continue;
        };
        let cid = obj
            .get("id")
            .and_then(|v| v.as_str())
            .map(|v| v.trim())
            .unwrap_or("");
        if cid.is_empty() {
            obj.insert("hasFailedData".to_string(), serde_json::Value::Bool(false));
            continue;
        }

        let has_failed_data = conversation_has_failed_data(&conversations_dir.join(format!("{}.jsonl", cid)));
        obj.insert(
            "hasFailedData".to_string(),
            serde_json::Value::Bool(has_failed_data),
        );
    }

    serde_json::to_string(&items).map_err(|e| e.to_string())
}

/// Return absolute media directory path for an account: `accounts/{id}/media`.
#[tauri::command]
fn get_account_media_dir(app: tauri::AppHandle, account_id: String) -> Result<String, String> {
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let media_dir = data_dir
        .join("accounts")
        .join(account_id)
        .join("media");
    Ok(media_dir.to_string_lossy().to_string())
}

/// Read one conversation JSONL detail file and return a Conversation object JSON or `null`.
#[tauri::command]
fn load_conversation_detail(
    app: tauri::AppHandle,
    account_id: String,
    conversation_id: String,
) -> Result<String, String> {
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let bare_id = normalize_conversation_id(&conversation_id);
    if bare_id.is_empty() {
        return Ok("null".to_string());
    }

    let jsonl_file = data_dir
        .join("accounts")
        .join(&account_id)
        .join("conversations")
        .join(format!("{}.jsonl", bare_id));

    if !jsonl_file.exists() {
        return Ok("null".to_string());
    }

    let raw = std::fs::read_to_string(&jsonl_file).map_err(|e| e.to_string())?;
    let mut meta: Option<serde_json::Value> = None;
    let mut messages: Vec<serde_json::Value> = Vec::new();
    let mut parse_error_count: usize = 0;
    let mut parse_error_lines: Vec<usize> = Vec::new();

    for (idx, line) in raw.lines().enumerate() {
        let s = line.trim();
        if s.is_empty() {
            continue;
        }
        let row: serde_json::Value = match serde_json::from_str(s) {
            Ok(v) => v,
            Err(_) => {
                parse_error_count += 1;
                if parse_error_lines.len() < 5 {
                    parse_error_lines.push(idx + 1);
                }
                continue;
            }
        };
        match row.get("type").and_then(|v| v.as_str()) {
            Some("meta") => {
                if meta.is_none() {
                    meta = Some(row);
                }
            }
            Some("message") => messages.push(row),
            _ => {}
        }
    }

    let parse_warning = if parse_error_count > 0 {
        let sample_line_str = if parse_error_lines.is_empty() {
            String::new()
        } else {
            format!(
                "（示例行: {}）",
                parse_error_lines
                    .iter()
                    .map(|n| n.to_string())
                    .collect::<Vec<String>>()
                    .join(", ")
            )
        };
        let warning = format!(
            "本地会话数据有 {} 行解析失败{}，已跳过。建议点击该会话右侧同步按钮修复。",
            parse_error_count, sample_line_str
        );
        eprintln!(
            "[load_conversation_detail] account={} conversation={} parse_errors={} lines={:?}",
            account_id, bare_id, parse_error_count, parse_error_lines
        );
        Some(warning)
    } else {
        None
    };

    let meta_val = meta.unwrap_or_else(|| serde_json::json!({}));
    let title = meta_val
        .get("title")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let created_at = meta_val
        .get("createdAt")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let updated_at = meta_val
        .get("updatedAt")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let synced_at = meta_val
        .get("syncedAt")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .unwrap_or_else(|| updated_at.clone());
    let remote_hash = meta_val.get("remoteHash").cloned().unwrap_or(serde_json::Value::Null);
    let account_id_meta = meta_val
        .get("accountId")
        .and_then(|v| v.as_str())
        .unwrap_or(&account_id)
        .to_string();

    let conversation = serde_json::json!({
        "id": bare_id,
        "accountId": account_id_meta,
        "title": title,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "syncedAt": synced_at,
        "remoteHash": remote_hash,
        "parseWarning": parse_warning,
        "messages": messages,
    });

    serde_json::to_string(&conversation).map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    init_python_bin();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .setup(|app| -> Result<(), Box<dyn std::error::Error>> {
            let app_handle = app.handle().clone();
            let output_dir = app_handle.path().app_data_dir()?;
            let worker_script = find_worker_script(&app_handle);
            if !worker_script.exists() {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::NotFound,
                    format!("worker 脚本未找到: {}", worker_script.display()),
                )
                .into());
            }

            worker_host::init_worker_host(
                app_handle,
                python_bin().to_string(),
                worker_script,
                output_dir,
            )
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            load_accounts,
            run_accounts_import,
            enqueue_job,
            clear_account_data,
            load_conversation_summaries,
            get_account_media_dir,
            load_conversation_detail
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|_app, event| {
        if let tauri::RunEvent::ExitRequested { .. } = event {
            worker_host::shutdown_worker_host();
        }
    });
}
