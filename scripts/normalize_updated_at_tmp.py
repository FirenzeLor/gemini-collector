#!/usr/bin/env python3
"""
一次性修复本地会话时间字段格式：
- conversations.json: items[].updatedAt / items[].remoteHash
- conversations/*.jsonl: 首行 meta.updatedAt / meta.remoteHash

统一规则：
- 优先使用 remoteHash(秒级时间戳) 作为权威时间
- 若 remoteHash 不可用，则尝试从 updatedAt 解析时间
- 统一输出:
  - updatedAt: UTC ISO8601 (例如 2026-02-27T12:34:56+00:00)
  - remoteHash: 秒级时间戳字符串 (例如 "1740659696")
"""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
from typing import Any, Tuple


def _epoch_to_iso_utc(epoch_seconds: int) -> str:
    return datetime.datetime.fromtimestamp(int(epoch_seconds), datetime.UTC).isoformat()


def _parse_epoch_from_remote_hash(raw: Any) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if s.isdigit():
            return int(s)
    return None


def _parse_epoch_from_updated_at(raw: Any) -> int | None:
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = f"{s[:-1]}+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return int(dt.astimezone(datetime.UTC).timestamp())


def _normalize_pair(updated_at: Any, remote_hash: Any) -> Tuple[str | None, str | None, bool]:
    ts = _parse_epoch_from_remote_hash(remote_hash)
    if ts is None:
        ts = _parse_epoch_from_updated_at(updated_at)
    if ts is None:
        return (
            updated_at if isinstance(updated_at, str) and updated_at.strip() else None,
            remote_hash if isinstance(remote_hash, str) and remote_hash.strip() else None,
            False,
        )

    normalized_updated_at = _epoch_to_iso_utc(ts)
    normalized_remote_hash = str(ts)
    changed = (updated_at != normalized_updated_at) or (remote_hash != normalized_remote_hash)
    return normalized_updated_at, normalized_remote_hash, changed


def _update_conversations_index(account_dir: Path, dry_run: bool) -> dict[str, int]:
    stats = {"items_seen": 0, "items_changed": 0, "file_changed": 0}
    conv_index_file = account_dir / "conversations.json"
    if not conv_index_file.exists():
        return stats

    try:
        data = json.loads(conv_index_file.read_text(encoding="utf-8"))
    except Exception:
        return stats
    if not isinstance(data, dict):
        return stats

    items = data.get("items")
    if not isinstance(items, list):
        return stats

    changed = False
    for item in items:
        if not isinstance(item, dict):
            continue
        stats["items_seen"] += 1
        updated, remote_hash, item_changed = _normalize_pair(
            item.get("updatedAt"),
            item.get("remoteHash"),
        )
        if item_changed:
            item["updatedAt"] = updated
            item["remoteHash"] = remote_hash
            stats["items_changed"] += 1
            changed = True

    if changed:
        stats["file_changed"] = 1
        if not dry_run:
            data["updatedAt"] = datetime.datetime.now(datetime.UTC).isoformat()
            conv_index_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    return stats


def _update_jsonl_meta(account_dir: Path, dry_run: bool) -> dict[str, int]:
    stats = {"files_seen": 0, "files_changed": 0}
    conv_dir = account_dir / "conversations"
    if not conv_dir.exists():
        return stats

    for jsonl_file in conv_dir.glob("*.jsonl"):
        stats["files_seen"] += 1
        try:
            lines = jsonl_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        if not lines:
            continue

        try:
            meta = json.loads(lines[0])
        except Exception:
            continue
        if not isinstance(meta, dict) or meta.get("type") != "meta":
            continue

        updated, remote_hash, changed = _normalize_pair(
            meta.get("updatedAt"),
            meta.get("remoteHash"),
        )
        if not changed:
            continue

        meta["updatedAt"] = updated
        meta["remoteHash"] = remote_hash
        lines[0] = json.dumps(meta, ensure_ascii=False)
        stats["files_changed"] += 1
        if not dry_run:
            jsonl_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return stats


def _iter_account_dirs(root: Path):
    accounts_dir = root / "accounts"
    if not accounts_dir.exists():
        return []
    out = []
    for p in accounts_dir.iterdir():
        if p.is_dir():
            out.append(p)
    out.sort(key=lambda x: x.name)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="一次性统一本地 updatedAt/remoteHash 格式")
    parser.add_argument(
        "--root",
        default="gemini_export_output",
        help="导出根目录（包含 accounts/）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅统计，不写入文件",
    )
    args = parser.parse_args()

    root = Path(args.root)
    accounts = _iter_account_dirs(root)
    if not accounts:
        print(f"[warn] 未找到账号目录: {root / 'accounts'}")
        return 0

    total = {
        "accounts": 0,
        "index_items_seen": 0,
        "index_items_changed": 0,
        "index_files_changed": 0,
        "jsonl_files_seen": 0,
        "jsonl_files_changed": 0,
    }

    for account_dir in accounts:
        total["accounts"] += 1
        idx_stats = _update_conversations_index(account_dir, dry_run=args.dry_run)
        jsonl_stats = _update_jsonl_meta(account_dir, dry_run=args.dry_run)

        total["index_items_seen"] += idx_stats["items_seen"]
        total["index_items_changed"] += idx_stats["items_changed"]
        total["index_files_changed"] += idx_stats["file_changed"]
        total["jsonl_files_seen"] += jsonl_stats["files_seen"]
        total["jsonl_files_changed"] += jsonl_stats["files_changed"]

        print(
            f"[account:{account_dir.name}] "
            f"index {idx_stats['items_changed']}/{idx_stats['items_seen']} changed, "
            f"jsonl {jsonl_stats['files_changed']}/{jsonl_stats['files_seen']} changed"
        )

    mode = "DRY-RUN" if args.dry_run else "APPLY"
    print(
        f"[{mode}] accounts={total['accounts']}, "
        f"index_items_changed={total['index_items_changed']}/{total['index_items_seen']}, "
        f"index_files_changed={total['index_files_changed']}, "
        f"jsonl_files_changed={total['jsonl_files_changed']}/{total['jsonl_files_seen']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
