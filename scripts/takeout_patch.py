#!/usr/bin/env python3
"""
takeout_patch.py  —  将 Google Takeout 活动记录作为新 turn 插入 app 对话

每条 Takeout entry = app 里缺失的一对 (user + model) turn，本脚本将其插入到
指定对话的正确时间位置，不修改任何现有 turn。

用法：
  # 预览（不写文件）
  python3 scripts/takeout_patch.py \
      --account cynaustraline_gmail_com \
      --conv-id f2c4dabd8666bd39 \
      --takeout-ids 758cf151a931a75f

  # 执行插入
  python3 scripts/takeout_patch.py ... --apply

  # 自测（临时目录，不触碰真实数据）
  python3 scripts/takeout_patch.py --test
"""

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── 自动安装依赖 ───────────────────────────────────────────────────────────────

try:
    import markdownify as _md_lib
except ImportError:
    print("缺少 markdownify，正在安装...")
    os.system(f"{sys.executable} -m pip install markdownify -q")
    import markdownify as _md_lib

# ── 常量 ──────────────────────────────────────────────────────────────────────

APP_DATA_DEFAULT = (
    Path.home() / "Library" / "Application Support" / "com.gemini-collector.app"
)
TAKEOUT_DEFAULT = (
    Path.home()
    / "Downloads"
    / "Takeout 2"
    / "我的活动"
    / "Gemini Apps"
    / "我的活动记录_with_id.json"
)

_EXT_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".pdf": "application/pdf",
}

PROMPTED_PREFIX = "Prompted "

# ── 工具函数 ──────────────────────────────────────────────────────────────────


def load_jsonl(path: Path) -> list:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def write_jsonl_atomic(path: Path, records: list):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.rename(path)


