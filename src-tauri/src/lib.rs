mod worker_host;

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use chrono::{DateTime, FixedOffset, Local};
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

#[derive(serde::Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct AccountExportStats {
    account_id: String,
    conversation_count: u64,
    conversation_file_count: u64,
    media_file_count: u64,
    total_file_count: u64,
    total_bytes: u64,
    estimated_zip_bytes: u64,
}

fn resolve_account_id_arg(
    account_id: Option<String>,
    account_id_camel: Option<String>,
) -> Result<String, String> {
    account_id
        .or(account_id_camel)
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "缺少 account_id/accountId 参数".to_string())
}

fn sanitize_file_component(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    for ch in raw.chars() {
        if ch.is_control()
            || matches!(ch, '/' | '\\' | ':' | '*' | '?' | '"' | '<' | '>' | '|')
        {
            out.push('_');
            continue;
        }
        if ch.is_whitespace() {
            out.push('_');
            continue;
        }
        out.push(ch);
    }
    let trimmed = out.trim_matches('_');
    if trimmed.is_empty() {
        "account".to_string()
    } else {
        trimmed.to_string()
    }
}

fn count_files_and_bytes_recursive(root: &Path) -> Result<(u64, u64), String> {
    if !root.exists() {
        return Ok((0, 0));
    }

    let mut files: u64 = 0;
    let mut total_bytes: u64 = 0;
    let mut stack: Vec<PathBuf> = vec![root.to_path_buf()];

    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(&dir).map_err(|e| e.to_string())? {
            let entry = entry.map_err(|e| e.to_string())?;
            let file_type = entry.file_type().map_err(|e| e.to_string())?;
            if file_type.is_dir() {
                stack.push(entry.path());
                continue;
            }
            if !file_type.is_file() {
                continue;
            }
            files += 1;
            total_bytes += entry.metadata().map_err(|e| e.to_string())?.len();
        }
    }

    Ok((files, total_bytes))
}

fn count_jsonl_files(conversations_dir: &Path) -> Result<u64, String> {
    if !conversations_dir.exists() {
        return Ok(0);
    }
    let mut count: u64 = 0;
    for entry in std::fs::read_dir(conversations_dir).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let path = entry.path();
        let file_type = entry.file_type().map_err(|e| e.to_string())?;
        if !file_type.is_file() {
            continue;
        }
        if path.extension().and_then(|s| s.to_str()) == Some("jsonl") {
            count += 1;
        }
    }
    Ok(count)
}

fn conversation_count_from_index(account_dir: &Path) -> Option<u64> {
    let index_file = account_dir.join("conversations.json");
    if !index_file.exists() {
        return None;
    }
    let raw = std::fs::read_to_string(&index_file).ok()?;
    let parsed: serde_json::Value = serde_json::from_str(&raw).ok()?;
    if let Some(items) = parsed.get("items").and_then(|v| v.as_array()) {
        return Some(items.len() as u64);
    }
    parsed.get("totalCount").and_then(|v| v.as_u64())
}

fn account_export_user_label(account_dir: &Path, account_id: &str) -> String {
    let meta_file = account_dir.join("meta.json");
    if meta_file.exists() {
        if let Ok(raw) = std::fs::read_to_string(&meta_file) {
            if let Ok(meta) = serde_json::from_str::<serde_json::Value>(&raw) {
                if let Some(email) = value_to_non_empty_string(meta.get("email")) {
                    let name = email.split('@').next().unwrap_or("").trim();
                    if !name.is_empty() {
                        return name.to_string();
                    }
                }
                if let Some(name) = value_to_non_empty_string(meta.get("name")) {
                    if !name.is_empty() {
                        return name;
                    }
                }
            }
        }
    }
    account_id.to_string()
}

fn build_account_export_stats(account_dir: &Path, account_id: &str) -> Result<AccountExportStats, String> {
    let conversations_dir = account_dir.join("conversations");
    let media_dir = account_dir.join("media");

    let conversation_file_count = count_jsonl_files(&conversations_dir)?;
    let media_file_count = count_files_and_bytes_recursive(&media_dir)?.0;
    let (total_file_count, total_bytes) = count_files_and_bytes_recursive(account_dir)?;
    let conversation_count =
        conversation_count_from_index(account_dir).unwrap_or(conversation_file_count);
    let estimated_zip_bytes = if total_bytes == 0 {
        0
    } else {
        ((total_bytes as f64) * 0.62).round() as u64
    };

    Ok(AccountExportStats {
        account_id: account_id.to_string(),
        conversation_count,
        conversation_file_count,
        media_file_count,
        total_file_count,
        total_bytes,
        estimated_zip_bytes,
    })
}

fn zip_account_dir(account_dir: &Path, zip_path: &Path) -> Result<(), String> {
    let parent = account_dir
        .parent()
        .ok_or_else(|| "账号目录路径异常".to_string())?;
    let folder_name = account_dir
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| "账号目录名称异常".to_string())?;

    let ditto_output = std::process::Command::new("ditto")
        .current_dir(parent)
        .arg("-c")
        .arg("-k")
        .arg("--sequesterRsrc")
        .arg("--keepParent")
        .arg(folder_name)
        .arg(zip_path)
        .output();

    match ditto_output {
        Ok(output) if output.status.success() => return Ok(()),
        Ok(output) => {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            let reason = if stderr.is_empty() {
                format!("ditto 退出码 {:?}", output.status.code())
            } else {
                stderr
            };
            eprintln!("[export_account_zip] ditto 打包失败，尝试 zip 兜底: {}", reason);
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            eprintln!("[export_account_zip] 系统无 ditto，尝试 zip 兜底");
        }
        Err(err) => {
            eprintln!("[export_account_zip] ditto 执行异常，尝试 zip 兜底: {}", err);
        }
    }

    let zip_output = std::process::Command::new("zip")
        .current_dir(parent)
        .arg("-r")
        .arg("-q")
        .arg(zip_path)
        .arg(folder_name)
        .output()
        .map_err(|e| format!("zip 执行失败: {}", e))?;

    if zip_output.status.success() {
        Ok(())
    } else {
        let stderr = String::from_utf8_lossy(&zip_output.stderr).trim().to_string();
        let reason = if stderr.is_empty() {
            format!("zip 退出码 {:?}", zip_output.status.code())
        } else {
            stderr
        };
        Err(format!("zip 打包失败: {}", reason))
    }
}

