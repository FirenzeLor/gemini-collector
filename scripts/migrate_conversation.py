#!/usr/bin/env python3
"""从旧导出目录迁移单个对话到 app 数据目录（强制覆盖）。

用法:
  python3 scripts/migrate_conversation.py <source_dir> <conversation_id> [--account <authuser或account_id>]

示例:
  python3 scripts/migrate_conversation.py ~/Downloads/gemini-user4-full-export 8c98a0387383e99c
  python3 scripts/migrate_conversation.py ~/Downloads/gemini-user4-full-export 8c98a0387383e99c --account 4
  python3 scripts/migrate_conversation.py ~/Downloads/gemini-user4-full-export 8c98a0387383e99c --account cynaustine88_gmail_com

源目录结构 (旧导出格式):
  <source_dir>/
    ├── <id>.jsonl           (turn-based 格式)
    ├── chat_list_full.json  (可选, 含标题等元数据)
    └── media/               (媒体文件)

目标 (app 格式):
  ~/Library/Application Support/com.gemini-collector.app/accounts/<account_id>/
    ├── conversations.json   (索引, 会更新)
    ├── conversations/<id>.jsonl  (message-based 格式)
    └── media/               (媒体文件)
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

APP_DATA = Path.home() / "Library" / "Application Support" / "com.gemini-collector.app"
ACCOUNTS_JSON = APP_DATA / "accounts.json"
INTERNAL_PLACEHOLDER_PATH_RE = re.compile(r"(?:^|/)[a-z0-9_]+_content(?:/|$)")


def _is_internal_placeholder_content_url(url_text: str) -> bool:
    if not isinstance(url_text, str):
        return False
    candidate = url_text.strip().rstrip("。.,;，；）)]}\"'")
    if not candidate.startswith(("https://", "http://")):
        return False
    try:
        parsed = urlparse(candidate)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if not (host == "googleusercontent.com" or host.endswith(".googleusercontent.com")):
        return False
    path = (parsed.path or "").lower()
    return bool(INTERNAL_PLACEHOLDER_PATH_RE.search(path))


def sanitize_internal_placeholder_text(text: str | None) -> str:
    if not isinstance(text, str):
        return ""
    if "_content/" not in text or "googleusercontent.com" not in text:
        return text
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        urls = re.findall(r"https?://\S+", stripped)
        if any(_is_internal_placeholder_content_url(url) for url in urls):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def load_accounts():
    with open(ACCOUNTS_JSON) as f:
        return json.load(f)["accounts"]


def resolve_account(hint: str | None, conv_id: str) -> dict:
    """根据 hint 或自动匹配找到目标账号。"""
    accounts = load_accounts()

    if hint:
        for acc in accounts:
            if acc["authuser"] == hint or acc["id"] == hint or acc["email"] == hint:
                return acc
        print(f"[错误] 未找到匹配的账号: {hint}")
        print(f"  可用账号: {', '.join(a['authuser'] + '=' + a['id'] for a in accounts)}")
        sys.exit(1)

    # 自动匹配: 扫描所有账号的 conversations.json 找 conv_id
    matched = []
    for acc in accounts:
        conv_json = APP_DATA / acc["dataDir"] / "conversations.json"
        if not conv_json.exists():
            continue
        with open(conv_json) as f:
            data = json.load(f)
        if any(item["id"] == conv_id for item in data.get("items", [])):
            matched.append(acc)

    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        print(f"[错误] 对话 {conv_id} 存在于多个账号中, 请用 --account 指定:")
        for a in matched:
            print(f"  {a['authuser']} = {a['id']}")
        sys.exit(1)

    print(f"[错误] 对话 {conv_id} 未在任何账号中找到, 请用 --account 指定目标账号")
    print(f"  可用账号: {', '.join(a['authuser'] + '=' + a['id'] for a in accounts)}")
    sys.exit(1)


def find_source_jsonl(src_dir: Path, conv_id: str) -> Path:
    """定位源 JSONL 文件。"""
    # 直接匹配
    p = src_dir / f"{conv_id}.jsonl"
    if p.exists():
        return p
    # 可能传入的是带 c_ 前缀的 id
    if conv_id.startswith("c_"):
        p = src_dir / f"{conv_id[2:]}.jsonl"
        if p.exists():
            return p
    print(f"[错误] 源文件不存在: {p}")
    sys.exit(1)


def load_source_metadata(src_dir: Path, conv_id: str) -> dict | None:
    """从 chat_list_full.json 等文件中查找对话元数据。"""
    for name in ("chat_list_full.json", "chat_list_union.json", "chat_list_api_latest.json", "chat_list.json"):
        p = src_dir / name
        if not p.exists():
            continue
        with open(p) as f:
            items = json.load(f)
        if not isinstance(items, list):
            continue
        for item in items:
            raw_id = item.get("id", "")
            cid = raw_id[2:] if raw_id.startswith("c_") else raw_id
            if cid == conv_id:
                return item
    return None


def parse_source_turns(jsonl_path: Path) -> list[dict]:
    """解析源 JSONL (turn-based 格式)。"""
    turns = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            turns.append(json.loads(line))
    return turns


def normalize_turn_order(turns: list[dict]) -> list[dict]:
    """将 turn 顺序归一为旧->新。"""
    if len(turns) < 2:
        return turns

    def ts_value(turn: dict) -> float | None:
        ts = turn.get("timestamp_iso")
        if not isinstance(ts, str) or not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    first = ts_value(turns[0])
    last = ts_value(turns[-1])
    if first is None or last is None:
        return turns
    if first > last:
        return list(reversed(turns))
    return turns


def convert_to_app_format(turns: list[dict], conv_id: str, account_id: str, metadata: dict | None) -> tuple[list[dict], set[str]]:
    """将 turn-based 数据转换为 app message-based 格式。返回 (行列表, 引用的media_id集合)。"""
    media_ids = set()
    lines = []
    turns = normalize_turn_order(turns)

    # 构建 meta 行
    if turns:
        first_ts = turns[0].get("timestamp_iso", "")
        last_ts = turns[-1].get("timestamp_iso", first_ts)
    else:
        first_ts = last_ts = ""

    title = ""
    remote_hash = ""
    if metadata:
        title = metadata.get("title", "")
        ts = metadata.get("latest_update_ts")
        if ts:
            remote_hash = str(ts)
            last_ts = metadata.get("latest_update_iso", last_ts)

    if not title and turns:
        user_text = turns[0].get("user", {}).get("text", "")
        title = user_text[:200] if user_text else ""

    now = datetime.now(timezone.utc).isoformat()
    meta = {
        "type": "meta",
        "id": conv_id,
        "accountId": account_id,
        "title": title,
        "createdAt": first_ts,
        "updatedAt": last_ts,
        "syncedAt": now,
        "remoteHash": remote_hash,
    }
    lines.append(meta)

    # 转换每个 turn
    for turn in turns:
        turn_id = turn.get("turn_id", "")
        ts = turn.get("timestamp_iso", "")
        user = turn.get("user", {})
        assistant = turn.get("assistant", {})

        # user message
        user_msg = {
            "type": "message",
            "id": f"{turn_id}_u",
            "role": "user",
            "text": user.get("text", ""),
            "attachments": [],
            "timestamp": ts,
        }
        for f in user.get("files", []):
            mid = f.get("media_id", "")
            if mid:
                media_ids.add(mid)
                user_msg["attachments"].append({
                    "mediaId": mid,
                    "mimeType": f.get("mime", ""),
                })
        lines.append(user_msg)

        # model message
        if assistant.get("text") is not None or assistant.get("files"):
            model_msg = {
                "type": "message",
                "id": f"{turn_id}_m",
                "role": "model",
                "text": sanitize_internal_placeholder_text(assistant.get("text", "")),
                "attachments": [],
                "timestamp": ts,
            }
            if assistant.get("model"):
                model_msg["model"] = assistant["model"]
            if assistant.get("thinking"):
                model_msg["thinking"] = assistant["thinking"]
            for f in assistant.get("files", []):
                mid = f.get("media_id", "")
                if mid:
                    media_ids.add(mid)
                    model_msg["attachments"].append({
                        "mediaId": mid,
                        "mimeType": f.get("mime", ""),
                    })
            lines.append(model_msg)

    return lines, media_ids


def update_conversations_json(conv_json_path: Path, conv_id: str, account_id: str, lines: list[dict]):
    """更新目标 conversations.json 索引。"""
    if conv_json_path.exists():
        with open(conv_json_path) as f:
            data = json.load(f)
    else:
        data = {"version": 1, "accountId": account_id, "updatedAt": "", "totalCount": 0, "items": []}

    meta = lines[0]  # type: "meta" 行
    messages = [l for l in lines if l.get("type") == "message"]
    msg_count = len(messages)
    image_count = sum(
        1 for m in messages
        for a in m.get("attachments", [])
        if a.get("mimeType", "").startswith("image/")
    )
    video_count = sum(
        1 for m in messages
        for a in m.get("attachments", [])
        if a.get("mimeType", "").startswith("video/")
    )
    has_media = image_count > 0 or video_count > 0
    has_failed = any(
        a.get("downloadFailed") for m in messages for a in m.get("attachments", [])
    )

    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "id": conv_id,
        "title": meta.get("title", ""),
        "lastMessage": (meta.get("title", "") or "")[:100],
        "messageCount": msg_count,
        "hasMedia": has_media,
        "updatedAt": meta.get("updatedAt", now),
        "syncedAt": now,
        "remoteHash": meta.get("remoteHash", ""),
        "imageCount": image_count,
        "videoCount": video_count,
    }
    if has_failed:
        entry["hasFailedData"] = True

    # 替换或追加
    items = data["items"]
    replaced = False
    for i, item in enumerate(items):
        if item["id"] == conv_id:
            items[i] = entry
            replaced = True
            break
    if not replaced:
        items.append(entry)

    data["updatedAt"] = now
    data["totalCount"] = len(items)

    with open(conv_json_path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=None)


def main():
    parser = argparse.ArgumentParser(description="迁移单个对话到 app 数据目录")
    parser.add_argument("source_dir", help="源导出目录路径")
    parser.add_argument("conversation_id", help="对话 ID (16位hex, 可带 c_ 前缀)")
    parser.add_argument("--account", help="目标账号 (authuser编号/account_id/email), 不指定则自动匹配")
    args = parser.parse_args()

    src_dir = Path(args.source_dir).expanduser().resolve()
    conv_id = args.conversation_id
    if conv_id.startswith("c_"):
        conv_id = conv_id[2:]

    if not src_dir.is_dir():
        print(f"[错误] 源目录不存在: {src_dir}")
        sys.exit(1)

    # 定位源文件
    src_jsonl = find_source_jsonl(src_dir, conv_id)
    print(f"[源] {src_jsonl}")

    # 解析源数据
    turns = parse_source_turns(src_jsonl)
    print(f"[源] {len(turns)} turns")

    # 查找元数据
    metadata = load_source_metadata(src_dir, conv_id)
    if metadata:
        print(f"[源] 标题: {metadata.get('title', '(无)')[:60]}")

    # 解析目标账号
    account = resolve_account(args.account, conv_id)
    account_id = account["id"]
    target_dir = APP_DATA / account["dataDir"]
    print(f"[目标] {account_id} (authuser={account['authuser']})")

    # 转换格式
    lines, media_ids = convert_to_app_format(turns, conv_id, account_id, metadata)
    print(f"[转换] {len(lines)-1} messages, {len(media_ids)} media refs")

    # 确保目标目录存在
    (target_dir / "conversations").mkdir(parents=True, exist_ok=True)
    (target_dir / "media").mkdir(parents=True, exist_ok=True)

    # 写入 JSONL
    target_jsonl = target_dir / "conversations" / f"{conv_id}.jsonl"
    with open(target_jsonl, "w") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"[写入] {target_jsonl}")

    # 复制媒体文件
    src_media_dir = src_dir / "media"
    tgt_media_dir = target_dir / "media"
    copied = 0
    skipped = 0
    for mid in media_ids:
        src_file = src_media_dir / mid
        if src_file.exists():
            shutil.copy2(src_file, tgt_media_dir / mid)
            copied += 1
        else:
            skipped += 1
    print(f"[媒体] 复制 {copied}, 缺失 {skipped}")

    # 更新 conversations.json
    conv_json = target_dir / "conversations.json"
    update_conversations_json(conv_json, conv_id, account_id, lines)
    print(f"[索引] 已更新 {conv_json}")

    print(f"\n迁移完成: {conv_id} -> {account_id}")


if __name__ == "__main__":
    main()
