#!/usr/bin/env python3
"""
gemini_to_kelivo.py — Gemini Collector → Kelivo 备份 ZIP 转换脚本

用法:
    python3 gemini_to_kelivo.py [账号ID] [输出路径] [--max-json SIZE] [--max-upload SIZE]

    账号ID      可选，默认转换第一个账号
    输出路径    可选，默认输出到 ~/Downloads/kelivo_backup_<账号>_<时间戳>.zip
    --max-json   每包 chats.json 大小上限（如 5MB、500KB）
    --max-upload 每包 upload/ 附件总大小上限（如 10MB）
    分包时在文件名前加字母前缀：a_xxx.zip、b_xxx.zip …

示例:
    python3 gemini_to_kelivo.py cynaustraline_gmail_com ~/Downloads/kelivo.zip
    python3 gemini_to_kelivo.py cynaustraline_gmail_com --max-json 5MB --max-upload 10MB
"""

import argparse
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# ── App 数据目录 ──────────────────────────────────────────────────────────────
APP_DATA = Path.home() / "Library" / "Application Support" / "com.gemini-collector.app"


# ── 工具函数 ──────────────────────────────────────────────────────────────────
def parse_size(s: str) -> int:
    """将 '5MB'、'500KB'、'1GB' 等解析为字节数。"""
    s = s.strip().upper()
    for suffix, mult in [("GB", 1024**3), ("MB", 1024**2), ("KB", 1024), ("B", 1)]:
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(s)


def idx_to_label(n: int) -> str:
    """0→'a', 1→'b', …, 25→'z', 26→'aa', …"""
    label = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        label = chr(97 + r) + label
    return label


# ── 媒体 MIME 类型 → Kelivo 标记类型 ─────────────────────────────────────────
def is_image_mime(mime: str) -> bool:
    return mime.startswith("image/")


def build_content(text: str, attachments: list) -> str:
    """把 text + attachments 合并成 Kelivo content 字符串。"""
    parts = [text] if text else [""]
    for att in attachments:
        media_id = att["mediaId"]
        mime = att.get("mimeType", "application/octet-stream")
        if is_image_mime(mime):
            parts.append(f"[image:/upload/{media_id}]")
        else:
            parts.append(f"[file:/upload/{media_id}|{media_id}|{mime}]")
    return "\n".join(parts)


# ── JSONL 解析 ────────────────────────────────────────────────────────────────
def parse_jsonl(path: Path):
    """解析一个 .jsonl 文件，返回 (meta_dict, [message_dict, ...])。"""
    meta = None
    messages = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("type") == "meta":
                meta = obj
            elif obj.get("type") == "message":
                messages.append(obj)
    return meta, messages


# ── 转换单条消息 ───────────────────────────────────────────────────────────────
def convert_message(msg: dict, conv_id: str) -> dict:
    role_map = {"user": "user", "model": "assistant"}
    role = role_map.get(msg.get("role", "user"), "user")
    attachments = msg.get("attachments") or []
    content = build_content(msg.get("text", ""), attachments)

    return {
        "id": msg["id"],
        "role": role,
        "content": content,
        "timestamp": msg.get("timestamp"),
        "modelId": msg.get("model"),
        "providerId": "google",
        "totalTokens": None,
        "conversationId": conv_id,
        "isStreaming": False,
        "reasoningText": msg.get("thinking"),
        "reasoningStartAt": None,
        "reasoningFinishedAt": None,
        "translation": None,
        "reasoningSegmentsJson": None,
        "groupId": None,
        "version": 0,
    }


# ── 转换单个对话 ───────────────────────────────────────────────────────────────
def convert_conversation(meta: dict, messages: list):
    """返回 (kelivo_conv_dict, [kelivo_msg_dict, ...], {media_id, ...})"""
    conv_id = meta["id"]
    kelivo_msgs = [convert_message(m, conv_id) for m in messages]
    message_ids = [m["id"] for m in messages]

    media_ids: set = set()
    for m in messages:
        for att in m.get("attachments") or []:
            if att.get("mediaId"):
                media_ids.add(att["mediaId"])

    kelivo_conv = {
        "id": conv_id,
        "title": meta.get("title", ""),
        "createdAt": meta.get("createdAt"),
        "updatedAt": meta.get("updatedAt"),
        "messageIds": message_ids,
        "isPinned": False,
        "mcpServerIds": [],
        "assistantId": None,
        "truncateIndex": -1,
        "versionSelections": {},
        "summary": None,
        "lastSummarizedMessageCount": 0,
    }
    return kelivo_conv, kelivo_msgs, media_ids


