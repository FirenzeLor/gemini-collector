use rusqlite::{params, Connection};
use std::path::Path;

/// 打开（或创建）指定账号的 search.db，返回连接。
pub fn open_search_db(account_dir: &Path) -> Result<Connection, String> {
    let db_path = account_dir.join("search.db");
    let conn = Connection::open(&db_path).map_err(|e| format!("打开 search.db 失败: {}", e))?;
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA synchronous=NORMAL;",
    )
    .map_err(|e| e.to_string())?;
    create_schema(&conn)?;
    Ok(conn)
}

fn create_schema(conn: &Connection) -> Result<(), String> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS indexed_conversations (
             id    TEXT PRIMARY KEY,
             mtime REAL
         );

         CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
             conversation_id UNINDEXED,
             message_id      UNINDEXED,
             role,
             title,
             text,
             tokenize='trigram'
         );",
    )
    .map_err(|e| format!("创建 search schema 失败: {}", e))
}

fn is_action_card_text(text: &str) -> bool {
    text.contains("action_card_content")
        || text.trim() == "没问题，我可以帮忙。在这些媒体服务提供方中，你想使用哪个？"
}

/// 计算需要从索引/展示中过滤掉的消息下标集合。
/// 规则与前端 visibleMessages 一致：含 action_card_content 的消息，或纯文字 action card，以及其之前最近的 user 消息。
pub fn action_card_indices_to_remove(messages: &[serde_json::Value]) -> std::collections::HashSet<usize> {
    let mut to_remove = std::collections::HashSet::new();
    for (i, msg) in messages.iter().enumerate() {
        let text = msg.get("text").and_then(|v| v.as_str()).unwrap_or("");
        if is_action_card_text(text) {
            to_remove.insert(i);
            for j in (0..i).rev() {
                let role = messages[j].get("role").and_then(|v| v.as_str()).unwrap_or("");
                if role == "user" { to_remove.insert(j); break; }
                if role == "model" { break; }
            }
        }
    }
    to_remove
}

/// 对单个对话文件进行增量索引（删旧插新）。
pub fn index_conversation(conn: &Connection, conv_id: &str, jsonl_path: &Path) -> Result<(), String> {
    let mtime = std::fs::metadata(jsonl_path)
        .map(|m| {
            m.modified()
                .unwrap_or(std::time::UNIX_EPOCH)
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64()
        })
        .unwrap_or(0.0);

    // 检查 mtime 是否变化
    let existing_mtime: Option<f64> = conn
        .query_row(
            "SELECT mtime FROM indexed_conversations WHERE id = ?1",
            params![conv_id],
            |row| row.get(0),
        )
        .ok();

    if let Some(old) = existing_mtime {
        if (old - mtime).abs() < 0.001 {
            return Ok(());
        }
    }

    // 解析 JSONL：先收集所有消息，再统一过滤
    let raw = std::fs::read_to_string(jsonl_path).map_err(|e| e.to_string())?;
    let mut title = String::new();
    let mut messages: Vec<serde_json::Value> = Vec::new();

    for line in raw.lines() {
        let s = line.trim();
        if s.is_empty() { continue; }
        let row: serde_json::Value = match serde_json::from_str(s) {
            Ok(v) => v,
            Err(_) => continue,
        };
        match row.get("type").and_then(|v| v.as_str()) {
            Some("meta") => {
                if let Some(t) = row.get("title").and_then(|v| v.as_str()) {
                    title = t.to_string();
                }
            }
            Some("message") => messages.push(row),
            _ => {}
        }
    }

    // 应用与前端展示一致的过滤规则
    let to_remove = action_card_indices_to_remove(&messages);

    // 删旧行
    conn.execute(
        "DELETE FROM messages_fts WHERE conversation_id = ?1",
        params![conv_id],
    )
    .map_err(|e| e.to_string())?;

    // 插入过滤后的消息
    for (i, msg) in messages.iter().enumerate() {
        if to_remove.contains(&i) { continue; }
        let msg_id = msg.get("id").and_then(|v| v.as_str()).unwrap_or("");
        let role = msg.get("role").and_then(|v| v.as_str()).unwrap_or("");
        let text = msg.get("text").and_then(|v| v.as_str()).unwrap_or("");
        if !text.is_empty() {
            conn.execute(
                "INSERT INTO messages_fts (conversation_id, message_id, role, title, text) VALUES (?1, ?2, ?3, ?4, ?5)",
                params![conv_id, msg_id, role, title, text],
            )
            .map_err(|e| e.to_string())?;
        }
    }

    // 更新 mtime
    conn.execute(
        "INSERT OR REPLACE INTO indexed_conversations (id, mtime) VALUES (?1, ?2)",
        params![conv_id, mtime],
    )
    .map_err(|e| e.to_string())?;

    Ok(())
}

