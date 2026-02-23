#!/usr/bin/env python3
"""
旧数据修复/兼容/迁移相关工具。

主脚本仅负责主流程，这里承接历史格式处理与一次性修复逻辑。
"""

import datetime
import json
from pathlib import Path

GENERATION_PLACEHOLDER_TOKEN = "image_generation_content/"


def _looks_like_http_url(value):
    return isinstance(value, str) and (
        value.startswith("https://") or value.startswith("http://")
    )


def extract_legacy_ai_media_items_from_message(msg):
    """
    从旧结构 content[0][4][0][4] 中提取 AI 附件项。
    """
    if not (
        isinstance(msg, list)
        and len(msg) > 4
        and msg[4] is not None
        and isinstance(msg[4], list)
        and len(msg[4]) > 0
        and isinstance(msg[4][0], list)
        and len(msg[4][0]) > 4
        and msg[4][0][4] is not None
    ):
        return []
    return [item for item in msg[4][0][4] if isinstance(item, list)]


def resolve_ai_media_items_with_legacy_fallback(ai_media_items, msg):
    """
    当前结构提取不到附件时，尝试旧结构兜底。
    """
    if ai_media_items:
        return ai_media_items
    return extract_legacy_ai_media_items_from_message(msg)


def sanitize_generation_placeholder_text(text, has_attachments):
    """
    在已提取到附件时移除旧占位 URL 文本，避免污染 assistant 正文。
    """
    if not has_attachments or not isinstance(text, str) or GENERATION_PLACEHOLDER_TOKEN not in text:
        return text

    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _looks_like_http_url(stripped) and GENERATION_PLACEHOLDER_TOKEN in stripped:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def has_generation_placeholder_messages(jsonl_file):
    """
    检测旧逻辑遗留的占位 URL 消息（用于触发一次性全量重建）。
    """
    path = Path(jsonl_file)
    if not path.exists():
        return False

    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "message" or row.get("role") != "model":
                continue
            text = row.get("text")
            if isinstance(text, str) and GENERATION_PLACEHOLDER_TOKEN in text:
                return True
    return False


def has_mixed_format_model_image_pairs(jsonl_file):
    """
    检测旧解析写入的同条模型消息 png/jpeg 双格式附件对。
    该结构通常是同源图的多格式变体，需触发一次性重建去重。
    """
    path = Path(jsonl_file)
    if not path.exists():
        return False

    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "message" or row.get("role") != "model":
                continue

            attachments = row.get("attachments")
            if not isinstance(attachments, list) or len(attachments) != 2:
                continue

            image_mimes = []
            for att in attachments:
                if not isinstance(att, dict):
                    continue
                mime = (att.get("mimeType") or "").lower()
                if mime.startswith("image/"):
                    image_mimes.append(mime)
            if len(image_mimes) != 2:
                continue

            mime_set = set(image_mimes)
            if mime_set in ({"image/png", "image/jpeg"}, {"image/png", "image/jpg"}):
                return True

            # Nano Banana 旧数据中还有同格式(png+png)重复变体，常见特征是:
            # - assistant 文本为空
            # - 同条消息仅两张图片附件
            model_name = (row.get("model") or "").lower()
            text = row.get("text")
            text_empty = not isinstance(text, str) or not text.strip()
            if text_empty and "nano banana" in model_name:
                return True
    return False


def has_cross_role_media_echo(jsonl_file):
    """
    检测同一 turn 的 user/model 是否引用了相同 mediaId。
    命中表示历史解析曾把同一附件写入两侧，需要触发一次全量重建修复。
    """
    path = Path(jsonl_file)
    if not path.exists():
        return False

    by_turn = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "message":
                continue
            msg_id = row.get("id")
            if not isinstance(msg_id, str) or len(msg_id) < 3:
                continue
            if msg_id.endswith("_u"):
                turn_id = msg_id[:-2]
                by_turn.setdefault(turn_id, {})["user"] = row
            elif msg_id.endswith("_m"):
                turn_id = msg_id[:-2]
                by_turn.setdefault(turn_id, {})["model"] = row

    for pair in by_turn.values():
        user = pair.get("user")
        model = pair.get("model")
        if not isinstance(user, dict) or not isinstance(model, dict):
            continue

        user_ids = set()
        for att in user.get("attachments") or []:
            if isinstance(att, dict):
                media_id = att.get("mediaId")
                if isinstance(media_id, str) and media_id:
                    user_ids.add(media_id)

        if not user_ids:
            continue

        for att in model.get("attachments") or []:
            if not isinstance(att, dict):
                continue
            media_id = att.get("mediaId")
            if isinstance(media_id, str) and media_id in user_ids:
                return True

    return False


def maybe_apply_generation_placeholder_repair(
    detail_mode,
    local_jsonl_exists,
    raw_turns,
    jsonl_file,
    fetch_full_turns,
):
    """
    旧数据增量模式下若命中占位文本遗留，回退为一次全量重建。
    """
    needs_placeholder_repair = (
        detail_mode == "incremental"
        and local_jsonl_exists
        and not raw_turns
        and has_generation_placeholder_messages(jsonl_file)
    )
    needs_variant_repair = (
        detail_mode == "incremental"
        and local_jsonl_exists
        and not raw_turns
        and has_mixed_format_model_image_pairs(jsonl_file)
    )
    needs_role_echo_repair = (
        detail_mode == "incremental"
        and local_jsonl_exists
        and not raw_turns
        and has_cross_role_media_echo(jsonl_file)
    )
    if not (needs_placeholder_repair or needs_variant_repair or needs_role_echo_repair):
        return detail_mode, raw_turns, None

    repaired_turns = fetch_full_turns()
    if needs_placeholder_repair:
        reason = "placeholder"
    elif needs_variant_repair:
        reason = "format_variant"
    else:
        reason = "role_echo"
    return "full", repaired_turns, reason


