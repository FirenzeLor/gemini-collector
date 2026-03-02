#!/usr/bin/env python3
"""
refresh_media_counts.py - 刷新 cynaustraline_gmail_com 账号的 imageCount / videoCount

扫描所有对话 JSONL，重新统计各对话的图片/视频数量，原子写回 conversations.json。

用法：
  # 预览（不写文件）
  python3 scripts/refresh_media_counts.py

  # 执行写入
  python3 scripts/refresh_media_counts.py --apply
"""

import argparse
import json
from pathlib import Path

ACCOUNT_ID = "cynaustraline_gmail_com"
APP_DATA = Path.home() / "Library" / "Application Support" / "com.gemini-collector.app"


def load_jsonl(path: Path) -> list:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def write_json_atomic(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(path)


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


def main():
    parser = argparse.ArgumentParser(description="刷新 imageCount / videoCount")
    parser.add_argument("--apply", action="store_true", help="执行写入（默认只预览）")
    args = parser.parse_args()

    acct_dir = APP_DATA / "accounts" / ACCOUNT_ID
    conv_dir = acct_dir / "conversations"
    index_path = acct_dir / "conversations.json"

    index = json.loads(index_path.read_text(encoding="utf-8"))
    index_map = {item["id"]: item for item in index["items"]}

    changed = 0
    skipped_missing = 0
    skipped_parse_err = 0

    for conv_id, item in index_map.items():
        jsonl = conv_dir / f"{conv_id}.jsonl"
        if not jsonl.exists():
            skipped_missing += 1
            continue

        try:
            records = load_jsonl(jsonl)
        except Exception as e:
            print(f"  [解析失败] {conv_id}: {e}")
            skipped_parse_err += 1
            continue

        msgs = [r for r in records if r.get("type") == "message"]
        img_cnt, vid_cnt = count_media_by_type(msgs)

        old_img = item.get("imageCount", -1)
        old_vid = item.get("videoCount", -1)

        if old_img != img_cnt or old_vid != vid_cnt:
            changed += 1
            print(
                f"  {'[写入]' if args.apply else '[预览]'} {conv_id[:16]}  "
                f"image: {old_img} → {img_cnt}  video: {old_vid} → {vid_cnt}  "
                f"{item.get('title', '')[:30]}"
            )
            if args.apply:
                item["imageCount"] = img_cnt
                item["videoCount"] = vid_cnt

    print(
        f"\n共扫描 {len(index_map)} 条，"
        f"需更新 {changed} 条，"
        f"JSONL 缺失 {skipped_missing} 条，"
        f"解析失败 {skipped_parse_err} 条"
    )

    if args.apply and changed:
        write_json_atomic(index_path, index)
        print("conversations.json 已写入。")
    elif not args.apply:
        print("\n添加 --apply 参数执行写入。")


if __name__ == "__main__":
    main()