/// 从 conversations 目录全量增量索引。
pub fn index_all(conn: &Connection, conversations_dir: &Path) -> Result<u32, String> {
    if !conversations_dir.exists() {
        return Ok(0);
    }

    // 收集目录中现有的 conversation ids
    let mut file_ids: std::collections::HashSet<String> = std::collections::HashSet::new();
    let entries = std::fs::read_dir(conversations_dir).map_err(|e| e.to_string())?;
    let mut count = 0u32;

    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("jsonl") {
            continue;
        }
        if let Some(stem) = path.file_stem().and_then(|s| s.to_str()) {
            let conv_id = stem.to_string();
            file_ids.insert(conv_id.clone());
            index_conversation(conn, &conv_id, &path)?;
            count += 1;
        }
    }

    // 清理已删除的对话
    let mut stmt = conn
        .prepare("SELECT id FROM indexed_conversations")
        .map_err(|e| e.to_string())?;
    let indexed_ids: Vec<String> = stmt
        .query_map([], |row| row.get(0))
        .map_err(|e| e.to_string())?
        .filter_map(|r| r.ok())
        .collect();

    for id in indexed_ids {
        if !file_ids.contains(&id) {
            conn.execute(
                "DELETE FROM messages_fts WHERE conversation_id = ?1",
                params![&id],
            )
            .map_err(|e| e.to_string())?;
            conn.execute(
                "DELETE FROM indexed_conversations WHERE id = ?1",
                params![&id],
            )
            .map_err(|e| e.to_string())?;
        }
    }

    Ok(count)
}

/// 删除单个对话的索引。
pub fn remove_conversation(conn: &Connection, conv_id: &str) -> Result<(), String> {
    conn.execute(
        "DELETE FROM messages_fts WHERE conversation_id = ?1",
        params![conv_id],
    )
    .map_err(|e| e.to_string())?;
    conn.execute(
        "DELETE FROM indexed_conversations WHERE id = ?1",
        params![conv_id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

#[derive(serde::Serialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct SearchResult {
    pub conversation_id: String,
    pub message_id: String,
    pub title: String,
    pub snippet: String,
    pub role: String,
    pub rank: f64,
}

/// 全文搜索。
pub fn search_messages(
    conn: &Connection,
    query: &str,
    limit: u32,
) -> Result<Vec<SearchResult>, String> {
    let query = query.trim();
    if query.is_empty() {
        return Ok(Vec::new());
    }

    let mut stmt = conn
        .prepare(
            "SELECT conversation_id, message_id, role, title,
                    snippet(messages_fts, 4, '<mark>', '</mark>', '...', 30),
                    bm25(messages_fts, 1.0, 1.0, 0.0, 1.0, 10.0)
             FROM messages_fts
             WHERE messages_fts MATCH ?1
             ORDER BY bm25(messages_fts, 1.0, 1.0, 0.0, 1.0, 10.0)
             LIMIT ?2",
        )
        .map_err(|e| format!("搜索 SQL 错误: {}", e))?;

    let rows = stmt
        .query_map(params![query, limit], |row| {
            Ok(SearchResult {
                conversation_id: row.get(0)?,
                message_id: row.get(1)?,
                role: row.get(2)?,
                title: row.get(3)?,
                snippet: row.get(4)?,
                rank: row.get(5)?,
            })
        })
        .map_err(|e| format!("搜索查询失败: {}", e))?;

    let mut results = Vec::new();
    for row in rows {
        if let Ok(r) = row {
            results.push(r);
        }
    }
    Ok(results)
}