/// 扫描指定账号、指定时间范围内的 JSONL 文件，累加其中所有 attachment 对应的
/// media 文件大小，返回 `{"totalBytes": N}` JSON 字符串。
/// after_date 为 ISO 8601 字符串（可选），不传则统计全部。
#[tauri::command]
fn get_account_range_bytes(
    app: tauri::AppHandle,
    account_id: Option<String>,
    #[allow(non_snake_case)] accountId: Option<String>,
    after_date: Option<String>,
    #[allow(non_snake_case)] afterDate: Option<String>,
) -> Result<String, String> {
    let account_id = resolve_account_id_arg(account_id, accountId)?;
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let account_dir = data_dir.join("accounts").join(&account_id);
    if !account_dir.exists() {
        return Err(format!("账号目录不存在: {}", account_id));
    }
    let conversations_dir = account_dir.join("conversations");
    let media_dir = account_dir.join("media");

    // after_date 过滤阈值
    let after = after_date
        .or(afterDate)
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());

    if !conversations_dir.exists() {
        let result = serde_json::json!({ "totalBytes": 0u64 });
        return serde_json::to_string(&result).map_err(|e| e.to_string());
    }

    let mut total_bytes: u64 = 0;

    for entry in std::fs::read_dir(&conversations_dir).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let path = entry.path();
        if path.extension().and_then(|s| s.to_str()) != Some("jsonl") {
            continue;
        }

        let raw = match std::fs::read_to_string(&path) {
            Ok(s) => s,
            Err(_) => continue,
        };

        // 先扫描 meta 行获取 updatedAt，如有 after_date 则过滤
        let mut updated_at: Option<String> = None;
        let mut media_ids: Vec<String> = Vec::new();

        for line in raw.lines() {
            let s = line.trim();
            if s.is_empty() { continue; }
            let obj: serde_json::Value = match serde_json::from_str(s) {
                Ok(v) => v,
                Err(_) => continue,
            };
            match obj.get("type").and_then(|v| v.as_str()) {
                Some("meta") => {
                    if updated_at.is_none() {
                        updated_at = obj.get("updatedAt").and_then(|v| v.as_str()).map(|s| s.to_string());
                    }
                }
                Some("message") => {
                    if let Some(atts) = obj.get("attachments").and_then(|v| v.as_array()) {
                        for att in atts {
                            if let Some(mid) = att.get("mediaId").and_then(|v| v.as_str()) {
                                if !mid.is_empty() {
                                    media_ids.push(mid.to_string());
                                }
                            }
                        }
                    }
                }
                _ => {}
            }
        }

        // 时间范围过滤：updatedAt < after_date 则跳过
        if let Some(ref after_str) = after {
            let conv_updated = updated_at.as_deref().unwrap_or("");
            if !conv_updated.is_empty() && conv_updated < after_str.as_str() {
                continue;
            }
        }

        // 累加该会话所有 media 文件大小
        for mid in &media_ids {
            let file_path = media_dir.join(mid);
            if let Ok(meta_fs) = std::fs::metadata(&file_path) {
                total_bytes += meta_fs.len();
            }
        }
    }

    let result = serde_json::json!({ "totalBytes": total_bytes });
    serde_json::to_string(&result).map_err(|e| e.to_string())
}

#[tauri::command]
fn get_account_export_stats(
    app: tauri::AppHandle,
    account_id: Option<String>,
    #[allow(non_snake_case)] accountId: Option<String>,
) -> Result<String, String> {
    let account_id = resolve_account_id_arg(account_id, accountId)?;
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let account_dir = data_dir.join("accounts").join(&account_id);
    if !account_dir.exists() {
        return Err(format!("账号目录不存在: {}", account_id));
    }
    let stats = build_account_export_stats(&account_dir, &account_id)?;
    serde_json::to_string(&stats).map_err(|e| e.to_string())
}

