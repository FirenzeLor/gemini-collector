#!/usr/bin/env python3
"""
Gemini 导出 CLI 入口：全量导出、增量导出、main。
"""

import datetime
import json
import sys
from pathlib import Path

from gemini_protocol import normalize_chat_id, email_to_account_id, _coerce_epoch_seconds, _summary_to_epoch_seconds, mask_email
from gemini_cookies import get_cookies_from_local_browser, discover_email_authuser_mapping
from gemini_storage import (
    _load_conversations_index,
    _load_media_manifest_new, _save_media_manifest_new,
    _build_existing_turn_id_set_new, _count_message_rows_new,
    _count_media_types_from_rows, _rows_has_failed_data,
    _read_jsonl_rows, _write_jsonl_rows,
    _sort_parsed_turns_by_timestamp, _turns_to_jsonl_rows,
    _merge_message_rows_for_write, _remote_hash_from_jsonl,
    _write_accounts_json, _write_account_meta,
    _write_conversations_index, _write_sync_state,
    _normalize_conversation_status, _status_for_remote_summary,
    _build_lost_summary, _build_summary_from_chat_listing,
    CONVERSATION_STATUS_NORMAL, CONVERSATION_STATUS_LOST,
    _dedupe_raw_turns_by_id,
    _update_jsonl_media_failure_flags,
)
from gemini_media import _ensure_video_previews_from_turns
from gemini_turn_parser import parse_turn, normalize_turn_media_first_seen
from gemini_export import GeminiExporter, OUTPUT_DIR