def write_json_atomic(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024**2:
        return f"{n / 1024:.0f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


# ── HTML → Markdown ───────────────────────────────────────────────────────────


def html_to_md(html: str) -> str:
    """将 Gemini safeHtmlItem HTML 转换为 Markdown（img 输出后再删除）。"""
    import re

    result = _md_lib.markdownify(
        html,
        heading_style="ATX",
        bullets="*",
    )
    # 删除 markdownify 转换出的图片 markdown（AI 生成图暂不处理）
    result = re.sub(r"!\[.*?\]\(.*?\)", "", result)
    # 去除 markdownify 对 * _ 的多余转义（code block 内不受影响）
    result = re.sub(r"\\([*_])", r"\1", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# ── Takeout 解析 ──────────────────────────────────────────────────────────────


def load_takeout(path: Path) -> dict:
    """返回 {id: entry} 映射。"""
    entries = json.loads(path.read_text(encoding="utf-8"))
    return {e["id"]: e for e in entries}


def extract_user_text(title: str) -> str:
    """从 Takeout title 提取用户文本（去掉 'Prompted ' 前缀）。"""
    if title.startswith(PROMPTED_PREFIX):
        return title[len(PROMPTED_PREFIX):]
    return title


def parse_subtitles_attachments(subtitles: list) -> list:
    """
    从 subtitles 提取用户附件（有 url 且非远程链接）。
    返回 [{"url": str, "name": str}, ...]
    """
    result = []
    for s in subtitles:
        url = s.get("url", "")
        name = s.get("name", "")
        if url and not url.startswith("http"):
            result.append({"url": url, "name": name})
    return result


# ── 媒体文件处理 ──────────────────────────────────────────────────────────────


def find_actual_file(takeout_media_dir: Path, declared_url: str) -> Path | None:
    """查找实际文件（忽略声明后缀，按 stem 模糊匹配）。"""
    exact = takeout_media_dir / declared_url
    if exact.exists():
        return exact
    base = declared_url.rsplit(".", 1)[0]
    for f in takeout_media_dir.iterdir():
        stem = f.name.rsplit(".", 1)[0] if "." in f.name else f.name
        if stem == base:
            return f
    return None


def build_media_action(
    declared_url: str,
    name: str,
    takeout_media_dir: Path,
    app_media_dir: Path,
    placeholder_prefix: str,
    placeholder_index: int,
) -> dict:
    """
    为单个附件构建处理计划。
    文件存在：MD5+实际后缀 作为 mediaId。
    文件缺失：生成占位 mediaId（missing_前缀），不复制文件。
    """
    actual = find_actual_file(takeout_media_dir, declared_url)

    if actual is None:
        declared_ext = (
            "." + declared_url.rsplit(".", 1)[-1] if "." in declared_url else ""
        )
        media_id = f"missing_{placeholder_prefix}_{placeholder_index}{declared_ext}"
        mime = _EXT_MIME.get(declared_ext.lower(), "application/octet-stream")
        return {
            "declared_url": declared_url,
            "name": name,
            "actual_src": None,
            "media_id": media_id,
            "mime": mime,
            "is_placeholder": True,
            "already_exists": False,
            "size": 0,
        }

    md5 = file_md5(actual)
    ext = actual.suffix
    media_id = md5 + ext
    mime = detect_mime(actual)
    already_exists = (app_media_dir / media_id).exists()

    return {
        "declared_url": declared_url,
        "name": name,
        "actual_src": actual,
        "media_id": media_id,
        "mime": mime,
        "is_placeholder": False,
        "already_exists": already_exists,
        "size": actual.stat().st_size if not already_exists else 0,
    }


# ── Turn ID ───────────────────────────────────────────────────────────────────


def make_turn_ids(takeout_id: str) -> tuple[str, str]:
    """生成新 turn 的 user/model ID，格式与 app 原生 turn 一致。"""
    base = f"r_{takeout_id}"
    return f"{base}_u", f"{base}_m"


# ── 构建计划 ──────────────────────────────────────────────────────────────────


def build_plan(
    account_id: str,
    conv_id: str,
    takeout_ids: list,
    takeout_path: Path,
    app_data: Path,
) -> dict:
    account_dir = app_data / "accounts" / account_id
    conv_dir = account_dir / "conversations"
    media_dir = account_dir / "media"
    takeout_media_dir = takeout_path.parent

    takeout_map = load_takeout(takeout_path)

    jsonl_path = conv_dir / f"{conv_id}.jsonl"
    if not jsonl_path.exists():
        raise FileNotFoundError(f"对话 JSONL 不存在: {jsonl_path}")
    records = load_jsonl(jsonl_path)
    conv_meta = records[0]
    existing_msgs = records[1:]

    # 已有 turn ID 集合（JSONL 已有 + 本次 plan 内已计划），用于去重
    existing_ids = {m.get("id") for m in existing_msgs}
    planned_user_ids: set[str] = set()  # 本次 build 中已计划插入的 user ID

    # 对话标题
    index_path = account_dir / "conversations.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index_map = {item["id"]: item for item in index["items"]}
    conv_title = index_map.get(conv_id, {}).get("title", conv_id)

    entries = []
    for tid in takeout_ids:
        entry_data = takeout_map.get(tid)
        if entry_data is None:
            entries.append({"takeout_id": tid, "error": "Takeout 中未找到该 ID"})
            continue

        try:
            ts = parse_ts(entry_data["time"])
        except Exception as e:
            entries.append({"takeout_id": tid, "error": f"时间解析失败: {e}"})
            continue

        user_id, model_id = make_turn_ids(tid)

        # 去重：已在 JSONL 中，或本次 plan 内已计划过
        if user_id in existing_ids or user_id in planned_user_ids:
            entries.append(
                {
                    "takeout_id": tid,
                    "error": None,
                    "skip": True,
                    "skip_reason": "turn 已存在（已插入过）",
                    "time": entry_data["time"],
                    "user_id": user_id,
                    "model_id": model_id,
                }
            )
            continue

        # 用户文本
        user_text = extract_user_text(entry_data.get("title", ""))

        # model 文本
        safe_html = entry_data.get("safeHtmlItem")
        model_text = ""
        model_text_chars = 0
        if safe_html:
            try:
                model_text = html_to_md(safe_html[0]["html"])
                model_text_chars = len(model_text)
            except Exception:
                model_text_chars = -1  # 转换失败

        # 媒体附件
        subtitles = entry_data.get("subtitles", [])
        attach_urls = parse_subtitles_attachments(subtitles)
        media_actions = []
        for idx, att in enumerate(attach_urls):
            action = build_media_action(
                declared_url=att["url"],
                name=att["name"],
                takeout_media_dir=takeout_media_dir,
                app_media_dir=media_dir,
                placeholder_prefix=tid,
                placeholder_index=idx,
            )
            media_actions.append(action)

        # 计算插入位置（在已有消息中按时间排序后的位置）
        insert_pos = sum(
            1
            for m in existing_msgs
            if m.get("timestamp", "") <= entry_data["time"]
        )

        planned_user_ids.add(user_id)
        entries.append(
            {
                "takeout_id": tid,
                "error": None,
                "skip": False,
                "time": entry_data["time"],
                "user_id": user_id,
                "model_id": model_id,
                "user_text": user_text,
                "model_text": model_text,
                "model_text_chars": model_text_chars,
                "media_actions": media_actions,
                "insert_pos": insert_pos,
                "insert_pos_desc": _pos_desc(insert_pos, len(existing_msgs)),
            }
        )

    # 计算 createdAt 是否需要更新
    current_created = conv_meta.get("createdAt", "")
    new_created = current_created
    try:
        current_dt = parse_ts(current_created) if current_created else None
    except Exception:
        current_dt = None

    for e in entries:
        if e.get("error") or e.get("skip") or not e.get("time"):
            continue
        try:
            e_dt = parse_ts(e["time"])
            if current_dt is None or e_dt < current_dt:
                current_dt = e_dt
                new_created = e["time"]
        except Exception:
            pass

    return {
        "account_id": account_id,
        "conv_id": conv_id,
        "conv_title": conv_title,
        "account_dir": str(account_dir),
        "media_dir": str(media_dir),
        "takeout_path": str(takeout_path),
        "jsonl_path": str(jsonl_path),
        "existing_msg_count": len(existing_msgs),
        "entries": entries,
        "conv_meta": conv_meta,
        "current_created": current_created,
        "new_created": new_created,
        "created_will_update": new_created != current_created,
    }


def _pos_desc(pos: int, total: int) -> str:
    if pos == 0:
        return "头部（最早）"
    if pos >= total:
        return "尾部（最晚）"
    return f"第{pos}条之后"


# ── 打印报告 ──────────────────────────────────────────────────────────────────


def print_report(plan: dict):
    entries = plan["entries"]
    active = [e for e in entries if not e.get("error") and not e.get("skip")]
    skipped = [e for e in entries if e.get("skip")]
    errored = [e for e in entries if e.get("error")]

    all_media = [a for e in active for a in e.get("media_actions", [])]
    new_files = [a for a in all_media if not a["already_exists"] and not a["is_placeholder"]]
    placeholders = [a for a in all_media if a["is_placeholder"]]

    print("=" * 64)
    print("Takeout 补全预报告")
    print("=" * 64)
    print(f"账号：{plan['account_id']}")
    print(f"对话：{plan['conv_id']}  《{plan['conv_title'][:40]}》")
    print(f"现有消息：{plan['existing_msg_count']} 条")
    print(f"Takeout：{Path(plan['takeout_path']).name}")
    print()
    print(
        f"待插入：{len(active)} 条  跳过（已存在）：{len(skipped)} 条  "
        f"失败：{len(errored)} 条"
    )
    print()

    if active:
        col = "%-2s  %-16s  %-19s  %-16s  %-12s  %-14s  %s"
        print(col % ("#", "Takeout ID", "时间", "插入位置", "User附件", "Model文字", "Turn ID"))
        print("─" * 110)
        for i, e in enumerate(active, 1):
            ts_str = e["time"][:19].replace("T", " ")

            ma = e.get("media_actions", [])
            if not ma:
                attach_s = "无"
            else:
                ok_cnt = sum(1 for a in ma if not a["is_placeholder"])
                ph_cnt = sum(1 for a in ma if a["is_placeholder"])
                parts = []
                if ok_cnt:
                    parts.append(f"{ok_cnt}✓")
                if ph_cnt:
                    parts.append(f"{ph_cnt}✗")
                attach_s = f"{len(ma)}个(" + "/".join(parts) + ")"

            if e["model_text_chars"] < 0:
                md_s = "转换失败"
            elif e["model_text_chars"] == 0:
                md_s = "无safeHtml"
            else:
                md_s = f"写入({e['model_text_chars']}字)"

            print(
                col % (
                    i,
                    e["takeout_id"],
                    ts_str,
                    e["insert_pos_desc"],
                    attach_s,
                    md_s,
                    e["user_id"],
                )
            )
        print()

    # 媒体文件详情
    if all_media:
        print(f"媒体文件（共 {len(all_media)} 个）：")
        for a in all_media:
            url_s = a["declared_url"][:44]
            mid_s = a["media_id"][:20] + "..."
            if a["is_placeholder"]:
                print(f"  ✗  {url_s:<44} → 占位: {a['media_id']}")
            elif a["already_exists"]:
                print(f"  ✓  {url_s:<44} → {mid_s}  (已在media目录)")
            else:
                decl_ext = a["declared_url"].rsplit(".", 1)[-1] if "." in a["declared_url"] else ""
                act_ext = a["actual_src"].suffix.lstrip(".") if a["actual_src"] else ""
                ext_note = f"  ext修正:{decl_ext}→{act_ext}" if decl_ext.lower() != act_ext.lower() else ""
                print(f"  ✓  {url_s:<44} → {mid_s}  ({fmt_size(a['size'])}{ext_note})")
        if new_files:
            print(f"  合计新增：{len(new_files)} 个，约 {fmt_size(sum(a['size'] for a in new_files))}")
        if placeholders:
            print(f"  占位文件：{len(placeholders)} 个（原始文件缺失，app将显示媒体缺失）")
        print()

    # 时间更新
    if plan["created_will_update"]:
        print("时间更新：")
        print(f"  createdAt: {plan['current_created']}")
        print(f"          →  {plan['new_created']}  (← 更早，将更新)")
        print()

    if skipped:
        print(f"跳过（{len(skipped)} 条，turn 已存在）：")
        for e in skipped:
            print(f"  {e['takeout_id']}  {e['user_id']}")
        print()

    if errored:
        print(f"失败（{len(errored)} 条）：")
        for e in errored:
            print(f"  {e['takeout_id']}  {e['error']}")
        print()

    if active:
        print("执行插入：添加 --apply 参数")
    else:
        print("无需执行。")
    print("=" * 64)


# ── 执行插入 ──────────────────────────────────────────────────────────────────


def apply_plan(plan: dict):
    account_dir = Path(plan["account_dir"])
    media_dir = Path(plan["media_dir"])
    jsonl_path = Path(plan["jsonl_path"])
    index_path = account_dir / "conversations.json"

    records = load_jsonl(jsonl_path)
    conv_meta = records[0]
    msgs = list(records[1:])

    active = [e for e in plan["entries"] if not e.get("error") and not e.get("skip")]

    # 按时间从早到晚处理，保证插入位置递增，每次从上次插入点之后继续搜索
    active_sorted = sorted(active, key=lambda e: e["time"])
    search_from = 0  # 下一次搜索的起始索引（利用有序性避免从头扫）

    for e in active_sorted:
        # 构建 user turn
        attachments = []
        for action in e.get("media_actions", []):
            if not action["is_placeholder"] and action["actual_src"] and not action["already_exists"]:
                dst = media_dir / action["media_id"]
                if not dst.exists():
                    shutil.copy2(action["actual_src"], dst)
            attachments.append(
                {"mediaId": action["media_id"], "mimeType": action["mime"]}
            )

        ts = e["time"].replace("Z", "+00:00")
        user_turn = {
            "type": "message",
            "id": e["user_id"],
            "role": "user",
            "text": e["user_text"],
            "attachments": attachments,
            "timestamp": ts,
            "source": "takeout_patch",
        }
        model_turn = {
            "type": "message",
            "id": e["model_id"],
            "role": "model",
            "text": e["model_text"],
            "attachments": [],
            "timestamp": ts,
            "source": "takeout_patch",
        }

        # 从 search_from 向后线性扫描，找到第一条时间戳严格晚于当前 ts 的位置
        insert_pos = len(msgs)
        for i in range(search_from, len(msgs)):
            if msgs[i].get("timestamp", "") > ts:
                insert_pos = i
                break

        msgs[insert_pos:insert_pos] = [user_turn, model_turn]
        search_from = insert_pos + 2  # 跳过刚插入的两条
        print(f"  ✓ 插入  {e['takeout_id']}  pos={insert_pos}  {e['user_id']}")

    # 更新 createdAt
    if plan["created_will_update"]:
        conv_meta["createdAt"] = plan["new_created"]

    # 原子写回
    write_jsonl_atomic(jsonl_path, [conv_meta] + msgs)

    # 更新 conversations.json
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index_map = {item["id"]: item for item in index["items"]}
    conv_id = plan["conv_id"]
    if conv_id in index_map:
        index_map[conv_id]["hasMedia"] = any(m.get("attachments") for m in msgs)
        index_map[conv_id]["messageCount"] = len(msgs)
    index["updatedAt"] = now_iso()
    write_json_atomic(index_path, index)

    print(f"\n插入完成：{len(active)} 条 turn 已写入，对话现有 {len(msgs)} 条消息")


# ── 自测 ──────────────────────────────────────────────────────────────────────


def _run_tests():
    import tempfile
    import traceback

    print("=" * 56)
    print("运行自测（使用临时目录）")
    print("=" * 56)

    passed = 0
    failed = 0

    def check(name: str, cond: bool, detail: str = ""):
        nonlocal passed, failed
        if cond:
            print(f"  ✓ {name}")
            passed += 1
        else:
            print(f"  ✗ {name}" + (f"  ({detail})" if detail else ""))
            failed += 1

    def make_msg(base_id, role, ts, text="", attachments=None):
        suffix = "_u" if role == "user" else "_m"
        return {
            "type": "message",
            "id": f"{base_id}{suffix}",
            "role": role,
            "text": text,
            "attachments": attachments or [],
            "timestamp": ts,
        }

    def make_conv_meta(conv_id, account_id, title, created):
        return {
            "type": "meta",
            "id": conv_id,
            "accountId": account_id,
            "title": title,
            "createdAt": created,
            "updatedAt": created,
            "syncedAt": created,
            "remoteHash": None,
        }

    def make_takeout_entry(entry_id, time, title, html="", subtitles=None):
        e = {
            "id": entry_id,
            "header": "Gemini Apps",
            "title": title,
            "time": time,
            "products": ["Gemini Apps"],
            "activityControls": ["Gemini Apps Activity"],
        }
        if html:
            e["safeHtmlItem"] = [{"html": html}]
        if subtitles:
            e["subtitles"] = subtitles
        return e

    ACCOUNT = "test_account"
    CONV_ID = "abcdef1234567890"

    # 对话已有两条 turn，时间为 11月
    TS_EXIST_1 = "2025-11-10T10:00:00+00:00"
    TS_EXIST_2 = "2025-11-20T10:00:00+00:00"

    # 待插入：一条比现有更早（10月）、一条在中间（11月15日）、一条更晚（12月）
    TS_NEW_EARLY  = "2025-10-01T08:00:00.000Z"  # 最早 → 触发 createdAt 更新
    TS_NEW_MIDDLE = "2025-11-15T12:00:00.000Z"  # 插入两条之间
    TS_NEW_LATE   = "2025-12-01T09:00:00.000Z"  # 最晚

    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)

            # ── app 目录 ───────────────────────────────────────────────────
            app_data = tmp / "app"
            acc_dir = app_data / "accounts" / ACCOUNT
            conv_dir = acc_dir / "conversations"
            media_dir = acc_dir / "media"
            conv_dir.mkdir(parents=True)
            media_dir.mkdir(parents=True)

            (acc_dir / "meta.json").write_text(
                json.dumps({
                    "version": 1, "id": ACCOUNT, "conversationCount": 1,
                    "name": "Test", "email": "t@t.com",
                    "avatarText": "T", "avatarColor": "#000",
                    "remoteConversationCount": 1,
                    "lastSyncAt": "2025-11-20T10:00:00+00:00",
                    "lastSyncResult": "success",
                }),
                encoding="utf-8",
            )
            (acc_dir / "conversations.json").write_text(
                json.dumps({
                    "version": 1, "accountId": ACCOUNT,
                    "updatedAt": "2025-11-20T10:00:00+00:00",
                    "totalCount": 1,
                    "items": [{
                        "id": CONV_ID, "title": "测试对话",
                        "lastMessage": "", "messageCount": 4, "hasMedia": False,
                        "updatedAt": TS_EXIST_2, "syncedAt": TS_EXIST_2, "remoteHash": None,
                    }],
                }),
                encoding="utf-8",
            )
            write_jsonl_atomic(
                conv_dir / f"{CONV_ID}.jsonl",
                [
                    make_conv_meta(CONV_ID, ACCOUNT, "测试对话", TS_EXIST_1),
                    make_msg("r_exist1", "user",  TS_EXIST_1, "已有消息1"),
                    make_msg("r_exist1", "model", TS_EXIST_1, "已有回复1"),
                    make_msg("r_exist2", "user",  TS_EXIST_2, "已有消息2"),
                    make_msg("r_exist2", "model", TS_EXIST_2, "已有回复2"),
                ],
            )

            # ── Takeout 目录 ────────────────────────────────────────────────
            takeout_dir = tmp / "takeout"
            takeout_dir.mkdir()

            # 媒体文件：声明 .png 实际存 .jpg
            (takeout_dir / "photo-001.jpg").write_bytes(b"FAKE_JPG_CONTENT")

            takeout_json = takeout_dir / "takeout.json"
            takeout_json.write_text(
                json.dumps([
                    # 场景A：比现有更早，含附件（ext 不匹配），触发 createdAt 更新
                    make_takeout_entry(
                        "id_early", TS_NEW_EARLY,
                        "Prompted 早期的用户消息",
                        html="<p>早期的 <strong>model回复</strong></p>",
                        subtitles=[
                            {"name": "Attached 1 file."},
                            {"name": "-  photo.png", "url": "photo-001.png"},
                        ],
                    ),
                    # 场景B：插入两条之间，附件文件缺失（占位）
                    make_takeout_entry(
                        "id_middle", TS_NEW_MIDDLE,
                        "Prompted 中间的用户消息",
                        html="<ul><li>列表A</li><li>列表B</li></ul>",
                        subtitles=[
                            {"name": "Attached 1 file."},
                            {"name": "-  missing.jpg", "url": "no-such-file.jpg"},
                        ],
                    ),
                    # 场景C：最晚，无附件，无 safeHtml
                    make_takeout_entry(
                        "id_late", TS_NEW_LATE,
                        "Prompted 最新的用户消息",
                    ),
                    # 场景D：takeout 中不存在的 ID（用于测试错误处理）
                    make_takeout_entry("id_ghost", "2025-01-01T00:00:00Z", "ghost"),
                ], ensure_ascii=False),
                encoding="utf-8",
            )

            # ── build_plan ─────────────────────────────────────────────────
            print("\n--- 计划验证 ---")
            # id_early 传两次：第二次应被识别为重复跳过
            plan = build_plan(
                account_id=ACCOUNT,
                conv_id=CONV_ID,
                takeout_ids=["id_early", "id_middle", "id_late", "id_early", "id_notexist"],
                takeout_path=takeout_json,
                app_data=app_data,
            )

            entries = plan["entries"]
            active  = [e for e in entries if not e.get("error") and not e.get("skip")]
            skipped = [e for e in entries if e.get("skip")]
            errored = [e for e in entries if e.get("error")]

            check("待插入 3 条", len(active) == 3, f"实际={len(active)}")
            check("跳过 1 条（重复）", len(skipped) == 1, f"实际={len(skipped)}")
            check("失败 1 条（ID不存在）", len(errored) == 1, f"实际={len(errored)}")

            e_early = next(e for e in active if e["takeout_id"] == "id_early")
            check("early: user_id=r_id_early_u", e_early["user_id"] == "r_id_early_u")
            check("early: 附件1个", len(e_early["media_actions"]) == 1)
            if e_early["media_actions"]:
                ma = e_early["media_actions"][0]
                check("early: 附件非占位", not ma["is_placeholder"])
                check("early: mediaId 以 .jpg 结尾", ma["media_id"].endswith(".jpg"), ma["media_id"])
            check("early: model text 含**", "**" in (e_early.get("model_text") or ""))
            check("early: 插入头部", e_early["insert_pos"] == 0)

            e_mid = next(e for e in active if e["takeout_id"] == "id_middle")
            check("middle: 附件1个（占位）", len(e_mid["media_actions"]) == 1)
            if e_mid["media_actions"]:
                check("middle: 是占位", e_mid["media_actions"][0]["is_placeholder"])
            check("middle: model text 含 * ", "* " in (e_mid.get("model_text") or ""))

            e_late = next(e for e in active if e["takeout_id"] == "id_late")
            check("late: 无附件", len(e_late.get("media_actions", [])) == 0)
            check("late: model text 为空", e_late.get("model_text") == "")

            check("createdAt 将更新", plan["created_will_update"])
            check("new_created == TS_NEW_EARLY", plan["new_created"] == TS_NEW_EARLY)

            # ── 打印报告 ──────────────────────────────────────────────────
            print()
            print_report(plan)

            # ── apply ──────────────────────────────────────────────────────
            print("\n--- 执行 apply ---")
            apply_plan(plan)

            # ── 写入验证 ──────────────────────────────────────────────────
            print("\n--- 写入验证 ---")
            result = load_jsonl(conv_dir / f"{CONV_ID}.jsonl")
            meta_out = result[0]
            msgs_out = result[1:]
            ids_out = [m["id"] for m in msgs_out]

            check("总消息数=10（原4+新6）", len(msgs_out) == 10, f"实际={len(msgs_out)}")

            # 顺序验证：early应在最前
            check("第1条=r_id_early_u", msgs_out[0]["id"] == "r_id_early_u", msgs_out[0]["id"])
            check("第2条=r_id_early_m", msgs_out[1]["id"] == "r_id_early_m", msgs_out[1]["id"])

            # exist_1 在 early 之后
            idx_exist1 = next(i for i, m in enumerate(msgs_out) if m["id"] == "r_exist1_u")
            check("r_exist1 在 early 之后", idx_exist1 > 1, f"idx={idx_exist1}")

            # middle 在 exist1 和 exist2 之间
            idx_mid_u = next(i for i, m in enumerate(msgs_out) if m["id"] == "r_id_middle_u")
            idx_exist2 = next(i for i, m in enumerate(msgs_out) if m["id"] == "r_exist2_u")
            check("middle 在 exist1 和 exist2 之间",
                  idx_exist1 < idx_mid_u < idx_exist2,
                  f"exist1={idx_exist1} mid={idx_mid_u} exist2={idx_exist2}")

            # late 在最后
            check("r_id_late_u 在列表中", "r_id_late_u" in ids_out)
            idx_late_m = next(i for i, m in enumerate(msgs_out) if m["id"] == "r_id_late_m")
            check("late_m 在最后两条之一", idx_late_m >= len(msgs_out) - 2, f"idx={idx_late_m}")

            # source 字段
            early_u = next(m for m in msgs_out if m["id"] == "r_id_early_u")
            check("fix turn 有 source=takeout_patch", early_u.get("source") == "takeout_patch")

            # 附件写入
            check("early user 有1个附件", len(early_u.get("attachments", [])) == 1)
            if early_u.get("attachments"):
                att = early_u["attachments"][0]
                check("early 附件 mediaId .jpg", att["mediaId"].endswith(".jpg"), att["mediaId"])
                check("early 附件文件已复制", (media_dir / att["mediaId"]).exists())

            middle_u = next(m for m in msgs_out if m["id"] == "r_id_middle_u")
            check("middle 占位附件写入", len(middle_u.get("attachments", [])) == 1)
            if middle_u.get("attachments"):
                att2 = middle_u["attachments"][0]
                check("middle 占位 mediaId 含 missing_", "missing_" in att2["mediaId"])
                check("middle 占位文件不存在（预期）", not (media_dir / att2["mediaId"]).exists())

            # createdAt 更新
            check("createdAt 已更新", meta_out["createdAt"] == TS_NEW_EARLY, meta_out["createdAt"])

            # 重复插入不产生多余记录（id_early 只有一份）
            early_u_count = sum(1 for m in msgs_out if m["id"] == "r_id_early_u")
            check("id_early 只插入一次", early_u_count == 1, f"实际={early_u_count}")

            # conversations.json 更新
            idx_data = json.loads((acc_dir / "conversations.json").read_text(encoding="utf-8"))
            item = next(x for x in idx_data["items"] if x["id"] == CONV_ID)
            check("messageCount=10", item["messageCount"] == 10, f"实际={item['messageCount']}")
            check("hasMedia=True", item["hasMedia"] is True)

            # 现有 turn 内容不变
            exist1_u = next(m for m in msgs_out if m["id"] == "r_exist1_u")
            check("现有 turn 内容不变", exist1_u["text"] == "已有消息1", exist1_u["text"])

    except Exception:
        traceback.print_exc()
        failed += 1

    print()
    print(f"结果：{passed} 通过  {failed} 失败")
    print("=" * 56)
    return failed == 0


# ── 主入口 ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="将 Takeout 活动记录作为新 turn 插入 app 对话")
    parser.add_argument("--account", help="账号 ID，如 cynaustraline_gmail_com")
    parser.add_argument("--conv-id", help="目标对话 ID（16位hex）")
    parser.add_argument("--takeout-ids", help="Takeout 条目 ID，逗号分隔")
    parser.add_argument(
        "--takeout-file",
        default=str(TAKEOUT_DEFAULT),
        help="Takeout JSON 文件路径（默认：Takeout 2 标准位置）",
    )
    parser.add_argument(
        "--app-data",
        default=str(APP_DATA_DEFAULT),
        help="app 数据根目录",
    )
    parser.add_argument("--apply", action="store_true", help="执行插入（默认只显示报告）")
    parser.add_argument("--test", action="store_true", help="运行自测")
    args = parser.parse_args()

    if args.test:
        ok = _run_tests()
        sys.exit(0 if ok else 1)

    if not args.account or not args.conv_id or not args.takeout_ids:
        parser.error("--account、--conv-id、--takeout-ids 均为必填项（或使用 --test）")

    takeout_path = Path(args.takeout_file).expanduser()
    if not takeout_path.exists():
        print(f"错误：Takeout 文件不存在: {takeout_path}", file=sys.stderr)
        sys.exit(1)

    app_data = Path(args.app_data).expanduser()
    if not (app_data / "accounts" / args.account).exists():
        print(f"错误：账号目录不存在", file=sys.stderr)
        sys.exit(1)

    takeout_ids = [t.strip() for t in args.takeout_ids.split(",") if t.strip()]

    plan = build_plan(
        account_id=args.account,
        conv_id=args.conv_id,
        takeout_ids=takeout_ids,
        takeout_path=takeout_path,
        app_data=app_data,
    )
    print_report(plan)

    if args.apply:
        active = [e for e in plan["entries"] if not e.get("error") and not e.get("skip")]
        if not active:
            print("没有需要插入的内容。")
            return
        print("\n开始执行插入...")
        apply_plan(plan)


if __name__ == "__main__":
    main()