#[tauri::command]
fn export_account_zip(
    app: tauri::AppHandle,
    account_id: Option<String>,
    #[allow(non_snake_case)] accountId: Option<String>,
    output_dir: Option<String>,
    #[allow(non_snake_case)] outputDir: Option<String>,
) -> Result<String, String> {
    let account_id = resolve_account_id_arg(account_id, accountId)?;
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let account_dir = data_dir.join("accounts").join(&account_id);
    if !account_dir.exists() {
        return Err(format!("账号目录不存在: {}", account_id));
    }

    let stats = build_account_export_stats(&account_dir, &account_id)?;
    let user_label = account_export_user_label(&account_dir, &account_id);
    let timestamp = Local::now().format("%Y-%m-%d_%H-%M-%S").to_string();
    let file_name = format!(
        "gemini-{}-{}.zip",
        sanitize_file_component(&user_label),
        timestamp
    );

    let preferred_export_dir = output_dir
        .or(outputDir)
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .map(PathBuf::from);

    let export_dir = preferred_export_dir
        .unwrap_or_else(|| dirs::download_dir().unwrap_or_else(|| data_dir.join("exports")));
    if !export_dir.exists() {
        std::fs::create_dir_all(&export_dir).map_err(|e| e.to_string())?;
    }
    if !export_dir.is_dir() {
        return Err(format!("导出目录不可用: {}", export_dir.display()));
    }

    let zip_path = export_dir.join(file_name);
    if zip_path.exists() {
        std::fs::remove_file(&zip_path).map_err(|e| e.to_string())?;
    }

    zip_account_dir(&account_dir, &zip_path)?;
    let zip_size_bytes = std::fs::metadata(&zip_path)
        .map_err(|e| e.to_string())?
        .len();

    let result = serde_json::json!({
        "accountId": account_id,
        "zipPath": zip_path.to_string_lossy().to_string(),
        "fileName": zip_path.file_name().and_then(|s| s.to_str()).unwrap_or("export.zip"),
        "zipSizeBytes": zip_size_bytes,
        "conversationCount": stats.conversation_count,
        "conversationFileCount": stats.conversation_file_count,
        "mediaFileCount": stats.media_file_count,
        "totalFileCount": stats.total_file_count,
        "totalBytes": stats.total_bytes,
        "estimatedZipBytes": stats.estimated_zip_bytes,
    });
    serde_json::to_string(&result).map_err(|e| e.to_string())
}

// ── Kelivo 导出：纯 Rust 实现 ─────────────────────────────────────────────────

/// 将 UTC 时间加 8 小时得到北京时间数值，以 +00:00 标签输出。
/// Kelivo 内部按 UTC 展示，用此方式让它显示正确的北京时间。
fn to_cst(utc_str: &str) -> String {
    let cst = FixedOffset::east_opt(8 * 3600).unwrap();
    if let Ok(dt) = DateTime::parse_from_rfc3339(utc_str) {
        let cst_dt = dt.with_timezone(&cst);
        return format!("{}+00:00", cst_dt.format("%Y-%m-%dT%H:%M:%S"));
    }
    utc_str.to_string()
}

/// 将 serde_json::Value（字符串或 null）中的时间戳转换为东八区。
fn to_cst_value(v: &serde_json::Value) -> serde_json::Value {
    match v.as_str() {
        Some(s) => serde_json::Value::String(to_cst(s)),
        None => v.clone(),
    }
}

/// 将 "5MB"、"500KB"、"1GB" 等解析为字节数。
fn parse_size(s: &str) -> Result<u64, String> {
    let upper = s.trim().to_uppercase();
    for (suffix, mult) in &[("GB", 1u64 << 30), ("MB", 1u64 << 20), ("KB", 1u64 << 10), ("B", 1u64)] {
        if upper.ends_with(suffix) {
            let num_str = upper[..upper.len() - suffix.len()].trim();
            let val: f64 = num_str.parse().map_err(|_| format!("无法解析大小: {}", s))?;
            return Ok((val * (*mult as f64)).round() as u64);
        }
    }
    s.trim().parse::<u64>().map_err(|_| format!("无法解析大小: {}", s))
}

/// 0→"a", 1→"b", …, 25→"z", 26→"aa", …
fn idx_to_label(mut n: usize) -> String {
    let mut label = String::new();
    n += 1;
    while n > 0 {
        let r = (n - 1) % 26;
        label.insert(0, (b'a' + r as u8) as char);
        n = (n - 1) / 26;
    }
    label
}

fn build_kelivo_content(text: &str, attachments: &[serde_json::Value]) -> String {
    let mut parts: Vec<String> = vec![text.to_string()];
    for att in attachments {
        let media_id = att.get("mediaId").and_then(|v| v.as_str()).unwrap_or("");
        if media_id.is_empty() {
            continue;
        }
        let mime = att.get("mimeType").and_then(|v| v.as_str()).unwrap_or("application/octet-stream");
        if mime.starts_with("image/") {
            parts.push(format!("[image:/upload/{}]", media_id));
        } else {
            parts.push(format!("[file:/upload/{mid}|{mid}|{mime}]", mid = media_id, mime = mime));
        }
    }
    parts.join("\n")
}

struct KelivoItem {
    #[allow(dead_code)]
    conv_id: String,
    kelivo_conv: serde_json::Value,
    kelivo_msgs: Vec<serde_json::Value>,
    media_ids: Vec<String>,
    json_bytes: u64,
    media_bytes: u64,
}