# ── 分包算法（FFD 贪心） ──────────────────────────────────────────────────────
def pack_bins(items: list, json_limit, media_limit) -> list:
    """
    items 格式：
      (conv_id, kelivo_conv, kelivo_msgs, media_ids, json_bytes, media_bytes)

    策略：First Fit Decreasing（归一化权重降序排列，贪心放入第一个能容纳的 bin）。
    单对话若自身超过任一限制，独立成包（不报错）。
    """

    def norm_weight(item):
        jb, mb = item[4], item[5]
        jn = jb / json_limit  if json_limit  else 0
        mn = mb / media_limit if media_limit else 0
        return max(jn, mn)

    sorted_items = sorted(items, key=norm_weight, reverse=True)
    bins: list = []  # [{"json": int, "media": int, "items": list}]

    for item in sorted_items:
        jb, mb = item[4], item[5]
        exceeds = (json_limit and jb > json_limit) or (media_limit and mb > media_limit)

        if exceeds:
            bins.append({"json": jb, "media": mb, "items": [item]})
            continue

        placed = False
        for b in bins:
            json_ok  = (not json_limit)  or (b["json"]  + jb <= json_limit)
            media_ok = (not media_limit) or (b["media"] + mb <= media_limit)
            if json_ok and media_ok:
                b["items"].append(item)
                b["json"]  += jb
                b["media"] += mb
                placed = True
                break

        if not placed:
            bins.append({"json": jb, "media": mb, "items": [item]})

    return [b["items"] for b in bins]


# ── 写单个 ZIP ────────────────────────────────────────────────────────────────
def write_zip(zip_path: Path, bin_items: list, media_dir: Path, label: str = ""):
    all_convs = [it[1] for it in bin_items]
    all_msgs  = [m for it in bin_items for m in it[2]]
    all_mids  = {mid for it in bin_items for mid in it[3]}

    chats_obj = {
        "version": 1,
        "conversations": all_convs,
        "messages": all_msgs,
        "toolEvents": {},
        "geminiThoughtSigs": {},
    }
    chats_json = json.dumps(chats_obj, ensure_ascii=False, separators=(",", ":"))

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    media_found = media_missing = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("chats.json", chats_json)
        for mid in sorted(all_mids):
            src = media_dir / mid
            if src.exists():
                zf.write(src, f"upload/{mid}")
                media_found += 1
            else:
                media_missing += 1

    size_mb = zip_path.stat().st_size / 1024 / 1024
    prefix  = f"[{label}] " if label else ""
    print(
        f"  {prefix}{zip_path.name}  "
        f"{len(all_convs)} 对话  {len(all_msgs)} 消息  "
        f"媒体 {media_found}✓/{media_missing}✗  {size_mb:.1f}MB"
    )