def export_all(exporter, output_dir=None, chat_ids=None):
    """
    导出所有（或指定的）聊天数据

    输出结构:
      <output_dir>/
        accounts.json
        accounts/{account_id}/
          meta.json
          conversations.json
          sync_state.json
          conversations/{bare_id}.jsonl   — 首行 meta，其余每行一条 message
          media/{media_id}.{ext}
    调用前需先完成 init_auth（由上层统一初始化）。
    """
    now_iso = datetime.datetime.now(datetime.UTC).isoformat()
    base_dir = Path(output_dir) if output_dir else OUTPUT_DIR
    base_dir.mkdir(parents=True, exist_ok=True)

    # 1. 解析账号信息
    account_info = exporter._resolve_account_info()
    account_id = account_info["id"]
    account_dir = base_dir / "accounts" / account_id
    conv_dir = account_dir / "conversations"
    media_dir = account_dir / "media"

    account_dir.mkdir(parents=True, exist_ok=True)
    conv_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)
    exporter._set_request_state_scope(account_dir)
    existing_order, existing_index = _load_conversations_index(account_dir)

    print(f"[*] 账号: {mask_email(account_info['email']) or account_id}")
    print(f"[*] 输出目录: {account_dir.absolute()}")

    # 3. 获取聊天列表
    if chat_ids:
        chats = [{
            "id": normalize_chat_id(cid),
            "title": "",
            "latest_update_ts": None,
            "latest_update_iso": None,
        } for cid in chat_ids]
        print(f"[*] 指定导出 {len(chats)} 个对话")
    else:
        chats = exporter.get_all_chats()

    if not chats:
        print("[!] 未找到任何对话")
        return

    account_info["remoteConversationCount"] = len(chats)

    # 4. 逐个导出对话详情
    total = len(chats)
    stats = {
        "success": 0,
        "failed": 0,
        "media_downloaded": 0,
        "media_failed": 0,
        "preview_generated": 0,
        "preview_failed": 0,
    }

    global_seen_urls = _load_media_manifest_new(account_dir)
    global_used_names = set(global_seen_urls.values())
    for f in media_dir.iterdir():
        if f.is_file():
            global_used_names.add(f.name)

    conv_summaries = []
    failed_ids = []

    for idx, chat in enumerate(chats):
        conv_id = chat["id"]
        bare_id = conv_id.replace("c_", "")
        title = chat.get("title", "")
        existing_summary = existing_index.get(bare_id)
        remote_status = _status_for_remote_summary(existing_summary)
        print(f"\n[{idx + 1}/{total}] {title} ({conv_id})")

        try:
            raw_turns = exporter.get_chat_detail(conv_id)
            raw_turns, removed_turns = _dedupe_raw_turns_by_id(raw_turns)
            print(f"  轮次: {len(raw_turns)}")
            if removed_turns > 0:
                print(f"  [dedupe] 分页结果去重: {removed_turns} 个重复 turn")

            parsed_turns = [parse_turn(turn) for turn in raw_turns]
            parsed_turns = normalize_turn_media_first_seen(parsed_turns)

            batch_list = exporter._assign_media_ids_and_collect_downloads(
                parsed_turns, media_dir, global_seen_urls, global_used_names,
            )

            # 转换为新 JSONL 格式并写入
            rows = _turns_to_jsonl_rows(parsed_turns, conv_id, account_id, title, chat)
            jsonl_file = conv_dir / f"{bare_id}.jsonl"
            _write_jsonl_rows(jsonl_file, rows)

            # 下载媒体
            failed_items = []
            if batch_list:
                print(f"  媒体文件: {len(batch_list)} 个（去重后）")
                failed_items = exporter.download_media_batch(batch_list, media_dir, stats)
                _save_media_manifest_new(account_dir, global_seen_urls)
            preview_stats = _ensure_video_previews_from_turns(parsed_turns, media_dir)
            stats["preview_generated"] += preview_stats["preview_generated"]
            stats["preview_failed"] += preview_stats["preview_failed"]

            batch_media_ids = {item.get("media_id") or item["filepath"].name for item in batch_list}
            failed_map = {item["media_id"]: (item.get("error") or "download_failed") for item in failed_items}
            recovered_ids = batch_media_ids - set(failed_map.keys())
            flag_stats = _update_jsonl_media_failure_flags(jsonl_file, failed_map, recovered_ids)
            if flag_stats["marked"] > 0 or flag_stats["cleared"] > 0:
                print(
                    "  [media-flag] 已更新附件下载标记:"
                    f" marked={flag_stats['marked']},"
                    f" cleared={flag_stats['cleared']}"
                )

            # 构建 summary
            rows_after = _read_jsonl_rows(jsonl_file)
            meta_row = (
                next(
                    (r for r in rows_after if isinstance(r, dict) and r.get("type") == "meta"),
                    None,
                )
                or rows[0]
            )
            msg_rows = [
                r for r in rows_after if isinstance(r, dict) and r.get("type") == "message"
            ]
            has_media = any(r.get("attachments") for r in msg_rows)
            has_failed_data = _rows_has_failed_data(msg_rows)
            image_count, video_count, _audio_count = _count_media_types_from_rows(msg_rows)
            last_text = ""
            for r in reversed(msg_rows):
                if r.get("text"):
                    last_text = r["text"][:80]
                    break

            conv_summaries.append({
                "id": bare_id,
                "title": title,
                "lastMessage": last_text,
                "messageCount": len(msg_rows),
                "hasMedia": has_media,
                "hasFailedData": has_failed_data,
                "imageCount": image_count,
                "videoCount": video_count,
                "updatedAt": meta_row.get("updatedAt"),
                "remoteHash": meta_row.get("remoteHash"),
                "status": remote_status,
            })
            stats["success"] += 1

        except Exception as e:
            print(f"  [!] 导出失败: {e}")
            import traceback; traceback.print_exc()
            stats["failed"] += 1
            failed_ids.append(bare_id)
            chat_remote_ts = _coerce_epoch_seconds(chat.get("latest_update_ts"))
            conv_summaries.append({
                "id": bare_id,
                "title": title,
                "lastMessage": "",
                "messageCount": 0,
                "hasMedia": False,
                "hasFailedData": False,
                "imageCount": 0,
                "videoCount": 0,
                "updatedAt": _to_iso_utc(chat_remote_ts) or chat.get("latest_update_iso"),
                "remoteHash": str(chat_remote_ts) if chat_remote_ts is not None else None,
                "status": remote_status,
            })

    remote_ids = {row.get("id") for row in conv_summaries if isinstance(row, dict)}
    lost_ids = [cid for cid in existing_order if cid not in remote_ids]
    for cid in lost_ids:
        conv_summaries.append(_build_lost_summary(cid, existing_index.get(cid)))
    if lost_ids:
        print(f"  [lost] 标记已丢失会话: {len(lost_ids)} 个")

    # 5. 写入账号结构文件
    account_info["conversationCount"] = stats["success"]
    account_info["lastSyncAt"] = now_iso
    if stats["failed"] == 0:
        account_info["lastSyncResult"] = "success"
    elif stats["success"] > 0:
        account_info["lastSyncResult"] = "partial"
    else:
        account_info["lastSyncResult"] = "failed"

    _write_accounts_json(base_dir, account_info)
    _write_account_meta(account_dir, account_info)
    _write_conversations_index(account_dir, account_id, now_iso, conv_summaries)
    _write_sync_state(account_dir, {
        "version": 1,
        "accountId": account_id,
        "updatedAt": now_iso,
        "requestState": exporter._current_request_state(now_iso),
        "concurrency": 3,
        "fullSync": {
            "phase": "done",
            "startedAt": now_iso,
            "listingCursor": None,
            "listingTotal": total,
            "listingFetched": total,
            "conversationsToFetch": [],
            "conversationsFetched": stats["success"],
            "conversationsFailed": failed_ids,
            "completedAt": now_iso,
            "errorMessage": None,
        },
        "pendingConversations": [],
    })

    # 6. 输出统计
    print(f"\n{'=' * 50}")
    print(f"导出完成!")
    print(f"  账号: {mask_email(account_info['email']) or account_id}")
    print(f"  成功: {stats['success']}/{total}")
    print(f"  失败: {stats['failed']}/{total}")
    print(f"  媒体下载: {stats['media_downloaded']}")
    print(f"  媒体失败: {stats['media_failed']}")
    print(f"  视频预览生成: {stats['preview_generated']}")
    print(f"  视频预览失败: {stats['preview_failed']}")
    print(f"  输出目录: {account_dir.absolute()}")