fn parse_kelivo_jsonl(path: &Path, media_dir: &Path, after_date: Option<&str>) -> Result<Option<KelivoItem>, String> {
    let raw = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
    let mut meta: Option<serde_json::Value> = None;
    let mut messages: Vec<serde_json::Value> = Vec::new();

    for line in raw.lines() {
        let s = line.trim();
        if s.is_empty() {
            continue;
        }
        let obj: serde_json::Value = serde_json::from_str(s).map_err(|e| e.to_string())?;
        match obj.get("type").and_then(|v| v.as_str()) {
            Some("meta") => {
                if meta.is_none() {
                    meta = Some(obj);
                }
            }
            Some("message") => messages.push(obj),
            _ => {}
        }
    }

    let meta = match meta {
        Some(m) => m,
        None => return Ok(None),
    };

    // 时间过滤
    if let Some(after) = after_date {
        let updated_at = meta.get("updatedAt").and_then(|v| v.as_str()).unwrap_or("");
        if !updated_at.is_empty() && updated_at < after {
            return Ok(None);
        }
    }

    let conv_id = meta.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let title = meta.get("title").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let created_at = to_cst_value(&meta.get("createdAt").cloned().unwrap_or(serde_json::Value::Null));
    let updated_at = to_cst_value(&meta.get("updatedAt").cloned().unwrap_or(serde_json::Value::Null));

    // 转换消息
    let mut kelivo_msgs: Vec<serde_json::Value> = Vec::new();
    let mut message_ids: Vec<serde_json::Value> = Vec::new();
    let mut media_ids: Vec<String> = Vec::new();

    // 预先标记需要过滤的索引：含 action_card_content 的消息及其前一条 user 消息
    let mut to_remove: std::collections::HashSet<usize> = std::collections::HashSet::new();
    for (i, msg) in messages.iter().enumerate() {
        let text = msg.get("text").and_then(|v| v.as_str()).unwrap_or("");
        if text.contains("action_card_content") {
            to_remove.insert(i);
            for j in (0..i).rev() {
                let role = messages[j].get("role").and_then(|v| v.as_str()).unwrap_or("");
                if role == "user" { to_remove.insert(j); break; }
                if role == "model" { break; }
            }
        }
    }

    for (i, msg) in messages.iter().enumerate() {
        if to_remove.contains(&i) { continue; }
        let text = msg.get("text").and_then(|v| v.as_str()).unwrap_or("");
        let msg_id = msg.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let role_raw = msg.get("role").and_then(|v| v.as_str()).unwrap_or("user");
        let role = if role_raw == "model" { "assistant" } else { "user" };
        let attachments = msg.get("attachments")
            .and_then(|v| v.as_array())
            .map(|a| a.as_slice())
            .unwrap_or(&[]);
        let content = build_kelivo_content(text, attachments);
        let timestamp = to_cst_value(&msg.get("timestamp").cloned().unwrap_or(serde_json::Value::Null));

        for att in attachments {
            if let Some(mid) = att.get("mediaId").and_then(|v| v.as_str()) {
                if !mid.is_empty() && !media_ids.contains(&mid.to_string()) {
                    media_ids.push(mid.to_string());
                }
            }
        }

        message_ids.push(serde_json::Value::String(msg_id.clone()));
        kelivo_msgs.push(serde_json::json!({
            "id": msg_id,
            "role": role,
            "content": content,
            "timestamp": timestamp,
            "modelId": msg.get("model").cloned().unwrap_or(serde_json::Value::Null),
            "providerId": "google",
            "totalTokens": null,
            "conversationId": conv_id,
            "isStreaming": false,
            "reasoningText": msg.get("thinking").cloned().unwrap_or(serde_json::Value::Null),
            "reasoningStartAt": null,
            "reasoningFinishedAt": null,
            "translation": null,
            "reasoningSegmentsJson": null,
            "groupId": null,
            "version": 0,
        }));
    }

    let kelivo_conv = serde_json::json!({
        "id": conv_id,
        "title": title,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "messageIds": message_ids,
        "isPinned": false,
        "mcpServerIds": [],
        "assistantId": null,
        "truncateIndex": -1,
        "versionSelections": {},
        "summary": null,
        "lastSummarizedMessageCount": 0,
    });

    // 估算 JSON 字节数（紧凑格式）
    let conv_bytes = serde_json::to_string(&kelivo_conv).unwrap_or_default().len() as u64;
    let msgs_bytes: u64 = kelivo_msgs.iter()
        .map(|m| serde_json::to_string(m).unwrap_or_default().len() as u64)
        .sum();
    let json_bytes = conv_bytes + msgs_bytes;

    // 媒体文件磁盘大小
    let media_bytes: u64 = media_ids.iter()
        .filter_map(|mid| {
            let p = media_dir.join(mid);
            p.metadata().ok().map(|m| m.len())
        })
        .sum();

    Ok(Some(KelivoItem {
        conv_id,
        kelivo_conv,
        kelivo_msgs,
        media_ids,
        json_bytes,
        media_bytes,
    }))
}

fn pack_bins(items: Vec<KelivoItem>, json_limit: Option<u64>, media_limit: Option<u64>) -> Vec<Vec<KelivoItem>> {
    if json_limit.is_none() && media_limit.is_none() {
        return vec![items];
    }

    // FFD：按归一化权重降序
    let mut indexed: Vec<(usize, f64)> = items.iter().enumerate().map(|(i, item)| {
        let jn = json_limit.map(|lim| item.json_bytes as f64 / lim as f64).unwrap_or(0.0);
        let mn = media_limit.map(|lim| item.media_bytes as f64 / lim as f64).unwrap_or(0.0);
        (i, jn.max(mn))
    }).collect();
    indexed.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

    struct Bin {
        json_used: u64,
        media_used: u64,
        indices: Vec<usize>,
    }

    let mut bins: Vec<Bin> = Vec::new();

    for (idx, _) in indexed {
        let jb = items[idx].json_bytes;
        let mb = items[idx].media_bytes;
        let exceeds = json_limit.map(|lim| jb > lim).unwrap_or(false)
            || media_limit.map(|lim| mb > lim).unwrap_or(false);

        if exceeds {
            bins.push(Bin { json_used: jb, media_used: mb, indices: vec![idx] });
            continue;
        }

        let mut placed = false;
        for bin in &mut bins {
            let json_ok = json_limit.map(|lim| bin.json_used + jb <= lim).unwrap_or(true);
            let media_ok = media_limit.map(|lim| bin.media_used + mb <= lim).unwrap_or(true);
            if json_ok && media_ok {
                bin.indices.push(idx);
                bin.json_used += jb;
                bin.media_used += mb;
                placed = true;
                break;
            }
        }

        if !placed {
            bins.push(Bin { json_used: jb, media_used: mb, indices: vec![idx] });
        }
    }

    // 将 items 按 bin 分组（消耗 items Vec）
    let mut items_opt: Vec<Option<KelivoItem>> = items.into_iter().map(Some).collect();
    bins.into_iter().map(|bin| {
        bin.indices.into_iter().map(|i| items_opt[i].take().unwrap()).collect()
    }).collect()
}

