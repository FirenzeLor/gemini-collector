"""
Gemini 数据持久化：JSONL 读写、账号元数据、sync state、媒体清单、对话索引。
"""

import datetime
import json
import uuid
from pathlib import Path

from gemini_protocol import _to_iso_utc, _coerce_epoch_seconds, _iso_to_epoch_seconds

# ============================================================================
# 对话状态常量
# ============================================================================
CONVERSATION_STATUS_NORMAL = "normal"
CONVERSATION_STATUS_LOST = "lost"
CONVERSATION_STATUS_HIDDEN = "hidden"


# ============================================================================
# JSONL 读写与去重
# ============================================================================
def _read_jsonl_rows(jsonl_file):
    rows = []
    if not Path(jsonl_file).exists():
        return rows
    with open(jsonl_file, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _write_jsonl_rows(jsonl_file, rows):
    with open(jsonl_file, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _turn_id_from_raw(raw_turn):
    try:
        ids = raw_turn[0]
        return ids[1] if len(ids) > 1 else ids[0]
    except (IndexError, TypeError):
        return None


def _dedupe_raw_turns_by_id(raw_turns):
    if not isinstance(raw_turns, list) or not raw_turns:
        return raw_turns, 0

    deduped = []
    seen = set()
    removed = 0
    for turn in raw_turns:
        tid = _turn_id_from_raw(turn)
        if isinstance(tid, str) and tid:
            if tid in seen:
                removed += 1
                continue
            seen.add(tid)
        deduped.append(turn)
    return deduped, removed


def _dedupe_message_rows_by_id(message_rows):
    if not isinstance(message_rows, list) or not message_rows:
        return message_rows, 0

    deduped = []
    seen = set()
    removed = 0
    for row in message_rows:
        if not isinstance(row, dict):
            deduped.append(row)
            continue
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            deduped.append(row)
            continue
        if row_id in seen:
            removed += 1
            continue
        seen.add(row_id)
        deduped.append(row)
    return deduped, removed


def _message_row_sort_num(row):
    if not isinstance(row, dict):
        return float("-inf")
    ts = row.get("timestamp")
    if not isinstance(ts, str) or not ts.strip():
        return float("-inf")
    parsed = _iso_to_epoch_seconds(ts)
    if parsed is None:
        return float("-inf")
    return parsed


def _is_message_rows_sorted_by_timestamp(message_rows):
    if not isinstance(message_rows, list) or len(message_rows) <= 1:
        return True
    prev = float("-inf")
    for row in message_rows:
        cur = _message_row_sort_num(row)
        if cur < prev:
            return False
        prev = cur
    return True


def _merge_message_rows_for_write(new_msg_rows, existing_msg_rows):
    """
    合并新增与已有 message 行：
    - 以新增行为优先去重（同 id 取 new）
    - 线性归并（两段输入必须已按 timestamp 升序）
    """
    new_deduped, removed_new_dup = _dedupe_message_rows_by_id(new_msg_rows)
    new_ids = {
        row.get("id")
        for row in new_deduped
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row.get("id")
    }

    existing_without_new = []
    removed_existing_by_new = 0
    for row in existing_msg_rows:
        if (
            isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and row.get("id")
            and row.get("id") in new_ids
        ):
            removed_existing_by_new += 1
            continue
        existing_without_new.append(row)

    existing_deduped, removed_existing_dup = _dedupe_message_rows_by_id(existing_without_new)
    removed_total = removed_new_dup + removed_existing_by_new + removed_existing_dup

    if not _is_message_rows_sorted_by_timestamp(new_deduped):
        raise RuntimeError("new_msg_rows 必须按 timestamp 升序")
    if not _is_message_rows_sorted_by_timestamp(existing_deduped):
        raise RuntimeError("existing_msg_rows 必须按 timestamp 升序")

    merged = []
    i = 0
    j = 0
    while i < len(new_deduped) and j < len(existing_deduped):
        if _message_row_sort_num(new_deduped[i]) <= _message_row_sort_num(existing_deduped[j]):
            merged.append(new_deduped[i])
            i += 1
        else:
            merged.append(existing_deduped[j])
            j += 1
    if i < len(new_deduped):
        merged.extend(new_deduped[i:])
    if j < len(existing_deduped):
        merged.extend(existing_deduped[j:])
    return merged, removed_total


# ============================================================================
# 媒体文件 / 清单
# ============================================================================
def _is_media_file_ready(media_dir, media_id):
    if not isinstance(media_id, str) or not media_id:
        return False
    try:
        p = Path(media_dir) / media_id
        return p.exists() and p.stat().st_size > 0
    except OSError:
        return False


def _load_media_manifest(out_dir):
    manifest_file = Path(out_dir) / "media_manifest.json"
    if not manifest_file.exists():
        return {}
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
        url_map = data.get("url_to_name", {}) if isinstance(data, dict) else {}
        return url_map if isinstance(url_map, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_media_manifest(out_dir, url_to_name):
    manifest_file = Path(out_dir) / "media_manifest.json"
    manifest_file.write_text(
        json.dumps({"url_to_name": url_to_name}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_media_manifest_new(account_dir):
    """从账号目录读取媒体清单"""
    manifest_file = Path(account_dir) / "media_manifest.json"
    if not manifest_file.exists():
        return {}
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
        url_map = data.get("url_to_name", {}) if isinstance(data, dict) else {}
        return url_map if isinstance(url_map, dict) else {}
    except Exception:
        return {}


def _save_media_manifest_new(account_dir, url_to_name):
    """保存媒体清单到账号目录"""
    manifest_file = Path(account_dir) / "media_manifest.json"
    manifest_file.write_text(
        json.dumps({"url_to_name": url_to_name}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_media_id_to_url_map(account_dir):
    url_to_name = _load_media_manifest_new(account_dir)
    media_to_url = {}
    if not isinstance(url_to_name, dict):
        return media_to_url
    for url, media_name in url_to_name.items():
        if not isinstance(url, str) or not isinstance(media_name, str):
            continue
        if media_name not in media_to_url:
            media_to_url[media_name] = url
    return media_to_url


# ============================================================================
# 失败媒体扫描与重试标记
# ============================================================================
def _scan_failed_media_from_rows(rows, media_dir, media_id_to_url):
    pending = []
    recovered = set()
    seen_pending = set()

    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "message":
            continue
        attachments = row.get("attachments")
        if not isinstance(attachments, list):
            continue

        for att in attachments:
            if not isinstance(att, dict):
                continue
            media_id = att.get("mediaId")
            if not isinstance(media_id, str) or not media_id:
                continue

            file_ready = _is_media_file_ready(media_dir, media_id)
            marked_failed = bool(att.get("downloadFailed"))

            if file_ready:
                if marked_failed:
                    recovered.add(media_id)
                continue

            if media_id in seen_pending:
                continue
            seen_pending.add(media_id)

            pending.append({
                "media_id": media_id,
                "url": media_id_to_url.get(media_id),
                "error": att.get("downloadError") if isinstance(att.get("downloadError"), str) else "download_failed",
            })

    return pending, recovered


def _update_jsonl_media_failure_flags(jsonl_file, failed_error_map, recovered_ids):
    rows = _read_jsonl_rows(jsonl_file)
    if not rows:
        return {"marked": 0, "cleared": 0}

    marked = 0
    cleared = 0
    changed = False
    recovered_ids = set(recovered_ids or set())
    failed_error_map = dict(failed_error_map or {})

    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "message":
            continue
        attachments = row.get("attachments")
        if not isinstance(attachments, list):
            continue

        for att in attachments:
            if not isinstance(att, dict):
                continue
            media_id = att.get("mediaId")
            if not isinstance(media_id, str) or not media_id:
                continue

            if media_id in recovered_ids:
                had_failed = "downloadFailed" in att
                had_error = "downloadError" in att
                if had_failed:
                    att.pop("downloadFailed", None)
                if had_error:
                    att.pop("downloadError", None)
                if had_failed or had_error:
                    changed = True
                    cleared += 1
                continue

            if media_id in failed_error_map:
                error_text = failed_error_map.get(media_id) or "download_failed"
                if att.get("downloadFailed") is not True or att.get("downloadError") != error_text:
                    att["downloadFailed"] = True
                    att["downloadError"] = error_text
                    changed = True
                marked += 1

    if changed:
        _write_jsonl_rows(jsonl_file, rows)
    return {"marked": marked, "cleared": cleared}


# ============================================================================
# Turn ID / 行集合工具
# ============================================================================
def _build_existing_turn_id_set(existing_rows):
    out = set()
    for row in existing_rows:
        tid = row.get("turn_id") if isinstance(row, dict) else None
        if isinstance(tid, str) and tid:
            out.add(tid)
    return out


def _latest_ts_from_rows(rows):
    latest = None
    for row in rows:
        ts = row.get("timestamp") if isinstance(row, dict) else None
        if isinstance(ts, int) and (latest is None or ts > latest):
            latest = ts
    return latest


def _build_existing_turn_id_set_new(jsonl_file):
    """从新格式 JSONL 中提取已有 turn_id 集合（跳过 meta 行）"""
    ids = set()
    if not Path(jsonl_file).exists():
        return ids
    with open(jsonl_file, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if row.get("type") != "message":
                    continue
                msg_id = row.get("id", "")
                if msg_id.endswith("_u") or msg_id.endswith("_m"):
                    ids.add(msg_id[:-2])
            except json.JSONDecodeError:
                continue
    return ids


def _count_message_rows_new(jsonl_file):
    """统计新格式 JSONL 中 message 行数量。"""
    count = 0
    if not Path(jsonl_file).exists():
        return 0
    with open(jsonl_file, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") == "message":
                count += 1
    return count


def _count_media_types_from_rows(rows):
    image_count = 0
    video_count = 0
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "message":
            continue
        attachments = row.get("attachments")
        if not isinstance(attachments, list):
            continue
        for att in attachments:
            if not isinstance(att, dict):
                continue
            mime_type = att.get("mimeType")
            if not isinstance(mime_type, str):
                continue
            mime_lower = mime_type.lower()
            if mime_lower.startswith("image/"):
                image_count += 1
            elif mime_lower.startswith("video/"):
                video_count += 1
    return image_count, video_count


def _rows_has_failed_data(rows):
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "message":
            continue
        attachments = row.get("attachments")
        if not isinstance(attachments, list):
            continue
        for att in attachments:
            if isinstance(att, dict) and att.get("downloadFailed") is True:
                return True
    return False


def _remote_hash_from_jsonl(jsonl_file):
    """从新格式 JSONL meta 行读取 remoteHash"""
    if not Path(jsonl_file).exists():
        return None
    with open(jsonl_file, "r", encoding="utf-8") as fh:
        line = fh.readline().strip()
        if not line:
            return None
        try:
            row = json.loads(line)
            if row.get("type") == "meta":
                return row.get("remoteHash")
        except json.JSONDecodeError:
            pass
    return None


# ============================================================================
# Turn → JSONL 转换
# ============================================================================
def _sort_parsed_turns_by_timestamp(parsed_turns):
    if not isinstance(parsed_turns, list) or not parsed_turns:
        return []
    indexed = list(enumerate(parsed_turns))

    def _sort_key(item):
        idx, turn = item
        ts = turn.get("timestamp") if isinstance(turn, dict) else None
        sort_ts = ts if isinstance(ts, int) else 2**63 - 1
        return (sort_ts, idx)

    return [turn for _, turn in sorted(indexed, key=_sort_key)]


def _turns_to_jsonl_rows(parsed_turns, conv_id, account_id, title, chat_info):
    """将 parsed_turns 转为新 JSONL 格式行列表（meta 首行 + message 行）"""
    now_iso = datetime.datetime.now(datetime.UTC).isoformat()
    bare_id = conv_id.replace("c_", "")
    ordered_turns = _sort_parsed_turns_by_timestamp(parsed_turns)

    ts_list = [t["timestamp"] for t in ordered_turns if isinstance(t.get("timestamp"), int)]
    created_at = _to_iso_utc(min(ts_list)) if ts_list else None

    remote_ts = _coerce_epoch_seconds(chat_info.get("latest_update_ts"))
    if remote_ts is None and ts_list:
        remote_ts = max(ts_list)

    updated_at = _to_iso_utc(remote_ts)
    if not updated_at:
        chat_iso = chat_info.get("latest_update_iso")
        if isinstance(chat_iso, str) and chat_iso.strip():
            updated_at = chat_iso.strip()
    if not created_at:
        created_at = updated_at or now_iso

    remote_hash = str(remote_ts) if remote_ts is not None else None

    rows = [{
        "type": "meta",
        "id": bare_id,
        "accountId": account_id,
        "title": title,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "remoteHash": remote_hash,
    }]

    for turn in ordered_turns:
        turn_id = turn.get("turn_id") or uuid.uuid4().hex
        ts = _to_iso_utc(turn.get("timestamp")) or now_iso

        user = turn.get("user", {})
        user_attachments = []
        for f in user.get("files", []):
            media_id = f.get("media_id")
            if not media_id:
                continue
            item = {"mediaId": media_id, "mimeType": f.get("mime") or ""}
            preview_id = f.get("preview_media_id")
            if preview_id:
                item["previewMediaId"] = preview_id
            user_attachments.append(item)
        rows.append({
            "type": "message",
            "id": f"{turn_id}_u",
            "role": "user",
            "text": user.get("text", ""),
            "attachments": user_attachments,
            "timestamp": ts,
        })

        asst = turn.get("assistant", {})
        asst_attachments = []
        for f in asst.get("files", []):
            media_id = f.get("media_id")
            if not media_id:
                continue
            item = {"mediaId": media_id, "mimeType": f.get("mime") or ""}
            preview_id = f.get("preview_media_id")
            if preview_id:
                item["previewMediaId"] = preview_id
            asst_attachments.append(item)
        model_row = {
            "type": "message",
            "id": f"{turn_id}_m",
            "role": "model",
            "text": asst.get("text", ""),
            "attachments": asst_attachments,
            "timestamp": ts,
            "model": asst.get("model", ""),
        }
        thinking = asst.get("thinking", "")
        if thinking:
            model_row["thinking"] = thinking
        rows.append(model_row)

    return rows


# ============================================================================
# 账号元数据 / sync state / conversations 索引
# ============================================================================
def _write_accounts_json(base_dir, account_info):
    """更新根目录 accounts.json"""
    accounts_file = Path(base_dir) / "accounts.json"
    now_iso = datetime.datetime.now(datetime.UTC).isoformat()

    existing = {}
    if accounts_file.exists():
        try:
            data = json.loads(accounts_file.read_text(encoding="utf-8"))
            for a in data.get("accounts", []):
                existing[a["id"]] = a
        except Exception:
            pass

    account_id = account_info["id"]
    existing_account = existing.get(account_id, {})
    authuser = account_info.get("authuser")
    if authuser is None:
        authuser = existing_account.get("authuser")
    existing[account_id] = {
        "id": account_id,
        "email": account_info.get("email", ""),
        "addedAt": existing_account.get("addedAt", now_iso),
        "dataDir": f"accounts/{account_id}",
        "authuser": authuser,
    }

    data = {
        "version": 1,
        "updatedAt": now_iso,
        "accounts": list(existing.values()),
    }
    accounts_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_account_meta(account_dir, account_info):
    """写入 accounts/{id}/meta.json"""
    meta = {
        "version": 1,
        "id": account_info["id"],
        "name": account_info.get("name", ""),
        "email": account_info.get("email", ""),
        "avatarText": account_info.get("avatarText", "?"),
        "avatarColor": account_info.get("avatarColor", "#667eea"),
        "conversationCount": account_info.get("conversationCount", 0),
        "remoteConversationCount": account_info.get("remoteConversationCount"),
        "lastSyncAt": account_info.get("lastSyncAt"),
        "lastSyncResult": account_info.get("lastSyncResult"),
        "authuser": account_info.get("authuser"),
    }
    (Path(account_dir) / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_conversations_index(account_dir, account_id, updated_at, summaries):
    """写入 accounts/{id}/conversations.json"""
    data = {
        "version": 1,
        "accountId": account_id,
        "updatedAt": updated_at,
        "totalCount": len(summaries),
        "items": summaries,
    }
    (Path(account_dir) / "conversations.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_sync_state(account_dir, state):
    """写入 accounts/{id}/sync_state.json"""
    (Path(account_dir) / "sync_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_sync_state(account_dir):
    """读取 accounts/{id}/sync_state.json，失败时返回空 dict。"""
    sync_file = Path(account_dir) / "sync_state.json"
    if not sync_file.exists():
        return {}
    try:
        data = json.loads(sync_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_conversations_index(account_dir):
    """
    读取 accounts/{id}/conversations.json。
    返回: (ordered_ids, index_map)
    """
    conv_file = Path(account_dir) / "conversations.json"
    if not conv_file.exists():
        return [], {}
    try:
        data = json.loads(conv_file.read_text(encoding="utf-8"))
    except Exception:
        return [], {}

    items = data.get("items", []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        return [], {}

    ordered_ids = []
    index_map = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        cid = item.get("id")
        if not isinstance(cid, str) or not cid:
            continue
        ordered_ids.append(cid)
        index_map[cid] = item
    return ordered_ids, index_map


# ============================================================================
# 对话摘要构建
# ============================================================================
def _normalize_conversation_status(value, default=None):
    fallback = default or CONVERSATION_STATUS_NORMAL
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return fallback


def _status_for_remote_summary(existing=None):
    existing = existing if isinstance(existing, dict) else {}
    current_status = _normalize_conversation_status(existing.get("status"))
    if current_status == CONVERSATION_STATUS_HIDDEN:
        return CONVERSATION_STATUS_HIDDEN
    return CONVERSATION_STATUS_NORMAL


def _build_lost_summary(bare_id, existing=None):
    existing = existing if isinstance(existing, dict) else {}
    last_message = existing.get("lastMessage")
    if not isinstance(last_message, str):
        last_message = ""

    message_count = existing.get("messageCount")
    if not isinstance(message_count, int) or message_count < 0:
        message_count = 0

    image_count = existing.get("imageCount")
    if not isinstance(image_count, int) or image_count < 0:
        image_count = 0
    video_count = existing.get("videoCount")
    if not isinstance(video_count, int) or video_count < 0:
        video_count = 0

    return {
        "id": bare_id,
        "title": existing.get("title") or bare_id,
        "lastMessage": last_message,
        "messageCount": message_count,
        "hasMedia": bool(existing.get("hasMedia", False)),
        "hasFailedData": bool(existing.get("hasFailedData", False)),
        "imageCount": image_count,
        "videoCount": video_count,
        "updatedAt": existing.get("updatedAt"),
        "remoteHash": existing.get("remoteHash"),
        "status": CONVERSATION_STATUS_LOST,
    }


def _build_summary_from_chat_listing(chat, existing=None):
    """将列表页 chat 条目转换为 conversations.json 的 summary 条目。"""
    existing = existing if isinstance(existing, dict) else {}
    status = _status_for_remote_summary(existing)
    bare_id = str(chat.get("id", "")).replace("c_", "")
    title = chat.get("title")
    if not isinstance(title, str):
        title = existing.get("title", "")
    remote_ts = _coerce_epoch_seconds(chat.get("latest_update_ts"))
    if remote_ts is not None:
        updated_at = _to_iso_utc(remote_ts)
        remote_hash = str(remote_ts)
    else:
        updated_at = existing.get("updatedAt")
        remote_hash = existing.get("remoteHash")

    msg_count = existing.get("messageCount", 0)
    if not isinstance(msg_count, int):
        msg_count = 0
    image_count = existing.get("imageCount", 0)
    if not isinstance(image_count, int) or image_count < 0:
        image_count = 0
    video_count = existing.get("videoCount", 0)
    if not isinstance(video_count, int) or video_count < 0:
        video_count = 0

    return {
        "id": bare_id,
        "title": title or "",
        "lastMessage": existing.get("lastMessage", ""),
        "messageCount": msg_count,
        "hasMedia": bool(existing.get("hasMedia", False)),
        "hasFailedData": bool(existing.get("hasFailedData", False)),
        "imageCount": image_count,
        "videoCount": video_count,
        "updatedAt": updated_at,
        "remoteHash": remote_hash,
        "status": status,
    }