def export_incremental(exporter, output_dir=None):
    """
    增量导出：
    - 按聊天列表新到旧扫描
    - 命中第一个未更新会话后停止继续下探
    - 对更新会话仅抓取新增 turn（遇到本地已存在 turn_id 即停止）
    调用前需先完成 init_auth（由上层统一初始化）。
    """
    now_iso = datetime.datetime.now(datetime.UTC).isoformat()
    base_dir = Path(output_dir) if output_dir else OUTPUT_DIR
    base_dir.mkdir(parents=True, exist_ok=True)

    # 解析账号信息
    account_info = exporter._resolve_account_info()
    account_id = account_info["id"]
    account_dir = base_dir / "accounts" / account_id
    conv_dir = account_dir / "conversations"
    media_dir = account_dir / "media"

    account_dir.mkdir(parents=True, exist_ok=True)
    conv_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)
    exporter._set_request_state_scope(account_dir)

    print(f"[*] 账号: {mask_email(account_info['email']) or account_id}")

    global_seen_urls = _load_media_manifest_new(account_dir)
    global_used_names = set(global_seen_urls.values())
    for f in media_dir.iterdir():
        if f.is_file():
            global_used_names.add(f.name)

    stats = {
        "updated": 0,
        "checked": 0,
        "media_downloaded": 0,
        "media_failed": 0,
        "preview_generated": 0,
        "preview_failed": 0,
    }
    stop_chat = None

    # 读取现有 conversations.json 构建索引
    existing_order, existing_index = _load_conversations_index(account_dir)
    conv_index = dict(existing_index)

    chats_to_update = []
    scanned_order = []
    scanned_seen = set()
    listing_cursor = None
    listing_exhausted = False

    while True:
        items, next_cursor = exporter.get_chats_page(listing_cursor)
        if not items and not next_cursor:
            listing_exhausted = True
            break

        for chat in items:
            conv_id = chat.get("id")
            if not isinstance(conv_id, str) or not conv_id:
                continue
            bare_id = conv_id.replace("c_", "")
            stats["checked"] += 1

            if bare_id not in scanned_seen:
                scanned_seen.add(bare_id)
                scanned_order.append(bare_id)

            conv_index[bare_id] = _build_summary_from_chat_listing(
                chat,
                conv_index.get(bare_id),
            )

            local_summary = existing_index.get(bare_id)
            local_updated_ts = _summary_to_epoch_seconds(local_summary)
            remote_latest_ts = chat.get("latest_update_ts")

            # 命中首个未更新会话，停止列表继续下探：
            # 当前会话及其后续更旧会话均视为未更新。
            if (
                isinstance(remote_latest_ts, int)
                and local_updated_ts is not None
                and int(remote_latest_ts) == int(local_updated_ts)
            ):
                stop_chat = conv_id
                print(f"[*] 命中未更新会话，停止列表扫描: {conv_id}")
                break

            needs_detail_sync = True
            if (
                isinstance(remote_latest_ts, int)
                and local_updated_ts is not None
                and int(remote_latest_ts) <= int(local_updated_ts)
            ):
                needs_detail_sync = False

            if needs_detail_sync:
                chats_to_update.append(chat)
            else:
                print(f"  [skip] 无远端更新，跳过详情拉取: {conv_id}")

        if stop_chat:
            break

        if not next_cursor:
            listing_exhausted = True
            break
        listing_cursor = next_cursor

    if listing_exhausted:
        account_info["remoteConversationCount"] = len(scanned_seen)
    elif not isinstance(account_info.get("remoteConversationCount"), int):
        account_info["remoteConversationCount"] = len(existing_order)

    total_to_update = len(chats_to_update)
    for idx, chat in enumerate(chats_to_update, start=1):
        conv_id = chat["id"]
        bare_id = conv_id.replace("c_", "")
        title = chat.get("title", "")
        jsonl_file = conv_dir / f"{bare_id}.jsonl"

        print(f"\n[{idx}/{total_to_update}] 增量检查: {title} ({conv_id})")
        existing_ids = _build_existing_turn_id_set_new(jsonl_file)
        raw_new_turns = exporter.get_chat_detail_incremental(conv_id, existing_ids)
        raw_new_turns, removed_turns = _dedupe_raw_turns_by_id(raw_new_turns)

        if not raw_new_turns:
            print("  无新增 turn")
            continue
        if removed_turns > 0:
            print(f"  [dedupe] 增量抓取结果去重: {removed_turns} 个重复 turn")

        parsed_new_turns = [parse_turn(turn) for turn in raw_new_turns]
        batch_list = exporter._assign_media_ids_and_collect_downloads(
            parsed_new_turns, media_dir, global_seen_urls, global_used_names,
        )

        # 读取现有 message 行（跳过 meta 首行）
        existing_msg_rows = []
        if jsonl_file.exists():
            with open(jsonl_file, "r", encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    if i == 0:
                        continue  # 跳过 meta
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        existing_msg_rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        # 将新 turns 转为 message 行（含新 meta）
        new_rows_full = _turns_to_jsonl_rows(parsed_new_turns, conv_id, account_id, title, chat)
        new_meta = new_rows_full[0]
        new_msg_rows = new_rows_full[1:]

        # 合并：新消息（正序）在前，旧消息跟随
        merged_msg_rows, removed_msg_rows = _merge_message_rows_for_write(
            new_msg_rows, existing_msg_rows
        )
        if removed_msg_rows > 0:
            print(f"  [dedupe] 增量合并写盘去重: {removed_msg_rows} 行")
        all_rows = [new_meta] + merged_msg_rows
        _write_jsonl_rows(jsonl_file, all_rows)

        failed_items = []
        if batch_list:
            failed_items = exporter.download_media_batch(batch_list, media_dir, stats)
            _save_media_manifest_new(account_dir, global_seen_urls)
        preview_stats = _ensure_video_previews_from_turns(parsed_new_turns, media_dir)
        stats["preview_generated"] += preview_stats["preview_generated"]
        stats["preview_failed"] += preview_stats["preview_failed"]

        batch_media_ids = {item.get("media_id") or item["filepath"].name for item in batch_list}
        failed_map = {item["media_id"]: (item.get("error") or "download_failed") for item in failed_items}
        recovered_ids = batch_media_ids - set(failed_map.keys())
        flag_stats = _update_jsonl_media_failure_flags(jsonl_file, failed_map, recovered_ids)
        if flag_stats["marked"] > 0 or flag_stats["cleared"] > 0:
            print(
                "  [media-flag] 已更新附件下载标记:"
                f" marked={flag_stats['marked']},"
                f" cleared={flag_stats['cleared']}"
            )

        # 更新 conv_index
        rows_after = _read_jsonl_rows(jsonl_file)
        meta_row = (
            next(
                (r for r in rows_after if isinstance(r, dict) and r.get("type") == "meta"),
                None,
            )
            or new_meta
        )
        all_msg_rows = [
            r for r in rows_after if isinstance(r, dict) and r.get("type") == "message"
        ]
        has_media = any(r.get("attachments") for r in all_msg_rows)
        has_failed_data = _rows_has_failed_data(all_msg_rows)
        image_count, video_count, _audio_count = _count_media_types_from_rows(all_msg_rows)
        last_text = ""
        for r in reversed(all_msg_rows):
            if r.get("text"):
                last_text = r["text"][:80]
                break

        conv_index[bare_id] = {
            "id": bare_id,
            "title": title,
            "lastMessage": last_text,
            "messageCount": len(all_msg_rows),
            "hasMedia": has_media,
            "hasFailedData": has_failed_data,
            "imageCount": image_count,
            "videoCount": video_count,
            "updatedAt": meta_row.get("updatedAt"),
            "remoteHash": meta_row.get("remoteHash"),
            "status": _status_for_remote_summary(conv_index.get(bare_id)),
        }

        print(f"  新增 turn: {len(parsed_new_turns)}")
        stats["updated"] += 1

    # 构建会话摘要输出顺序：
    # 1) 已扫描到的远端顺序（最新 -> 较旧）
    # 2) 若提前停止，未扫描的本地会话保持原顺序追加
    # 3) 若完整扫描，则可安全标记丢失会话
    summaries = []
    seen_ids = set()
    for bare_id in scanned_order:
        if bare_id in seen_ids:
            continue
        seen_ids.add(bare_id)
        summary = conv_index.get(bare_id)
        if isinstance(summary, dict):
            summaries.append(summary)

    if listing_exhausted:
        lost_ids = [cid for cid in existing_order if cid not in seen_ids]
        for cid in lost_ids:
            summaries.append(_build_lost_summary(cid, existing_index.get(cid)))
            seen_ids.add(cid)
        if lost_ids:
            print(f"  [lost] 标记已丢失会话: {len(lost_ids)} 个")
    else:
        for cid in existing_order:
            if cid in seen_ids:
                continue
            summary = conv_index.get(cid)
            if isinstance(summary, dict):
                summaries.append(summary)
                seen_ids.add(cid)

    for cid, summary in conv_index.items():
        if cid in seen_ids or not isinstance(summary, dict):
            continue
        summaries.append(summary)
        seen_ids.add(cid)

    # 写入账号结构文件
    account_info["conversationCount"] = len(summaries)
    account_info["lastSyncAt"] = now_iso
    account_info["lastSyncResult"] = "success"
    _write_accounts_json(base_dir, account_info)
    _write_account_meta(account_dir, account_info)
    _write_conversations_index(account_dir, account_id, now_iso, summaries)
    _write_sync_state(account_dir, {
        "version": 1,
        "accountId": account_id,
        "updatedAt": now_iso,
        "requestState": exporter._current_request_state(now_iso),
        "concurrency": 3,
        "fullSync": None,
        "pendingConversations": [],
    })

    print(f"\n{'=' * 50}")
    print("增量导出完成")
    print(f"  账号: {mask_email(account_info['email']) or account_id}")
    print(f"  检查会话: {stats['checked']}")
    print(f"  更新会话: {stats['updated']}")
    print(f"  停止位置: {stop_chat}")
    print(f"  媒体下载: {stats['media_downloaded']}")
    print(f"  媒体失败: {stats['media_failed']}")
    print(f"  视频预览生成: {stats['preview_generated']}")
    print(f"  视频预览失败: {stats['preview_failed']}")
    print(f"  输出目录: {account_dir.absolute()}")


# ============================================================================
# 入口
# ============================================================================


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Gemini 全量聊天导出工具")
    parser.add_argument("--cookies-file", help="Cookie JSON 文件路径")
    parser.add_argument("--output", "-o", default="gemini_export_output", help="输出根目录（内部自动创建 accounts/{id}/ 子目录）")
    parser.add_argument("--chat-ids", nargs="+", help="仅导出指定的对话 ID")
    parser.add_argument("--list-only", action="store_true", help="仅测试获取聊天列表")
    parser.add_argument("--list-users", action="store_true", help="列出本地账号邮箱与 authuser 映射")
    parser.add_argument("--user", help="指定 Google 账号（支持 0/1/2 或邮箱）")
    parser.add_argument("--account-id", help="强制写入指定账号目录 ID（用于 GUI 侧绑定）")
    parser.add_argument("--account-email", help="账号邮箱提示（用于写入 meta，不影响请求）")
    parser.add_argument("--check-chat-id", help="检查指定对话是否更新（需配合 --last-update-ts）")
    parser.add_argument("--last-update-ts", type=int, help="上次记录的对话更新时间（秒级时间戳）")
    parser.add_argument("--incremental", action="store_true", help="增量更新导出（命中首个未更新会话后停止）")
    parser.add_argument("--sync-list-only", action="store_true", help="仅同步会话列表（支持分页断点续传）")
    parser.add_argument("--sync-conversation", action="store_true", help="仅同步单个会话详情")
    parser.add_argument("--conversation-id", help="会话 ID（支持 bare id 或 c_xxx）")
    parser.add_argument("--accounts-only", action="store_true", help="仅导入账号信息并写入本地，不拉取对话")
    args = parser.parse_args()

    # 1. 获取 cookies
    cookies = None

    if args.cookies_file:
        # 从文件读取
        print(f"[*] 从文件加载 cookies: {args.cookies_file}")
        with open(args.cookies_file) as f:
            cookies = json.load(f)
    else:
        # 直接从本机浏览器读取 cookies
        cookies = get_cookies_from_local_browser()

    # 验证关键 cookies
    key_cookies = ["__Secure-1PSID", "__Secure-1PSIDTS"]
    found = [k for k in key_cookies if k in cookies]
    if not found:
        print(f"[!] 未找到关键 cookie ({', '.join(key_cookies)})，可能未登录")
        print(f"    找到的 cookies: {list(cookies.keys())[:10]}")
        sys.exit(1)

    print(f"[*] 已提取 {len(cookies)} 个 cookies")

    # 2. 账号映射列表
    exporter = GeminiExporter(
        cookies,
        user=args.user,
        account_id=args.account_id,
        account_email=args.account_email,
    )
    if args.list_users:
        rows = exporter.list_user_options()
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    # 3. 导出 / 仅列表测试
    if args.check_chat_id:
        if args.last_update_ts is None:
            print("[!] 使用 --check-chat-id 时必须提供 --last-update-ts")
            sys.exit(1)
        try:
            exporter.init_auth()
        except Exception as e:
            print(f"[!] cookies 鉴权失败: {e}")
            print("    请确认浏览器已登录 Gemini，或使用 --cookies-file 提供可用 cookie")
            sys.exit(1)
        check_result = exporter.is_chat_updated(
            normalize_chat_id(args.check_chat_id),
            args.last_update_ts,
        )
        print(json.dumps(check_result, ensure_ascii=False, indent=2))
        return

    if args.list_only:
        try:
            exporter.init_auth()
        except Exception as e:
            print(f"[!] cookies 鉴权失败: {e}")
            print("    请确认浏览器已登录 Gemini，或使用 --cookies-file 提供可用 cookie")
            sys.exit(1)
        chats = exporter.get_all_chats()
        print(f"[*] 聊天列表获取完成: {len(chats)} 个")
        return

    if args.accounts_only:
        base_dir = Path(args.output)
        base_dir.mkdir(parents=True, exist_ok=True)

        def _normalize_authuser(v):
            if v is None:
                return None
            s = str(v).strip()
            return s if s.isdigit() else None

        def _build_account_info_from_hint(email_hint, authuser_hint):
            email = (email_hint or "").strip().lower()
            authuser_str = _normalize_authuser(authuser_hint)
            if not email:
                return None
            name = email.split("@")[0]
            account_id = email_to_account_id(email)
            return {
                "id": account_id,
                "email": email,
                "name": name,
                "avatarText": name[0].upper() if name else "?",
                "avatarColor": "#667eea",
                "conversationCount": 0,
                "remoteConversationCount": None,
                "lastSyncAt": None,
                "lastSyncResult": None,
                "authuser": authuser_str,
            }

        def _persist_account(account_info):
            account_id = account_info["id"]
            account_dir = base_dir / "accounts" / account_id
            account_dir.mkdir(parents=True, exist_ok=True)
            (account_dir / "conversations").mkdir(exist_ok=True)
            (account_dir / "media").mkdir(exist_ok=True)
            _write_accounts_json(base_dir, account_info)
            _write_account_meta(account_dir, account_info)
            return account_info

        try:
            mappings = discover_email_authuser_mapping(cookies)
        except Exception as e:
            print(json.dumps({
                "status": "failed",
                "imported": [],
                "failed": [{"user": "mapping", "error": str(e)}],
            }, ensure_ascii=False))
            return

        imported_ids = []
        failed = []
        seen_ids = set()

        if args.user:
            user_spec = str(args.user).strip().lower()
            target = None
            if user_spec.isdigit():
                target = next(
                    (m for m in mappings if _normalize_authuser(m.get("authuser")) == user_spec),
                    None,
                )
            else:
                target = next(
                    (m for m in mappings if (m.get("email") or "").strip().lower() == user_spec),
                    None,
                )

            if not target:
                failed.append({"user": args.user, "error": "账号不在 ListAccounts 结果中"})
            else:
                email = (target.get("email") or "").strip().lower()
                authuser = _normalize_authuser(target.get("authuser"))
                info = _build_account_info_from_hint(email, authuser)
                if info is None:
                    failed.append({"user": args.user, "error": "账号缺少有效 authuser"})
                else:
                    _persist_account(info)
                    imported_ids.append(info["id"])
            status = "ok" if imported_ids else "failed"
            result = {"status": status, "imported": imported_ids}
            if failed:
                result["failed"] = failed
            print(json.dumps(result, ensure_ascii=False))
            return

        for item in mappings:
            email = (item.get("email") or "").strip().lower()
            authuser = _normalize_authuser(item.get("authuser"))
            info = _build_account_info_from_hint(email, authuser)
            if info is None:
                failed.append({
                    "user": email or str(item.get("authuser") or ""),
                    "error": "账号缺少有效 authuser",
                })
                continue
            if info["id"] in seen_ids:
                continue
            seen_ids.add(info["id"])
            _persist_account(info)
            imported_ids.append(info["id"])

        status = "ok" if imported_ids else "failed"
        result = {"status": status, "imported": imported_ids}
        if failed:
            result["failed"] = failed
        print(json.dumps(result, ensure_ascii=False))
        return

    try:
        exporter.init_auth()
    except Exception as e:
        print(f"[!] cookies 鉴权失败: {e}")
        print("    请确认浏览器已登录 Gemini，或使用 --cookies-file 提供可用 cookie")
        sys.exit(1)

    if args.incremental:
        export_incremental(exporter, output_dir=args.output)
        return

    if args.sync_list_only:
        exporter.export_list_only(output_dir=args.output)
        return

    if args.sync_conversation:
        if not args.conversation_id:
            print("[!] 使用 --sync-conversation 时必须提供 --conversation-id")
            sys.exit(1)
        exporter.sync_single_conversation(args.conversation_id, output_dir=args.output)
        return

    export_all(exporter, output_dir=args.output, chat_ids=args.chat_ids)


if __name__ == "__main__":
    main()