fn write_kelivo_zip(zip_path: &Path, bin_items: &[KelivoItem], media_dir: &Path) -> Result<(usize, usize, usize, usize), String> {
    use zip::write::{ZipWriter, SimpleFileOptions};
    use zip::CompressionMethod;

    let all_convs: Vec<&serde_json::Value> = bin_items.iter().map(|it| &it.kelivo_conv).collect();
    let all_msgs: Vec<&serde_json::Value> = bin_items.iter().flat_map(|it| it.kelivo_msgs.iter()).collect();
    let mut all_mids: Vec<&str> = bin_items.iter().flat_map(|it| it.media_ids.iter().map(|s| s.as_str())).collect();
    all_mids.sort_unstable();
    all_mids.dedup();

    let chats_obj = serde_json::json!({
        "version": 1,
        "conversations": all_convs,
        "messages": all_msgs,
        "toolEvents": {},
        "geminiThoughtSigs": {},
    });
    let chats_json = serde_json::to_string(&chats_obj).map_err(|e| e.to_string())?;

    if let Some(parent) = zip_path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }

    let file = std::fs::File::create(zip_path).map_err(|e| e.to_string())?;
    let mut zw = ZipWriter::new(file);
    let opts = SimpleFileOptions::default()
        .compression_method(CompressionMethod::Deflated)
        .compression_level(Some(6));

    zw.start_file("chats.json", opts).map_err(|e| e.to_string())?;
    zw.write_all(chats_json.as_bytes()).map_err(|e| e.to_string())?;

    let mut media_found = 0usize;
    let mut media_missing = 0usize;
    for mid in &all_mids {
        let src = media_dir.join(mid);
        if src.exists() {
            let data = std::fs::read(&src).map_err(|e| e.to_string())?;
            zw.start_file(format!("upload/{}", mid), opts).map_err(|e| e.to_string())?;
            zw.write_all(&data).map_err(|e| e.to_string())?;
            media_found += 1;
        } else {
            media_missing += 1;
        }
    }

    zw.finish().map_err(|e| e.to_string())?;

    Ok((all_convs.len(), all_msgs.len(), media_found, media_missing))
}

/// 核心 Kelivo 导出实现。
fn kelivo_export_impl(
    data_dir: &Path,
    account_id: &str,
    output_path: &Path,
    json_limit: Option<u64>,
    media_limit: Option<u64>,
    after_date: Option<&str>,
) -> Result<String, String> {
    let account_dir = data_dir.join("accounts").join(account_id);
    let conv_dir = account_dir.join("conversations");
    let media_dir = account_dir.join("media");

    if !conv_dir.exists() {
        return Err(format!("对话目录不存在: {}", conv_dir.display()));
    }

    // 收集并排序 .jsonl 文件
    let mut jsonl_files: Vec<PathBuf> = std::fs::read_dir(&conv_dir)
        .map_err(|e| e.to_string())?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|s| s.to_str()) == Some("jsonl"))
        .collect();
    jsonl_files.sort();

    let mut items: Vec<KelivoItem> = Vec::new();
    let mut skipped = 0usize;

    for jsonl_path in &jsonl_files {
        match parse_kelivo_jsonl(jsonl_path, &media_dir, after_date) {
            Ok(Some(item)) => items.push(item),
            Ok(None) => skipped += 1,
            Err(_) => skipped += 1,
        }
    }

    let total_convs = items.len();
    let total_msgs: usize = items.iter().map(|it| it.kelivo_msgs.len()).sum();

    // 分包
    let bins = pack_bins(items, json_limit, media_limit);
    let multi = bins.len() > 1;

    let stem = output_path.file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("kelivo_backup")
        .to_string();
    let suffix = output_path.extension()
        .and_then(|s| s.to_str())
        .unwrap_or("zip")
        .to_string();
    let output_dir = output_path.parent().unwrap_or(output_path);

    let mut result_lines: Vec<String> = Vec::new();

    for (idx, bin_items) in bins.iter().enumerate() {
        let zip_path = if multi {
            let label = idx_to_label(idx);
            output_dir.join(format!("{}_{}{}",label, stem,
                if suffix.is_empty() { String::new() } else { format!(".{}", suffix) }))
        } else {
            output_path.to_path_buf()
        };

        let (conv_count, msg_count, media_found, media_missing) =
            write_kelivo_zip(&zip_path, bin_items, &media_dir)?;

        let size_mb = std::fs::metadata(&zip_path)
            .map(|m| m.len() as f64 / 1024.0 / 1024.0)
            .unwrap_or(0.0);

        let label_prefix = if multi { format!("[{}] ", idx_to_label(idx)) } else { String::new() };
        result_lines.push(format!(
            "  {}{}  {} 对话  {} 消息  媒体 {}✓/{}✗  {:.1}MB",
            label_prefix,
            zip_path.file_name().and_then(|s| s.to_str()).unwrap_or(""),
            conv_count,
            msg_count,
            media_found,
            media_missing,
            size_mb,
        ));
    }

    let summary = if multi {
        format!(
            "[信息] 成功转换: {} 对话，{} 条消息，跳过 {}\n{}\n[完成] 共 {} 个包，输出到 {}",
            total_convs, total_msgs, skipped,
            result_lines.join("\n"),
            bins.len(),
            output_dir.display(),
        )
    } else {
        let zip_path = output_path;
        let size_mb = std::fs::metadata(zip_path)
            .map(|m| m.len() as f64 / 1024.0 / 1024.0)
            .unwrap_or(0.0);
        format!(
            "[信息] 成功转换: {} 对话，{} 条消息，跳过 {}\n{}\n[完成] 输出: {}  ({:.1} MB)",
            total_convs, total_msgs, skipped,
            result_lines.join("\n"),
            zip_path.display(),
            size_mb,
        )
    };

    Ok(summary)
}