def migrate_old_to_new(old_dir, new_base_dir, account_id, email="", name="", exporter_api=None):
    """
    将旧格式目录（{bare_id}.jsonl + chat_list*.json + media/）迁移到新格式。

    exporter_api 需提供:
    - _read_jsonl_rows
    - _turns_to_jsonl_rows
    - _write_jsonl_rows
    - _save_media_manifest_new
    - _write_accounts_json
    - _write_account_meta
    - _write_conversations_index
    - _write_sync_state
    """
    if exporter_api is None:
        raise ValueError("exporter_api is required for migration")

    old_dir = Path(old_dir)
    new_base_dir = Path(new_base_dir)
    now_iso = datetime.datetime.now(datetime.UTC).isoformat()

    account_dir = new_base_dir / "accounts" / account_id
    conv_dir = account_dir / "conversations"
    media_dir = account_dir / "media"

    account_dir.mkdir(parents=True, exist_ok=True)
    conv_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)

    chats = []
    for fname in ["chat_list_union.json", "chat_list.json", "chat_list_latest.json"]:
        candidate = old_dir / fname
        if not candidate.exists():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue

        if isinstance(raw, list):
            chats = raw
        elif isinstance(raw, dict) and "conversations" in raw:
            chats = raw["conversations"]

        if chats:
            print(f"[migrate] 聊天列表: {fname} ({len(chats)} 个)")
            break

    chat_info_map = {}
    for chat in chats:
        cid = chat.get("id", "")
        bare_id = cid.replace("c_", "")
        chat_info_map[bare_id] = chat

    old_media_dir = old_dir / "media"
    if old_media_dir.exists():
        for media_file in old_media_dir.iterdir():
            if not media_file.is_file():
                continue
            target = media_dir / media_file.name
            if target.exists():
                continue
            try:
                target.symlink_to(media_file.resolve())
            except Exception:
                pass
        print(f"[migrate] 媒体文件软链接完成: {media_dir}")

    old_manifest = old_dir / "media_manifest.json"
    if old_manifest.exists():
        try:
            manifest_data = json.loads(old_manifest.read_text(encoding="utf-8"))
            exporter_api._save_media_manifest_new(account_dir, manifest_data.get("url_to_name", {}))
        except Exception:
            pass

    jsonl_files = sorted(old_dir.glob("*.jsonl"))
    print(f"[migrate] 发现 {len(jsonl_files)} 个 JSONL 文件")

    conv_summaries = []
    for jsonl_file in jsonl_files:
        bare_id = jsonl_file.stem
        chat = chat_info_map.get(
            bare_id,
            {
                "id": f"c_{bare_id}",
                "title": bare_id,
                "latest_update_ts": None,
                "latest_update_iso": None,
            },
        )
        conv_id = chat.get("id", f"c_{bare_id}")
        title = chat.get("title", bare_id)

        old_turns = exporter_api._read_jsonl_rows(jsonl_file)
        rows = exporter_api._turns_to_jsonl_rows(old_turns, conv_id, account_id, title, chat)

        new_jsonl = conv_dir / f"{bare_id}.jsonl"
        exporter_api._write_jsonl_rows(new_jsonl, rows)

        meta_row = rows[0]
        msg_rows = [row for row in rows if row.get("type") == "message"]
        has_media = any(row.get("attachments") for row in msg_rows)
        last_text = ""
        for row in reversed(msg_rows):
            text = row.get("text")
            if text:
                last_text = text[:80]
                break

        conv_summaries.append(
            {
                "id": bare_id,
                "title": title,
                "lastMessage": last_text,
                "messageCount": len(msg_rows),
                "hasMedia": has_media,
                "updatedAt": meta_row.get("updatedAt"),
                "syncedAt": meta_row.get("syncedAt"),
                "remoteHash": meta_row.get("remoteHash"),
            }
        )

    avatar_text = (email.split("@")[0][0].upper() if email else account_id[0].upper())
    account_info = {
        "id": account_id,
        "email": email,
        "name": name or (email.split("@")[0] if email else account_id),
        "avatarText": avatar_text,
        "avatarColor": "#667eea",
        "conversationCount": len(conv_summaries),
        "remoteConversationCount": len(chats) or None,
        "lastSyncAt": now_iso,
        "lastSyncResult": "success",
        "authuser": None,
    }
    exporter_api._write_accounts_json(new_base_dir, account_info)
    exporter_api._write_account_meta(account_dir, account_info)
    exporter_api._write_conversations_index(account_dir, account_id, now_iso, conv_summaries)
    exporter_api._write_sync_state(
        account_dir,
        {
            "version": 1,
            "accountId": account_id,
            "updatedAt": now_iso,
            "concurrency": 3,
            "fullSync": {
                "phase": "done",
                "startedAt": now_iso,
                "listingCursor": None,
                "listingTotal": len(chats),
                "listingFetched": len(chats),
                "conversationsToFetch": [],
                "conversationsFetched": len(conv_summaries),
                "conversationsFailed": [],
                "completedAt": now_iso,
                "errorMessage": None,
            },
            "pendingConversations": [],
        },
    )

    print(f"[migrate] 完成: {len(conv_summaries)} 个对话已迁移")
    print(f"[migrate] 新格式目录: {account_dir.absolute()}")
    return account_dir
