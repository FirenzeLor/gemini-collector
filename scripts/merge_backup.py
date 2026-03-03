#!/usr/bin/env python3
"""
merge_backup.py - 将外部备份数据合并到 app 账号数据（取并集，只补旧 turn）

用法：
  # 预览（不写文件）
  python3 scripts/merge_backup.py --account <id> --sources <dir1> [dir2 ...]

  # 执行合并
  python3 scripts/merge_backup.py --account <id> --sources <dir1> [dir2 ...] --apply

  # 自测
  python3 scripts/merge_backup.py --test

合并规则：
  · 源有 + app 索引有 + JSONL 存在  → turn_id 对齐，只前插更早的旧 turn
  · 源有 + app 索引有 + JSONL 缺失  → 完整恢复
  · 源有 + app 完全无（索引也无）   → 跳过（可能已被删除）
  · 源无                            → 不动
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

APP_DATA_DEFAULT = (
    Path.home() / "Library" / "Application Support" / "com.gemini-collector"
)

# ── 格式检测 ──────────────────────────────────────────────────────────────────


def detect_format(src: Path) -> str:
    if (src / "conversations").is_dir():
        return "app-native"
    if list(src.glob("*.jsonl")):
        return "raw-export"
    raise ValueError(f"无法识别源目录格式: {src}")


# ── 工具函数 ──────────────────────────────────────────────────────────────────


def base_turn_id(msg_id: str) -> str:
    """r_xxx_u / r_xxx_m  →  r_xxx"""
    if msg_id.endswith("_u") or msg_id.endswith("_m"):
        return msg_id[:-2]
    return msg_id


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


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024**2:
        return f"{n / 1024:.0f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── raw turn → app 消息 ───────────────────────────────────────────────────────


def raw_turn_to_msgs(turn: dict) -> list:
    tid = turn["turn_id"]
    ts = turn["timestamp_iso"]

    u_files = turn.get("user", {}).get("files", [])
    a_files = turn.get("assistant", {}).get("files", [])

    # raw export 会把 user 上传的文件同时写到 assistant.files（echo）
    # 过滤掉 assistant.files 中 media_id 与 user.files 重复的条目
    u_mids = {f["media_id"] for f in u_files if f.get("media_id")}
    a_files_own = [f for f in a_files if f.get("media_id") not in u_mids]

    def to_attachments(files):
        return [
            {"mediaId": f["media_id"], "mimeType": f["mime"]}
            for f in files
            if f.get("media_id")
        ]

    user_msg = {
        "type": "message",
        "id": f"{tid}_u",
        "role": "user",
        "text": turn.get("user", {}).get("text", ""),
        "attachments": to_attachments(u_files),
        "timestamp": ts,
    }
    asst = turn.get("assistant", {})
    model_msg = {
        "type": "message",
        "id": f"{tid}_m",
        "role": "model",
        "text": asst.get("text", ""),
        "attachments": to_attachments(a_files_own),
        "timestamp": ts,
    }
    if asst.get("model"):
        model_msg["model"] = asst["model"]
    if asst.get("thinking"):
        model_msg["thinking"] = asst["thinking"]
    return [user_msg, model_msg]


# ── 从源目录加载对话 ──────────────────────────────────────────────────────────


def load_source_conv(src: Path, fmt: str, conv_id: str):
    """返回 (messages_sorted_by_time, title) 或 None。"""
    if fmt == "app-native":
        path = src / "conversations" / f"{conv_id}.jsonl"
        if not path.exists():
            return None
        records = load_jsonl(path)
        meta = records[0]
        msgs = sorted(records[1:], key=lambda m: m.get("timestamp", ""))
        return msgs, meta.get("title", conv_id)
    else:  # raw-export
        path = src / f"{conv_id}.jsonl"
        if not path.exists():
            return None
        turns = sorted(load_jsonl(path), key=lambda t: t.get("timestamp", 0))
        msgs = []
        for turn in turns:
            msgs.extend(raw_turn_to_msgs(turn))
        # 取标题
        title = conv_id
        for list_name in ("chat_list_full.json", "chat_list_union.json", "chat_list.json"):
            lp = src / list_name
            if lp.exists():
                for item in json.loads(lp.read_text(encoding="utf-8")):
                    raw_id = item.get("id", "")
                    clean = raw_id[2:] if raw_id.startswith("c_") else raw_id
                    if clean == conv_id:
                        title = item.get("title", conv_id)
                        break
                break
        return msgs, title


# ── 并集合并工具 ──────────────────────────────────────────────────────────────

# kind 显示标签
KIND_LABELS = {
    "older":       "补旧",
    "newer":       "源超前",
    "interleaved": "交错",
    "no_overlap":  "无重叠",
}


def find_union_additions(src_msgs: list, app_msgs: list):
    """
    返回 (additions, kind, stats)。
    additions : src 中 app 没有的消息（按 turn_id 分组，保留全部 _u/_m）。
    kind      : "source_subset" | "no_overlap" | "older" | "newer" | "interleaved"
    stats     : {"common": int, "src_only": int, "app_only": int}
    """
    app_turn_ids = {base_turn_id(m["id"]) for m in app_msgs}

    src_turns_ordered: list = []
    seen: set = set()
    for m in src_msgs:
        tid = base_turn_id(m["id"])
        if tid not in seen:
            seen.add(tid)
            src_turns_ordered.append(tid)

    src_turn_ids = set(src_turns_ordered)
    common  = src_turn_ids & app_turn_ids
    src_only = src_turn_ids - app_turn_ids

    additions = [m for m in src_msgs if base_turn_id(m["id"]) in src_only]
    stats = {
        "common":   len(common),
        "src_only": len(src_only),
        "app_only": len(app_turn_ids - src_turn_ids),
    }

    if not src_only:
        return additions, "source_subset", stats
    if not common:
        return additions, "no_overlap", stats

    # src_only turn 相对于 common turn 的位置
    first_common_idx   = min(i for i, t in enumerate(src_turns_ordered) if t in common)
    last_common_idx    = max(i for i, t in enumerate(src_turns_ordered) if t in common)
    first_src_only_idx = min(i for i, t in enumerate(src_turns_ordered) if t in src_only)
    last_src_only_idx  = max(i for i, t in enumerate(src_turns_ordered) if t in src_only)

    if last_src_only_idx < first_common_idx:
        return additions, "older", stats
    if first_src_only_idx > last_common_idx:
        return additions, "newer", stats
    return additions, "interleaved", stats


def merge_by_timestamp(app_msgs: list, additions: list) -> list:
    """将 additions 按时间戳插入 app_msgs，以 turn 为单位排序。"""
    groups: dict = {}
    order: list = []
    for m in app_msgs + additions:
        tid = base_turn_id(m["id"])
        if tid not in groups:
            groups[tid] = []
            order.append(tid)
        groups[tid].append(m)

    def turn_ts(tid: str) -> str:
        return next(
            (m.get("timestamp", "") for m in groups[tid] if m.get("timestamp")), ""
        ) or ""

    sorted_tids = sorted(order, key=turn_ts)
    result = []
    for tid in sorted_tids:
        result.extend(groups[tid])
    return result


# ── 媒体文件 ──────────────────────────────────────────────────────────────────


def media_ids_of(msgs: list) -> set:
    return {a["mediaId"] for m in msgs for a in m.get("attachments", [])}


def count_media_by_type(msgs: list) -> tuple:
    """返回 (image_count, video_count)，按唯一 mediaId 统计。"""
    image_ids: set = set()
    video_ids: set = set()
    for m in msgs:
        for a in m.get("attachments", []):
            mid = a.get("mediaId")
            if not mid:
                continue
            mime = a.get("mimeType", "")
            if mime.startswith("image/"):
                image_ids.add(mid)
            elif mime.startswith("video/"):
                video_ids.add(mid)
    return len(image_ids), len(video_ids)


def new_media_list(media_ids: set, src: Path, app_media: Path) -> list:
    """返回 [{"id", "src", "size"}, ...] 仅包含 app 尚无的文件。"""
    src_media = src / "media"
    result = []
    for mid in sorted(media_ids):
        dst = app_media / mid
        sp = src_media / mid
        if not dst.exists() and sp.exists():
            result.append({"id": mid, "src": sp, "size": sp.stat().st_size})
    return result


# ── 构建合并计划 ──────────────────────────────────────────────────────────────


def build_plan(account_id: str, app_data: Path, sources: list) -> dict:
    """sources: [(Path, fmt_str), ...]"""
    account_dir = app_data / "accounts" / account_id
    conv_dir = account_dir / "conversations"
    media_dir = account_dir / "media"

    index = json.loads(
        (account_dir / "conversations.json").read_text(encoding="utf-8")
    )
    index_map = {item["id"]: item for item in index["items"]}

    # 源目录所有 conv_id → [(src, fmt), ...]
    src_conv_map: dict = {}
    for src, fmt in sources:
        if fmt == "app-native":
            for p in (src / "conversations").glob("*.jsonl"):
                src_conv_map.setdefault(p.stem, []).append((src, fmt))
        else:
            for p in src.glob("*.jsonl"):
                src_conv_map.setdefault(p.stem, []).append((src, fmt))

    changes, skipped, no_change = [], [], 0

    for conv_id, srcs in sorted(src_conv_map.items()):
        if conv_id not in index_map:
            # 不在 app 索引 → 跳过
            title = conv_id
            for s, f in srcs:
                r = load_source_conv(s, f, conv_id)
                if r:
                    title = r[1][:40]
                    break
            skipped.append({"id": conv_id, "title": title})
            continue

        app_jsonl = conv_dir / f"{conv_id}.jsonl"
        idx_item = index_map[conv_id]

        if not app_jsonl.exists():
            # JSONL 缺失 → 完整恢复
            for s, f in srcs:
                r = load_source_conv(s, f, conv_id)
                if r:
                    msgs, title = r
                    mids = media_ids_of(msgs)
                    changes.append(
                        {
                            "type": "restore",
                            "conv_id": conv_id,
                            "title": title,
                            "src": s,
                            "fmt": f,
                            "msgs": msgs,
                            "media_ids": mids,
                            "new_media": new_media_list(mids, s, media_dir),
                            "idx_item": idx_item,
                            "before": 0,
                            "after": len(msgs),
                        }
                    )
                    break
            continue

        # 两端都有 JSONL → 并集合并（src 有、app 无的 turn 全部补入）
        app_records = load_jsonl(app_jsonl)
        app_msgs = app_records[1:]  # 保留原始顺序，不重排

        best = {"additions": [], "kind": "source_subset", "stats": {}, "src": None, "fmt": None}
        for s, f in srcs:
            r = load_source_conv(s, f, conv_id)
            if not r:
                continue
            src_msgs, _ = r
            additions, kind, stats = find_union_additions(src_msgs, app_msgs)
            if len(additions) > len(best["additions"]):
                best = {"additions": additions, "kind": kind, "stats": stats, "src": s, "fmt": f}

        if not best["additions"]:
            no_change += 1
            continue

        adds = best["additions"]
        mids = media_ids_of(adds)
        turns_added = len({base_turn_id(m["id"]) for m in adds})
        changes.append(
            {
                "type": "merge",
                "kind": best["kind"],
                "conv_id": conv_id,
                "title": idx_item.get("title", conv_id),
                "src": best["src"],
                "fmt": best["fmt"],
                "additions": adds,
                "app_meta": app_records[0],
                "app_msgs": app_msgs,
                "media_ids": mids,
                "new_media": new_media_list(mids, best["src"], media_dir),
                "idx_item": idx_item,
                "before": len(app_msgs),
                "after": len(app_msgs) + len(adds),
                "turns_added": turns_added,
                "stats": best["stats"],
            }
        )

    return {
        "account_id": account_id,
        "account_dir": account_dir,
        "media_dir": media_dir,
        "sources": [(str(s), f) for s, f in sources],
        "changes": changes,
        "skipped": skipped,
        "no_change": no_change,
    }


# ── 打印报告 ──────────────────────────────────────────────────────────────────


def print_report(plan: dict):
    changes = plan["changes"]
    skipped = plan["skipped"]
    merges  = [c for c in changes if c["type"] == "merge"]
    restores = [c for c in changes if c["type"] == "restore"]
    all_new_media = [m for c in changes for m in c["new_media"]]

    src_labels = []
    for s, f in plan["sources"]:
        label = "[app备份]" if f == "app-native" else "[原始导出]"
        src_labels.append(f"{label} {Path(s).name}")

    # 按 kind 统计合并数量
    kind_counts: dict = {}
    for c in merges:
        kind_counts[c["kind"]] = kind_counts.get(c["kind"], 0) + 1
    merge_summary = "  ".join(
        f"{KIND_LABELS.get(k, k)}×{v}"
        for k, v in kind_counts.items()
    ) if kind_counts else "-"

    print("=" * 62)
    print("预合并报告")
    print("=" * 62)
    print(f"账号：{plan['account_id']}")
    print(f"来源：{'  |  '.join(src_labels)}")
    print()
    print(f"跳过（仅源有，app无）：{len(skipped)} 条")
    print(f"待合并（有缺失 turn）：{len(merges)} 条  [{merge_summary}]")
    print(f"待恢复（JSONL 缺失）： {len(restores)} 条")
    print(f"无变化（源⊆app）：    {plan['no_change']} 条")
    print()

    if changes:
        row = "%-3s %-6s %-28s %-14s %-16s %s"
        print(row % ("#", "类型", "对话标题", "补充turns", "消息数变化", "媒体"))
        print("-" * 84)
        for i, c in enumerate(changes, 1):
            if c["type"] == "merge":
                typ = KIND_LABELS.get(c["kind"], c["kind"])
                turns_s = f"+{c['turns_added']} turns"
                msgs_s  = f"{c['before']} → {c['after']} 条"
            else:  # restore
                typ = "恢复"
                n = len({base_turn_id(m["id"]) for m in c["msgs"]})
                turns_s = f"全部 {n} turns"
                msgs_s  = f"0 → {c['after']} 条"
            title = c["title"][:26]
            if c["new_media"]:
                sz = sum(m["size"] for m in c["new_media"])
                media_s = f"+{len(c['new_media'])} 个 ({fmt_size(sz)})"
            else:
                media_s = "-"
            print(row % (i, typ, title, turns_s, msgs_s, media_s))
        print()
        if all_new_media:
            total_sz = sum(m["size"] for m in all_new_media)
            print(f"媒体汇总：共新增 {len(all_new_media)} 个文件，约 {fmt_size(total_sz)}")
        else:
            print("媒体汇总：无新增媒体文件")
        print()

    # 异常合并警告（非"补旧"的合并需人工确认）
    anomalous = [c for c in merges if c["kind"] != "older"]
    if anomalous:
        print(f"⚠  异常合并（需人工确认）：{len(anomalous)} 条")
        for c in anomalous:
            kind_s = KIND_LABELS.get(c["kind"], c["kind"])
            s = c.get("stats", {})
            print(f"  [{kind_s}]  {c['conv_id']}  {c['title'][:30]}")
            print(f"         共同={s.get('common',0)}  仅源={s.get('src_only',0)}  仅app={s.get('app_only',0)}")
        print()

    if skipped:
        print(f"跳过的对话（{len(skipped)} 条，未列入 app 索引）：")
        for s in skipped[:10]:
            print(f"  {s['id']}  {s['title']}")
        if len(skipped) > 10:
            print(f"  ... 共 {len(skipped)} 条")
        print()

    if not changes:
        print("无需变更。")
    else:
        print("执行合并：添加 --apply 参数")
    print("=" * 62)


# ── 执行合并 ──────────────────────────────────────────────────────────────────


def apply_plan(plan: dict):
    account_dir = Path(plan["account_dir"])
    media_dir = Path(plan["media_dir"])
    conv_dir = account_dir / "conversations"
    index_path = account_dir / "conversations.json"
    meta_path = account_dir / "meta.json"

    index = json.loads(index_path.read_text(encoding="utf-8"))
    index_map = {item["id"]: item for item in index["items"]}

    for c in plan["changes"]:
        conv_id = c["conv_id"]
        jsonl_path = conv_dir / f"{conv_id}.jsonl"

        if c["type"] == "restore":
            msgs = c["msgs"]
            idx = index_map.get(conv_id, {})
            ts_list = [m["timestamp"] for m in msgs if m.get("timestamp")]
            meta_line = {
                "type": "meta",
                "id": conv_id,
                "accountId": plan["account_id"],
                "title": c["title"],
                "createdAt": min(ts_list) if ts_list else "",
                "updatedAt": idx.get("updatedAt", max(ts_list) if ts_list else ""),
                "remoteHash": idx.get("remoteHash"),
            }
            write_jsonl_atomic(jsonl_path, [meta_line] + msgs)
            if conv_id in index_map:
                img_cnt, vid_cnt = count_media_by_type(msgs)
                index_map[conv_id]["messageCount"] = len(msgs)
                index_map[conv_id]["hasMedia"] = bool(c["media_ids"])
                index_map[conv_id]["imageCount"] = img_cnt
                index_map[conv_id]["videoCount"] = vid_cnt

        elif c["type"] == "merge":
            app_meta = c["app_meta"]
            app_msgs = c["app_msgs"]
            all_msgs = merge_by_timestamp(app_msgs, c["additions"])
            # 只更新 createdAt（对话实际开始时间），其余字段不动
            ts_list = [m["timestamp"] for m in all_msgs if m.get("timestamp")]
            if ts_list:
                app_meta["createdAt"] = min(ts_list)
            write_jsonl_atomic(jsonl_path, [app_meta] + all_msgs)
            if conv_id in index_map:
                img_cnt, vid_cnt = count_media_by_type(all_msgs)
                index_map[conv_id]["messageCount"] = len(all_msgs)
                index_map[conv_id]["hasMedia"] = any(
                    m.get("attachments") for m in all_msgs
                )
                index_map[conv_id]["imageCount"] = img_cnt
                index_map[conv_id]["videoCount"] = vid_cnt

        # 复制新媒体文件
        src_dir = Path(c["src"])
        for mf in c["new_media"]:
            dst = media_dir / mf["id"]
            if not dst.exists():
                shutil.copy2(mf["src"], dst)

        print(f"  ✓ {c['type']}  {conv_id[:16]}  {c['title'][:30]}")

    index["updatedAt"] = now_iso()
    write_json_atomic(index_path, index)

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["conversationCount"] = len(index["items"])
    write_json_atomic(meta_path, meta)

    print(f"\n合并完成：{len(plan['changes'])} 条对话已更新")


# ── 自测 ──────────────────────────────────────────────────────────────────────


def _run_tests():
    import tempfile
    import traceback

    print("=" * 50)
    print("运行自测（模拟数据）")
    print("=" * 50)

    def make_msg(turn_id: str, role: str, ts: str, media_id: str = None) -> dict:
        suffix = "_u" if role == "user" else "_m"
        att = [{"mediaId": media_id, "mimeType": "image/png"}] if media_id else []
        return {
            "type": "message",
            "id": f"{turn_id}{suffix}",
            "role": role,
            "text": f"{turn_id} {role} text",
            "attachments": att,
            "timestamp": ts,
        }

    def make_meta(conv_id: str, account_id: str, title: str, ts: str) -> dict:
        return {
            "type": "meta",
            "id": conv_id,
            "accountId": account_id,
            "title": title,
            "createdAt": ts,
            "updatedAt": ts,
            "syncedAt": ts,
            "remoteHash": None,
        }

    def make_raw_turn(turn_id: str, ts_iso: str, ts_unix: int,
                      user_text: str, model_text: str, media_id: str = None) -> dict:
        files = []
        if media_id:
            files = [{"role": "user", "type": "image", "filename": "img.png",
                      "mime": "image/png", "url": "http://x", "thumbnail_url": "",
                      "media_id": media_id}]
        return {
            "turn_id": turn_id,
            "timestamp": ts_unix,
            "timestamp_iso": ts_iso,
            "user": {"text": user_text, "files": files},
            "assistant": {"text": model_text, "thinking": "", "model": "gemini", "files": []},
        }

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

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ACCOUNT = "test_account"

        # ── 构建 app 目录 ──────────────────────────────────────────────────
        app_data = tmp / "app_data"
        acc_dir = app_data / "accounts" / ACCOUNT
        conv_dir = acc_dir / "conversations"
        media_dir = acc_dir / "media"
        conv_dir.mkdir(parents=True)
        media_dir.mkdir(parents=True)

        # meta.json
        (acc_dir / "meta.json").write_text(
            json.dumps({"version": 1, "id": ACCOUNT, "conversationCount": 4,
                        "name": "Test", "email": "test@test.com",
                        "avatarText": "T", "avatarColor": "#000",
                        "remoteConversationCount": 4,
                        "lastSyncAt": "2026-01-01T00:00:00+00:00",
                        "lastSyncResult": "success"}),
            encoding="utf-8"
        )

        # ── 对话定义 ──────────────────────────────────────────────────────
        # conv_prepend_native: app 有 T2/T3，源有 T1/T2/T3 → 前插 T1
        PREPEND_NATIVE = "aabbccdd11223344"
        write_jsonl_atomic(
            conv_dir / f"{PREPEND_NATIVE}.jsonl",
            [
                make_meta(PREPEND_NATIVE, ACCOUNT, "原生前插测试对话", "2026-01-02T00:00:00+00:00"),
                make_msg("r_t2", "user",  "2026-01-02T00:00:00+00:00"),
                make_msg("r_t2", "model", "2026-01-02T00:00:00+00:00"),
                make_msg("r_t3", "user",  "2026-01-03T00:00:00+00:00"),
                make_msg("r_t3", "model", "2026-01-03T00:00:00+00:00"),
            ],
        )

        # conv_prepend_raw: app 有 T2，raw 源有 T1/T2 → 前插 T1（含媒体）
        PREPEND_RAW = "eeff99887766aabb"
        write_jsonl_atomic(
            conv_dir / f"{PREPEND_RAW}.jsonl",
            [
                make_meta(PREPEND_RAW, ACCOUNT, "Raw前插测试对话", "2026-01-02T00:00:00+00:00"),
                make_msg("r_r2", "user",  "2026-01-02T00:00:00+00:00"),
                make_msg("r_r2", "model", "2026-01-02T00:00:00+00:00"),
            ],
        )

        # conv_restore: app 索引有但 JSONL 缺失 → 完整恢复
        RESTORE = "1122334455667788"

        # conv_nochange: app 与源 turns 完全相同 → 无变化
        NOCHANGE = "ffffeeeeddddcccc"
        write_jsonl_atomic(
            conv_dir / f"{NOCHANGE}.jsonl",
            [
                make_meta(NOCHANGE, ACCOUNT, "无变化对话", "2026-01-01T00:00:00+00:00"),
                make_msg("r_n1", "user",  "2026-01-01T00:00:00+00:00"),
                make_msg("r_n1", "model", "2026-01-01T00:00:00+00:00"),
            ],
        )

        # conv_no_overlap: app 有 T_X1，源有 T_Y1（完全不同的 turn）
        NO_OVERLAP = "aaaa1111bbbb2222"
        write_jsonl_atomic(
            conv_dir / f"{NO_OVERLAP}.jsonl",
            [
                make_meta(NO_OVERLAP, ACCOUNT, "无重叠测试对话", "2026-01-01T00:00:00+00:00"),
                make_msg("r_x1", "user",  "2026-01-01T00:00:00+00:00"),
                make_msg("r_x1", "model", "2026-01-01T00:00:00+00:00"),
            ],
        )

        # conv_src_fwd: app 有 T_F1，源有 T_F1/T_F2（源超前）
        SRC_FWD = "cccc3333dddd4444"
        write_jsonl_atomic(
            conv_dir / f"{SRC_FWD}.jsonl",
            [
                make_meta(SRC_FWD, ACCOUNT, "源超前测试对话", "2026-01-01T00:00:00+00:00"),
                make_msg("r_f1", "user",  "2026-01-01T00:00:00+00:00"),
                make_msg("r_f1", "model", "2026-01-01T00:00:00+00:00"),
            ],
        )

        # conv_interleaved: app 有 T_I1/T_I3，源有 T_I1/T_I2/T_I3（交错）
        INTERLEAVED = "eeee5555ffff6666"
        write_jsonl_atomic(
            conv_dir / f"{INTERLEAVED}.jsonl",
            [
                make_meta(INTERLEAVED, ACCOUNT, "交错测试对话", "2026-01-01T00:00:00+00:00"),
                make_msg("r_i1", "user",  "2026-01-01T00:00:00+00:00"),
                make_msg("r_i1", "model", "2026-01-01T00:00:00+00:00"),
                make_msg("r_i3", "user",  "2026-01-03T00:00:00+00:00"),
                make_msg("r_i3", "model", "2026-01-03T00:00:00+00:00"),
            ],
        )

        # conv_skip: 不在 app 索引 → 跳过
        SKIP = "deadbeefcafe1234"

        # conversations.json（包含 7 个对话，不含 SKIP）
        (acc_dir / "conversations.json").write_text(
            json.dumps({
                "version": 1,
                "accountId": ACCOUNT,
                "updatedAt": "2026-01-01T00:00:00+00:00",
                "totalCount": 7,
                "items": [
                    {"id": PREPEND_NATIVE, "title": "原生前插测试对话",
                     "lastMessage": "", "messageCount": 4, "hasMedia": False,
                     "updatedAt": "2026-01-03T00:00:00+00:00",
                     "syncedAt": "2026-01-03T00:00:00+00:00", "remoteHash": None},
                    {"id": PREPEND_RAW, "title": "Raw前插测试对话",
                     "lastMessage": "", "messageCount": 2, "hasMedia": False,
                     "updatedAt": "2026-01-02T00:00:00+00:00",
                     "syncedAt": "2026-01-02T00:00:00+00:00", "remoteHash": None},
                    {"id": RESTORE, "title": "恢复测试对话",
                     "lastMessage": "", "messageCount": 4, "hasMedia": False,
                     "updatedAt": "2026-01-01T00:00:00+00:00",
                     "syncedAt": "2026-01-01T00:00:00+00:00", "remoteHash": None},
                    {"id": NOCHANGE, "title": "无变化对话",
                     "lastMessage": "", "messageCount": 2, "hasMedia": False,
                     "updatedAt": "2026-01-01T00:00:00+00:00",
                     "syncedAt": "2026-01-01T00:00:00+00:00", "remoteHash": None},
                    {"id": NO_OVERLAP, "title": "无重叠测试对话",
                     "lastMessage": "", "messageCount": 2, "hasMedia": False,
                     "updatedAt": "2026-01-01T00:00:00+00:00",
                     "syncedAt": "2026-01-01T00:00:00+00:00", "remoteHash": None},
                    {"id": SRC_FWD, "title": "源超前测试对话",
                     "lastMessage": "", "messageCount": 2, "hasMedia": False,
                     "updatedAt": "2026-01-01T00:00:00+00:00",
                     "syncedAt": "2026-01-01T00:00:00+00:00", "remoteHash": None},
                    {"id": INTERLEAVED, "title": "交错测试对话",
                     "lastMessage": "", "messageCount": 4, "hasMedia": False,
                     "updatedAt": "2026-01-03T00:00:00+00:00",
                     "syncedAt": "2026-01-03T00:00:00+00:00", "remoteHash": None},
                ],
            }),
            encoding="utf-8"
        )

        # ── 构建 app-native 源 ─────────────────────────────────────────────
        src_native = tmp / "source_native"
        src_native_conv = src_native / "conversations"
        src_native_conv.mkdir(parents=True)
        (src_native / "media").mkdir()

        # conv_prepend_native: T1 + T2 + T3（T1 比 app 更老）
        write_jsonl_atomic(
            src_native_conv / f"{PREPEND_NATIVE}.jsonl",
            [
                make_meta(PREPEND_NATIVE, ACCOUNT, "原生前插测试对话", "2026-01-01T00:00:00+00:00"),
                make_msg("r_t1", "user",  "2026-01-01T00:00:00+00:00"),
                make_msg("r_t1", "model", "2026-01-01T00:00:00+00:00"),
                make_msg("r_t2", "user",  "2026-01-02T00:00:00+00:00"),
                make_msg("r_t2", "model", "2026-01-02T00:00:00+00:00"),
                make_msg("r_t3", "user",  "2026-01-03T00:00:00+00:00"),
                make_msg("r_t3", "model", "2026-01-03T00:00:00+00:00"),
            ],
        )

        # conv_restore: T1 + T2（app 索引有但 JSONL 缺失）
        write_jsonl_atomic(
            src_native_conv / f"{RESTORE}.jsonl",
            [
                make_meta(RESTORE, ACCOUNT, "恢复测试对话", "2026-01-01T00:00:00+00:00"),
                make_msg("r_s1", "user",  "2026-01-01T00:00:00+00:00"),
                make_msg("r_s1", "model", "2026-01-01T00:00:00+00:00"),
                make_msg("r_s2", "user",  "2026-01-02T00:00:00+00:00"),
                make_msg("r_s2", "model", "2026-01-02T00:00:00+00:00"),
            ],
        )

        # conv_nochange: 和 app 完全相同
        write_jsonl_atomic(
            src_native_conv / f"{NOCHANGE}.jsonl",
            [
                make_meta(NOCHANGE, ACCOUNT, "无变化对话", "2026-01-01T00:00:00+00:00"),
                make_msg("r_n1", "user",  "2026-01-01T00:00:00+00:00"),
                make_msg("r_n1", "model", "2026-01-01T00:00:00+00:00"),
            ],
        )

        # conv_skip: 不在 app 索引
        write_jsonl_atomic(
            src_native_conv / f"{SKIP}.jsonl",
            [
                make_meta(SKIP, ACCOUNT, "跳过的对话", "2026-01-01T00:00:00+00:00"),
                make_msg("r_x1", "user",  "2026-01-01T00:00:00+00:00"),
                make_msg("r_x1", "model", "2026-01-01T00:00:00+00:00"),
            ],
        )

        # conv_no_overlap: 源完全不同的 turn（T_Y1 vs app 的 T_X1）
        write_jsonl_atomic(
            src_native_conv / f"{NO_OVERLAP}.jsonl",
            [
                make_meta(NO_OVERLAP, ACCOUNT, "无重叠测试对话", "2026-01-01T00:00:00+00:00"),
                make_msg("r_y1", "user",  "2026-01-01T00:00:00+00:00"),
                make_msg("r_y1", "model", "2026-01-01T00:00:00+00:00"),
            ],
        )

        # conv_src_fwd: 源有 T_F1/T_F2，app 只有 T_F1
        write_jsonl_atomic(
            src_native_conv / f"{SRC_FWD}.jsonl",
            [
                make_meta(SRC_FWD, ACCOUNT, "源超前测试对话", "2026-01-01T00:00:00+00:00"),
                make_msg("r_f1", "user",  "2026-01-01T00:00:00+00:00"),
                make_msg("r_f1", "model", "2026-01-01T00:00:00+00:00"),
                make_msg("r_f2", "user",  "2026-01-02T00:00:00+00:00"),
                make_msg("r_f2", "model", "2026-01-02T00:00:00+00:00"),
            ],
        )

        # conv_interleaved: 源有 T_I1/T_I2/T_I3，app 只有 T_I1/T_I3
        write_jsonl_atomic(
            src_native_conv / f"{INTERLEAVED}.jsonl",
            [
                make_meta(INTERLEAVED, ACCOUNT, "交错测试对话", "2026-01-01T00:00:00+00:00"),
                make_msg("r_i1", "user",  "2026-01-01T00:00:00+00:00"),
                make_msg("r_i1", "model", "2026-01-01T00:00:00+00:00"),
                make_msg("r_i2", "user",  "2026-01-02T00:00:00+00:00"),
                make_msg("r_i2", "model", "2026-01-02T00:00:00+00:00"),
                make_msg("r_i3", "user",  "2026-01-03T00:00:00+00:00"),
                make_msg("r_i3", "model", "2026-01-03T00:00:00+00:00"),
            ],
        )

        # ── 构建 raw-export 源 ────────────────────────────────────────────
        src_raw = tmp / "source_raw"
        src_raw.mkdir()
        src_raw_media = src_raw / "media"
        src_raw_media.mkdir()

        # conv_prepend_raw: T1（raw 格式，含媒体）+ T2
        FAKE_MEDIA_ID = "abcdef1234567890abcdef1234567890.png"
        (src_raw_media / FAKE_MEDIA_ID).write_bytes(b"PNG_FAKE_DATA_12345")

        write_jsonl_atomic(
            src_raw / f"{PREPEND_RAW}.jsonl",
            [
                make_raw_turn("r_r1", "2026-01-01T00:00:00+00:00", 1735689600,
                              "r_r1 user text", "r_r1 model text", FAKE_MEDIA_ID),
                make_raw_turn("r_r2", "2026-01-02T00:00:00+00:00", 1735776000,
                              "r_r2 user text", "r_r2 model text"),
            ],
        )

        # chat_list_full.json
        (src_raw / "chat_list_full.json").write_text(
            json.dumps([
                {"id": f"c_{PREPEND_RAW}", "title": "Raw前插测试对话",
                 "latest_update_ts": 1735776000, "latest_update_iso": "2026-01-02T00:00:00+00:00"},
            ]),
            encoding="utf-8"
        )

        # ── 执行测试 ──────────────────────────────────────────────────────
        try:
            sources = [
                (src_native, detect_format(src_native)),
                (src_raw, detect_format(src_raw)),
            ]
            plan = build_plan(ACCOUNT, app_data, sources)

            print("\n--- 计划验证 ---")
            check("跳过数量 == 1", len(plan["skipped"]) == 1,
                  f"实际={len(plan['skipped'])}")
            check("跳过的是 SKIP conv", plan["skipped"][0]["id"] == SKIP,
                  plan["skipped"][0]["id"])
            check("无变化 == 1", plan["no_change"] == 1,
                  f"实际={plan['no_change']}")

            merges   = [c for c in plan["changes"] if c["type"] == "merge"]
            restores = [c for c in plan["changes"] if c["type"] == "restore"]
            check("合并数量 == 5", len(merges) == 5, f"实际={len(merges)}")
            check("恢复数量 == 1", len(restores) == 1, f"实际={len(restores)}")

            older_ms      = [c for c in merges if c["kind"] == "older"]
            newer_ms      = [c for c in merges if c["kind"] == "newer"]
            interleaved_ms = [c for c in merges if c["kind"] == "interleaved"]
            no_overlap_ms  = [c for c in merges if c["kind"] == "no_overlap"]
            check("older 合并 == 2",      len(older_ms) == 2,      f"实际={len(older_ms)}")
            check("newer 合并 == 1",      len(newer_ms) == 1,      f"实际={len(newer_ms)}")
            check("interleaved 合并 == 1", len(interleaved_ms) == 1, f"实际={len(interleaved_ms)}")
            check("no_overlap 合并 == 1",  len(no_overlap_ms) == 1,  f"实际={len(no_overlap_ms)}")

            # 验证原生 older 合并
            pn = next((c for c in older_ms if c["conv_id"] == PREPEND_NATIVE), None)
            check("原生 older 对话存在", pn is not None)
            if pn:
                check("原生 older turns_added == 1", pn["turns_added"] == 1,
                      f"实际={pn['turns_added']}")
                check("原生 older before=4 after=6",
                      pn["before"] == 4 and pn["after"] == 6,
                      f"before={pn['before']} after={pn['after']}")

            # 验证 raw older 合并
            pr = next((c for c in older_ms if c["conv_id"] == PREPEND_RAW), None)
            check("Raw older 对话存在", pr is not None)
            if pr:
                check("Raw older turns_added == 1", pr["turns_added"] == 1,
                      f"实际={pr['turns_added']}")
                check("Raw older before=2 after=4",
                      pr["before"] == 2 and pr["after"] == 4,
                      f"before={pr['before']} after={pr['after']}")
                check("Raw older 有媒体文件",
                      len(pr["new_media"]) == 1 and pr["new_media"][0]["id"] == FAKE_MEDIA_ID)

            # 验证异常合并 kind
            check("no_overlap conv_id 正确",
                  no_overlap_ms[0]["conv_id"] == NO_OVERLAP if no_overlap_ms else False)
            check("newer conv_id 正确",
                  newer_ms[0]["conv_id"] == SRC_FWD if newer_ms else False)
            check("interleaved conv_id 正确",
                  interleaved_ms[0]["conv_id"] == INTERLEAVED if interleaved_ms else False)
            check("interleaved turns_added == 1",
                  interleaved_ms[0]["turns_added"] == 1 if interleaved_ms else False,
                  f"实际={interleaved_ms[0]['turns_added'] if interleaved_ms else 'N/A'}")

            # 验证恢复
            rs = restores[0]
            check("恢复对话 ID 正确", rs["conv_id"] == RESTORE, rs["conv_id"])
            check("恢复 after=4", rs["after"] == 4, f"实际={rs['after']}")

            # ── 执行 apply ────────────────────────────────────────────────
            print("\n--- 执行 apply ---")
            apply_plan(plan)

            print("\n--- 写入验证 ---")
            # 验证原生 older 写入
            recs = load_jsonl(conv_dir / f"{PREPEND_NATIVE}.jsonl")
            msgs_written = recs[1:]
            check("原生 older：文件消息数 == 6", len(msgs_written) == 6,
                  f"实际={len(msgs_written)}")
            check("原生 older：第1条是 r_t1_u", msgs_written[0]["id"] == "r_t1_u",
                  msgs_written[0]["id"])
            check("原生 older：createdAt 更新为最老 turn",
                  recs[0]["createdAt"] == "2026-01-01T00:00:00+00:00",
                  recs[0]["createdAt"])
            check("原生 older：updatedAt 不变",
                  recs[0]["updatedAt"] == "2026-01-02T00:00:00+00:00",
                  recs[0].get("updatedAt"))

            # 验证 raw older 写入
            recs_r = load_jsonl(conv_dir / f"{PREPEND_RAW}.jsonl")
            msgs_r = recs_r[1:]
            check("Raw older：文件消息数 == 4", len(msgs_r) == 4, f"实际={len(msgs_r)}")
            check("Raw older：第1条是 r_r1_u", msgs_r[0]["id"] == "r_r1_u", msgs_r[0]["id"])
            check("Raw older：媒体已复制", (media_dir / FAKE_MEDIA_ID).exists())

            # 验证恢复写入
            recs_s = load_jsonl(conv_dir / f"{RESTORE}.jsonl")
            check("恢复：JSONL 已创建", len(recs_s) == 5,
                  f"行数={len(recs_s)}")  # 1 meta + 4 msgs

            # 验证无变化对话未被修改
            recs_n = load_jsonl(conv_dir / f"{NOCHANGE}.jsonl")
            check("无变化：消息数仍为 2", len(recs_n) - 1 == 2,
                  f"实际={len(recs_n) - 1}")

            # 验证 newer 写入（T_F1 + T_F2，T_F2 追加在后）
            recs_f = load_jsonl(conv_dir / f"{SRC_FWD}.jsonl")
            msgs_f = recs_f[1:]
            check("newer：消息数 == 4", len(msgs_f) == 4, f"实际={len(msgs_f)}")
            check("newer：最后是 r_f2_m", msgs_f[-1]["id"] == "r_f2_m", msgs_f[-1]["id"])

            # 验证 interleaved 写入（T_I1/T_I2/T_I3，T_I2 插入中间）
            recs_i = load_jsonl(conv_dir / f"{INTERLEAVED}.jsonl")
            msgs_i = recs_i[1:]
            check("interleaved：消息数 == 6", len(msgs_i) == 6, f"实际={len(msgs_i)}")
            check("interleaved：中间是 r_i2_u", msgs_i[2]["id"] == "r_i2_u", msgs_i[2]["id"])

            # 验证 no_overlap 写入（T_X1 + T_Y1，时间戳相同，app turn 在前）
            recs_x = load_jsonl(conv_dir / f"{NO_OVERLAP}.jsonl")
            msgs_x = recs_x[1:]
            check("no_overlap：消息数 == 4", len(msgs_x) == 4, f"实际={len(msgs_x)}")
            check("no_overlap：r_x1_u 在前", msgs_x[0]["id"] == "r_x1_u", msgs_x[0]["id"])

            # 验证 meta.json conversationCount
            meta_data = json.loads((acc_dir / "meta.json").read_text(encoding="utf-8"))
            check("meta.json conversationCount == 7",
                  meta_data["conversationCount"] == 7,
                  f"实际={meta_data['conversationCount']}")

        except Exception:
            traceback.print_exc()
            failed += 1

    print()
    print(f"结果：{passed} 通过  {failed} 失败")
    print("=" * 50)
    return failed == 0


# ── 主入口 ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="外部备份合并到 app 账号数据")
    parser.add_argument("--account", help="账号 ID，如 cynaustine88_gmail_com")
    parser.add_argument("--sources", nargs="+", help="源目录（可多个）")
    parser.add_argument(
        "--app-data", default=str(APP_DATA_DEFAULT), help="app 数据根目录"
    )
    parser.add_argument("--apply", action="store_true", help="执行合并（默认只显示报告）")
    parser.add_argument("--test", action="store_true", help="运行自测")
    args = parser.parse_args()

    if args.test:
        ok = _run_tests()
        sys.exit(0 if ok else 1)

    if not args.account or not args.sources:
        parser.error("--account 和 --sources 为必填项（或使用 --test）")

    app_data = Path(args.app_data)
    account_dir = app_data / "accounts" / args.account
    if not account_dir.exists():
        print(f"错误：账号目录不存在: {account_dir}", file=sys.stderr)
        sys.exit(1)

    sources = []
    for s in args.sources:
        p = Path(s).expanduser()
        if not p.exists():
            print(f"错误：源目录不存在: {p}", file=sys.stderr)
            sys.exit(1)
        sources.append((p, detect_format(p)))

    plan = build_plan(args.account, app_data, sources)
    print_report(plan)

    if args.apply:
        if not plan["changes"]:
            print("没有需要合并的内容。")
            return
        print("\n开始执行合并...")
        apply_plan(plan)


if __name__ == "__main__":
    main()