/// 将账号数据导出为 Kelivo 格式 ZIP。
/// output_path 是完整的输出 ZIP 路径（如 /Users/foo/Downloads/kelivo_xxx.zip）。
#[tauri::command]
async fn export_account_kelivo(
    app: tauri::AppHandle,
    account_id: String,
    output_path: String,
    after_date: Option<String>,
) -> Result<String, String> {
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let output = PathBuf::from(&output_path);
    let after = after_date.clone();

    tauri::async_runtime::spawn_blocking(move || {
        kelivo_export_impl(
            &data_dir,
            &account_id,
            &output,
            None,
            None,
            after.as_deref(),
        )
    })
    .await
    .map_err(|e| e.to_string())?
}

/// 将账号数据导出为 Kelivo 格式（分包）ZIP。
#[tauri::command]
async fn export_account_kelivo_split(
    app: tauri::AppHandle,
    account_id: String,
    output_path: String,
    max_json: Option<String>,
    max_upload: Option<String>,
    after_date: Option<String>,
) -> Result<String, String> {
    let json_limit = match &max_json {
        Some(s) if !s.trim().is_empty() => Some(parse_size(s)?),
        _ => None,
    };
    let media_limit = match &max_upload {
        Some(s) if !s.trim().is_empty() => Some(parse_size(s)?),
        _ => None,
    };

    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let output = PathBuf::from(&output_path);
    let after = after_date.clone();

    tauri::async_runtime::spawn_blocking(move || {
        kelivo_export_impl(
            &data_dir,
            &account_id,
            &output,
            json_limit,
            media_limit,
            after.as_deref(),
        )
    })
    .await
    .map_err(|e| e.to_string())?
}

// ── ZIP 导入：纯 Rust 实现 ─────────────────────────────────────────────────

/// 在解压后的临时目录中查找账号数据根目录（含 conversations/ 或 meta.json 的子目录）。
fn find_account_dir_in_zip(tmp_dir: &Path) -> Result<PathBuf, String> {
    if let Ok(entries) = std::fs::read_dir(tmp_dir) {
        for entry in entries.flatten() {
            if entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                let dir = entry.path();
                if dir.join("conversations").is_dir() || dir.join("meta.json").is_file() {
                    return Ok(dir);
                }
            }
        }
    }
    // 兜底：ZIP 未使用 --keepParent，数据直接在根目录
    if tmp_dir.join("conversations").is_dir() {
        return Ok(tmp_dir.to_path_buf());
    }
    Err("ZIP 中未找到有效账号数据（应包含 conversations/ 目录或 meta.json）".to_string())
}

/// 合并源账号的 conversations.json items 到目标账号：
/// 新 id 直接追加；已有 id 则以 updatedAt 较新的一方的 item 数据为准。
fn merge_conversations_index(src_dir: &Path, target_dir: &Path) -> Result<(), String> {
    let target_conv_file = target_dir.join("conversations.json");

    let mut existing_items: Vec<serde_json::Value> = if target_conv_file.exists() {
        let raw = std::fs::read_to_string(&target_conv_file).map_err(|e| e.to_string())?;
        serde_json::from_str::<serde_json::Value>(&raw)
            .ok()
            .and_then(|v| v.get("items").and_then(|a| a.as_array()).cloned())
            .unwrap_or_default()
    } else {
        Vec::new()
    };

    let src_conv_file = src_dir.join("conversations.json");
    if src_conv_file.exists() {
        let raw = std::fs::read_to_string(&src_conv_file).map_err(|e| e.to_string())?;
        if let Some(src_items) = serde_json::from_str::<serde_json::Value>(&raw)
            .ok()
            .and_then(|v| v.get("items").and_then(|a| a.as_array()).cloned())
        {
            for item in src_items {
                let id = item.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
                if id.is_empty() { continue; }
                if let Some(pos) = existing_items.iter().position(|e| {
                    e.get("id").and_then(|v| v.as_str()) == Some(id.as_str())
                }) {
                    // 同 id：比较 updatedAt，source 更新则替换
                    let existing_updated = existing_items[pos]
                        .get("updatedAt").and_then(|v| v.as_str()).unwrap_or("");
                    let src_updated = item.get("updatedAt").and_then(|v| v.as_str()).unwrap_or("");
                    if src_updated > existing_updated {
                        existing_items[pos] = item;
                    }
                } else {
                    existing_items.push(item);
                }
            }
        }
    }

    let out = serde_json::json!({ "items": existing_items });
    std::fs::write(&target_conv_file, serde_json::to_string_pretty(&out).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())
}