# ── 主转换函数 ─────────────────────────────────────────────────────────────────
def convert_account(account_id: str, output_path: Path,
                    json_limit=None, media_limit=None, exclude: set = None):
    accounts_file = APP_DATA / "accounts.json"
    if not accounts_file.exists():
        print(f"[错误] 找不到 accounts.json: {accounts_file}", file=sys.stderr)
        sys.exit(1)

    registry = json.loads(accounts_file.read_text(encoding="utf-8"))
    accounts = {a["id"]: a for a in registry.get("accounts", [])}

    if account_id not in accounts:
        print(f"[错误] 账号 '{account_id}' 不存在", file=sys.stderr)
        print(f"可用账号: {list(accounts.keys())}", file=sys.stderr)
        sys.exit(1)

    acct_info = accounts[account_id]
    data_dir  = APP_DATA / acct_info["dataDir"]
    conv_dir  = data_dir / "conversations"
    media_dir = data_dir / "media"

    print(f"[信息] 账号: {acct_info['email']}")
    jsonl_files = sorted(conv_dir.glob("*.jsonl"))
    if exclude:
        jsonl_files = [p for p in jsonl_files if p.stem not in exclude]
        print(f"[信息] 排除 {len(exclude)} 个对话，剩余文件数: {len(jsonl_files)}")
    else:
        print(f"[信息] 对话文件数: {len(jsonl_files)}")

    _SEP    = (",", ":")   # compact JSON separators，与最终写入保持一致
    items   = []           # (conv_id, kelivo_conv, kelivo_msgs, media_ids, json_bytes, media_bytes)
    skipped = 0

    for i, jsonl_path in enumerate(jsonl_files, 1):
        try:
            meta, messages = parse_jsonl(jsonl_path)
        except Exception as e:
            print(f"  [跳过] {jsonl_path.name}: 解析失败 ({e})")
            skipped += 1
            continue

        if meta is None:
            print(f"  [跳过] {jsonl_path.name}: 缺少 meta 行")
            skipped += 1
            continue

        conv, msgs, media_ids = convert_conversation(meta, messages)

        # 估算该对话在 chats.json 中的贡献字节数
        json_bytes = len(
            json.dumps(conv, ensure_ascii=False, separators=_SEP).encode()
        ) + sum(
            len(json.dumps(m, ensure_ascii=False, separators=_SEP).encode())
            for m in msgs
        )
        # 该对话引用的媒体文件磁盘大小
        media_bytes = sum(
            (media_dir / mid).stat().st_size
            for mid in media_ids
            if (media_dir / mid).exists()
        )

        items.append((conv["id"], conv, msgs, media_ids, json_bytes, media_bytes))

        if i % 50 == 0:
            print(f"  已处理 {i}/{len(jsonl_files)} 对话...")

    total_convs = len(items)
    total_msgs  = sum(len(it[2]) for it in items)
    total_mids  = len({mid for it in items for mid in it[3]})
    print(f"[信息] 成功转换: {total_convs} 对话，{total_msgs} 条消息，跳过 {skipped} 个")
    print(f"[信息] 引用媒体文件数: {total_mids}")

    # ── 分包 ────────────────────────────────────────────────────────────────
    if json_limit is None and media_limit is None:
        bins = [items]
    else:
        lj = f"{json_limit  / 1024 / 1024:.0f}MB" if json_limit  else "∞"
        lu = f"{media_limit / 1024 / 1024:.0f}MB" if media_limit else "∞"
        print(f"[信息] 分包限制：chats.json ≤ {lj}，upload ≤ {lu}")
        bins = pack_bins(items, json_limit, media_limit)
        print(f"[信息] 分包数量: {len(bins)}")

    # ── 写 ZIP ──────────────────────────────────────────────────────────────
    multi  = len(bins) > 1
    stem   = output_path.stem    # e.g. "kelivo_backup_cynaustraline_20260228T210549"
    suffix = output_path.suffix  # ".zip"

    for idx, bin_items in enumerate(bins):
        if multi:
            label    = idx_to_label(idx)
            zip_path = output_path.parent / f"{label}_{stem}{suffix}"
        else:
            label    = ""
            zip_path = output_path
        write_zip(zip_path, bin_items, media_dir, label)

    if multi:
        print(f"[完成] 共 {len(bins)} 个包，输出到 {output_path.parent}")
    else:
        print(f"[完成] 输出: {output_path}  ({output_path.stat().st_size / 1024 / 1024:.1f} MB)")


# ── 入口 ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Gemini Collector → Kelivo 备份 ZIP 转换脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("account_id", nargs="?", help="账号 ID（默认第一个）")
    parser.add_argument("output",     nargs="?", help="输出 ZIP 路径")
    parser.add_argument("--max-json",   metavar="SIZE", help="每包 chats.json 大小上限，如 5MB")
    parser.add_argument("--max-upload", metavar="SIZE", help="每包 upload/ 附件总大小上限，如 10MB")
    parser.add_argument("--exclude", metavar="ID", nargs="+", help="排除指定对话 ID（可多个）")
    args = parser.parse_args()

    if args.account_id:
        account_id = args.account_id
    else:
        registry   = json.loads((APP_DATA / "accounts.json").read_text(encoding="utf-8"))
        account_id = registry["accounts"][0]["id"]
        print(f"[信息] 未指定账号，使用默认: {account_id}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    output_path = (
        Path(args.output) if args.output
        else Path.home() / "Downloads" / f"kelivo_backup_{account_id}_{ts}.zip"
    )

    json_limit  = parse_size(args.max_json)   if args.max_json   else None
    media_limit = parse_size(args.max_upload) if args.max_upload else None
    exclude     = set(args.exclude) if args.exclude else None

    convert_account(account_id, output_path, json_limit, media_limit, exclude)


if __name__ == "__main__":
    main()