/// 将两个同 ID 的 .jsonl 对话文件合并：以 updatedAt 较新的一方 meta 为主，
/// 消息按 id 去重（winner 覆盖 loser 的同 id 消息），最终按 timestamp 排序写回 existing_path。
fn merge_jsonl(existing_path: &Path, src_path: &Path) -> Result<(), String> {
    let parse = |raw: &str| -> (Option<serde_json::Value>, Vec<serde_json::Value>) {
        let mut meta = None;
        let mut msgs = Vec::new();
        for line in raw.lines() {
            let s = line.trim();
            if s.is_empty() { continue; }
            let Ok(obj) = serde_json::from_str::<serde_json::Value>(s) else { continue; };
            match obj.get("type").and_then(|v| v.as_str()) {
                Some("meta") => { if meta.is_none() { meta = Some(obj); } }
                Some("message") => msgs.push(obj),
                _ => {}
            }
        }
        (meta, msgs)
    };

    let existing_raw = std::fs::read_to_string(existing_path).map_err(|e| e.to_string())?;
    let src_raw = std::fs::read_to_string(src_path).map_err(|e| e.to_string())?;
    let (existing_meta, existing_msgs) = parse(&existing_raw);
    let (src_meta, src_msgs) = parse(&src_raw);

    let existing_updated = existing_meta.as_ref()
        .and_then(|m| m.get("updatedAt").and_then(|v| v.as_str())).unwrap_or("");
    let src_updated = src_meta.as_ref()
        .and_then(|m| m.get("updatedAt").and_then(|v| v.as_str())).unwrap_or("");
    let src_is_newer = src_updated > existing_updated;

    let winner_meta = if src_is_newer { src_meta } else { existing_meta };
    let winner_meta = winner_meta.unwrap_or_else(|| serde_json::json!({"type": "meta"}));

    // loser 消息先入 map，winner 消息覆盖同 id 的 loser
    let mut msg_map: std::collections::HashMap<String, serde_json::Value> = std::collections::HashMap::new();
    let (loser_msgs, winner_msgs) = if src_is_newer {
        (&existing_msgs, &src_msgs)
    } else {
        (&src_msgs, &existing_msgs)
    };
    for msg in loser_msgs {
        let id = msg.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
        if !id.is_empty() { msg_map.insert(id, msg.clone()); }
    }
    for msg in winner_msgs {
        let id = msg.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
        if !id.is_empty() { msg_map.insert(id, msg.clone()); }
    }

    let mut merged: Vec<serde_json::Value> = msg_map.into_values().collect();
    merged.sort_by(|a, b| {
        let ta = a.get("timestamp").and_then(|v| v.as_str()).unwrap_or("");
        let tb = b.get("timestamp").and_then(|v| v.as_str()).unwrap_or("");
        ta.cmp(tb)
    });

    let mut out = serde_json::to_string(&winner_meta).map_err(|e| e.to_string())?;
    out.push('\n');
    for msg in &merged {
        out.push_str(&serde_json::to_string(msg).map_err(|e| e.to_string())?);
        out.push('\n');
    }
    std::fs::write(existing_path, out).map_err(|e| e.to_string())
}

/// 核心导入实现：从解压目录复制/合并文件到目标账号目录，更新索引。
fn do_import(src_dir: &Path, target_dir: &Path) -> Result<String, String> {
    let src_conv_dir = src_dir.join("conversations");
    let src_media_dir = src_dir.join("media");
    let target_conv_dir = target_dir.join("conversations");
    let target_media_dir = target_dir.join("media");

    std::fs::create_dir_all(&target_conv_dir).map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&target_media_dir).map_err(|e| e.to_string())?;

    let mut imported_convs: usize = 0;
    let mut merged_convs: usize = 0;
    let mut imported_media: usize = 0;
    let mut skipped_media: usize = 0;

    // 导入 .jsonl 对话文件：已存在则合并，不存在则复制
    if src_conv_dir.exists() {
        for entry in std::fs::read_dir(&src_conv_dir).map_err(|e| e.to_string())? {
            let entry = entry.map_err(|e| e.to_string())?;
            let path = entry.path();
            if !entry.file_type().map_err(|e| e.to_string())?.is_file() { continue; }
            if path.extension().and_then(|s| s.to_str()) != Some("jsonl") { continue; }
            let target_path = target_conv_dir.join(path.file_name().unwrap());
            if target_path.exists() {
                merge_jsonl(&target_path, &path)?;
                merged_convs += 1;
            } else {
                std::fs::copy(&path, &target_path).map_err(|e| e.to_string())?;
                imported_convs += 1;
            }
        }
    }

    // 导入媒体文件（不覆盖已有）
    if src_media_dir.exists() {
        for entry in std::fs::read_dir(&src_media_dir).map_err(|e| e.to_string())? {
            let entry = entry.map_err(|e| e.to_string())?;
            if !entry.file_type().map_err(|e| e.to_string())?.is_file() { continue; }
            let target_path = target_media_dir.join(entry.file_name());
            if target_path.exists() {
                skipped_media += 1;
            } else {
                std::fs::copy(&entry.path(), &target_path).map_err(|e| e.to_string())?;
                imported_media += 1;
            }
        }
    }

    // 合并 conversations.json 索引
    merge_conversations_index(src_dir, target_dir)?;

    serde_json::to_string(&serde_json::json!({
        "importedConversations": imported_convs,
        "mergedConversations": merged_convs,
        "importedMedia": imported_media,
        "skippedMedia": skipped_media,
    }))
    .map_err(|e| e.to_string())
}

/// 解压 ZIP 并导入到当前账号目录，返回导入统计 JSON。
fn import_account_zip_impl(data_dir: &Path, account_id: &str, zip_path: &Path) -> Result<String, String> {
    use std::io::Read;

    let file = std::fs::File::open(zip_path).map_err(|e| format!("打开 ZIP 失败: {}", e))?;
    let mut archive = zip::ZipArchive::new(file).map_err(|e| format!("读取 ZIP 格式失败: {}", e))?;

    let tmp_id = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let tmp_dir = std::env::temp_dir().join(format!("gemini_import_{}", tmp_id));
    std::fs::create_dir_all(&tmp_dir).map_err(|e| format!("创建临时目录失败: {}", e))?;

    let extract_result: Result<(), String> = (|| {
        for i in 0..archive.len() {
            let mut entry = archive.by_index(i).map_err(|e| e.to_string())?;
            let out_path = match entry.enclosed_name() {
                Some(p) => tmp_dir.join(p),
                None => continue,
            };
            if entry.is_dir() {
                std::fs::create_dir_all(&out_path).map_err(|e| e.to_string())?;
            } else {
                if let Some(parent) = out_path.parent() {
                    std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
                }
                let mut data = Vec::new();
                entry.read_to_end(&mut data).map_err(|e| e.to_string())?;
                std::fs::write(&out_path, &data).map_err(|e| e.to_string())?;
            }
        }
        Ok(())
    })();

    if let Err(e) = extract_result {
        let _ = std::fs::remove_dir_all(&tmp_dir);
        return Err(format!("解压 ZIP 失败: {}", e));
    }

    let src_dir = match find_account_dir_in_zip(&tmp_dir) {
        Ok(d) => d,
        Err(e) => {
            let _ = std::fs::remove_dir_all(&tmp_dir);
            return Err(e);
        }
    };

    let target_dir = data_dir.join("accounts").join(account_id);
    let result = do_import(&src_dir, &target_dir);
    let _ = std::fs::remove_dir_all(&tmp_dir);
    result
}

/// 导入 ZIP 压缩包到当前账号。
#[tauri::command]
async fn import_account_zip(
    app: tauri::AppHandle,
    account_id: String,
    zip_path: String,
) -> Result<String, String> {
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let zip = PathBuf::from(&zip_path);

    tauri::async_runtime::spawn_blocking(move || {
        import_account_zip_impl(&data_dir, &account_id, &zip)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn delete_conversation(
    app: tauri::AppHandle,
    account_id: String,
    conversation_id: String,
) -> Result<(), String> {
    let data_dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let account_dir = data_dir.join("accounts").join(&account_id);
    let bare_id = normalize_conversation_id(&conversation_id);

    // 删除 .jsonl 文件
    let conv_file = account_dir.join("conversations").join(format!("{}.jsonl", bare_id));
    if conv_file.exists() {
        std::fs::remove_file(&conv_file).map_err(|e| e.to_string())?;
    }

    // 从 conversations.json 的 items 中移除该条记录，并取得新数量
    let index_file = account_dir.join("conversations.json");
    let new_count: Option<usize> = if index_file.exists() {
        let raw = std::fs::read_to_string(&index_file).map_err(|e| e.to_string())?;
        if let Ok(mut parsed) = serde_json::from_str::<serde_json::Value>(&raw) {
            if let Some(items) = parsed.get_mut("items").and_then(|v| v.as_array_mut()) {
                items.retain(|item| {
                    item.get("id").and_then(|v| v.as_str()) != Some(bare_id.as_str())
                });
                let count = items.len();
                let serialized = serde_json::to_string_pretty(&parsed).map_err(|e| e.to_string())?;
                std::fs::write(&index_file, serialized).map_err(|e| e.to_string())?;
                Some(count)
            } else {
                None
            }
        } else {
            None
        }
    } else {
        None
    };

    // 同步更新 meta.json 中的 conversationCount
    if let Some(count) = new_count {
        let meta_file = account_dir.join("meta.json");
        if meta_file.exists() {
            let raw = std::fs::read_to_string(&meta_file).map_err(|e| e.to_string())?;
            if let Ok(mut meta) = serde_json::from_str::<serde_json::Value>(&raw) {
                if let Some(obj) = meta.as_object_mut() {
                    obj.insert("conversationCount".to_string(), serde_json::json!(count));
                    let serialized = serde_json::to_string_pretty(&meta).map_err(|e| e.to_string())?;
                    std::fs::write(&meta_file, serialized).map_err(|e| e.to_string())?;
                }
            }
        }
    }

    Ok(())
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
        let status = obj
            .get("status")
            .and_then(|v| v.as_str())
            .map(|v| v.trim())
            .filter(|v| !v.is_empty())
            .unwrap_or("normal")
            .to_string();
        obj.insert(
            "status".to_string(),
            serde_json::Value::String(status),
        );

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

    // 为每个 attachment 注入 size 字段（从 media 目录查找文件大小）
    let media_dir = data_dir
        .join("accounts")
        .join(&account_id)
        .join("media");
    for msg in messages.iter_mut() {
        if let Some(atts) = msg.get_mut("attachments").and_then(|v| v.as_array_mut()) {
            for att in atts.iter_mut() {
                if let Some(obj) = att.as_object_mut() {
                    if !obj.contains_key("size") {
                        if let Some(media_id) = obj.get("mediaId").and_then(|v| v.as_str()) {
                            if !media_id.is_empty() {
                                let file_path = media_dir.join(media_id);
                                if let Ok(meta_fs) = std::fs::metadata(&file_path) {
                                    obj.insert("size".to_string(), serde_json::Value::Number(meta_fs.len().into()));
                                }
                            }
                        }
                    }
                }
            }
        }
    }

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
        .plugin(tauri_plugin_dialog::init())
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
            get_account_export_stats,
            get_account_range_bytes,
            export_account_zip,
            export_account_kelivo,
            export_account_kelivo_split,
            import_account_zip,
            clear_account_data,
            delete_conversation,
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
