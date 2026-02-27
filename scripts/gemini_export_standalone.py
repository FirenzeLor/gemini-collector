#!/usr/bin/env python3
"""
Gemini 导出独立脚本（不依赖 App 进程，单用户模式）。

约束：
- 必须传入 --user（仅支持数字 authuser，如 0/1/4）
- 不提供账号映射查询与导入功能
- 不支持邮箱作为 user 输入

保留的接口能力：
- sync_list
- sync_conversation
- sync_full
- sync_incremental
- list_chats
- check_chat_updated
- export_all
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from gemini_export import GeminiExporter, get_cookies_from_local_browser


def normalize_user(user: str) -> str:
    val = str(user).strip()
    if not val.isdigit():
        raise RuntimeError("--user 仅支持数字 authuser（例如: 0/1/4）")
    return val


class StandaloneExporter:
    def __init__(self, output_dir: Path, user: str, cookies: Dict[str, str]) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.user = normalize_user(user)
        self.cookies = cookies
        self._exporter: Optional[GeminiExporter] = None

    @staticmethod
    def _is_auth_error(exc: BaseException) -> bool:
        text = str(exc).lower()
        keywords = [
            "csrf token",
            "snlm0e",
            "未登录",
            "auth",
            "cookies",
            "http 401",
            "http 403",
            "forbidden",
        ]
        return any(k in text for k in keywords)

    def _account_id(self) -> str:
        return f"user_{self.user}"

    def _account_dir(self) -> Path:
        return self.output_dir / "accounts" / self._account_id()

    def _get_exporter(self, force_refresh: bool = False) -> GeminiExporter:
        if force_refresh:
            self._exporter = None
        if self._exporter is not None:
            return self._exporter

        exporter = GeminiExporter(self.cookies, user=self.user)
        with contextlib.redirect_stdout(sys.stderr):
            exporter.init_auth()
        self._exporter = exporter
        return exporter

    def _run_with_auth_retry(self, fn: Callable[[GeminiExporter], Dict[str, Any]]) -> Dict[str, Any]:
        exporter = self._get_exporter(force_refresh=False)
        try:
            return fn(exporter)
        except Exception as exc:
            if not self._is_auth_error(exc):
                raise
            exporter = self._get_exporter(force_refresh=True)
            return fn(exporter)

    @staticmethod
    def _jsonl_has_failed_marker(jsonl_path: Path) -> bool:
        if not jsonl_path.exists():
            return False
        try:
            return '"downloadFailed": true' in jsonl_path.read_text(encoding="utf-8")
        except Exception:
            return False

    @staticmethod
    def _jsonl_has_message_rows(jsonl_path: Path) -> bool:
        if not jsonl_path.exists():
            return False
        try:
            with jsonl_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if '"type": "message"' in line:
                        return True
        except Exception:
            return False
        return False

    def _load_conversation_items(self) -> list[dict[str, Any]]:
        conv_index = self._account_dir() / "conversations.json"
        if not conv_index.exists():
            return []
        try:
            data = json.loads(conv_index.read_text(encoding="utf-8"))
        except Exception:
            return []
        items = data.get("items", []) if isinstance(data, dict) else []
        return [row for row in items if isinstance(row, dict)]

    def _load_conversation_ids(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for row in self._load_conversation_items():
            cid = row.get("id")
            if isinstance(cid, str) and cid.strip():
                normalized = cid.strip()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                out.append(normalized)
        return out

    def _collect_failed_conversation_ids(self) -> list[str]:
        conv_dir = self._account_dir() / "conversations"
        ids: set[str] = set()

        if conv_dir.exists():
            for jsonl_path in conv_dir.glob("*.jsonl"):
                if self._jsonl_has_failed_marker(jsonl_path):
                    ids.add(jsonl_path.stem)
        return sorted(ids)

    def _collect_empty_conversation_ids(self) -> list[str]:
        out: list[str] = []
        for row in self._load_conversation_items():
            cid = row.get("id")
            if not isinstance(cid, str) or not cid.strip():
                continue
            cid = cid.strip()
            message_count = row.get("messageCount")
            if isinstance(message_count, int) and message_count == 0:
                out.append(cid)
        return out

    def sync_list(self) -> Dict[str, Any]:
        def run(exporter: GeminiExporter) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                exporter.export_list_only(output_dir=str(self.output_dir))
            total = len(self._load_conversation_ids())
            return {
                "status": "ok",
                "user": self.user,
                "accountId": self._account_id(),
                "total": total,
            }

        return self._run_with_auth_retry(run)

    def sync_conversation(self, conversation_id: str) -> Dict[str, Any]:
        if not conversation_id or not str(conversation_id).strip():
            raise RuntimeError("sync_conversation 需要 conversation_id")
        conversation_id = str(conversation_id).strip()

        def run(exporter: GeminiExporter) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                exporter.sync_single_conversation(conversation_id, output_dir=str(self.output_dir))
            return {
                "status": "ok",
                "user": self.user,
                "accountId": self._account_id(),
                "conversationId": conversation_id,
            }

        return self._run_with_auth_retry(run)

    def sync_incremental(self) -> Dict[str, Any]:
        def run(exporter: GeminiExporter) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                exporter.export_incremental(output_dir=str(self.output_dir))
            return {
                "status": "ok",
                "user": self.user,
                "accountId": self._account_id(),
            }

        return self._run_with_auth_retry(run)

    def list_chats(self) -> Dict[str, Any]:
        def run(exporter: GeminiExporter) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                chats = exporter.get_all_chats()
            return {
                "status": "ok",
                "user": self.user,
                "accountId": self._account_id(),
                "count": len(chats),
                "items": chats,
            }

        return self._run_with_auth_retry(run)

    def check_chat_updated(self, conversation_id: str, last_update_ts: int) -> Dict[str, Any]:
        if not conversation_id or not str(conversation_id).strip():
            raise RuntimeError("check_chat_updated 需要 conversation_id")
        norm_id = GeminiExporter.normalize_chat_id(conversation_id)

        def run(exporter: GeminiExporter) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                result = exporter.is_chat_updated(norm_id, int(last_update_ts))
            if isinstance(result, dict):
                result.setdefault("status", "ok")
                result.setdefault("user", self.user)
                result.setdefault("accountId", self._account_id())
                return result
            return {
                "status": "ok",
                "user": self.user,
                "accountId": self._account_id(),
                "result": result,
            }

        return self._run_with_auth_retry(run)

    def export_all(self, chat_ids: Optional[list[str]] = None) -> Dict[str, Any]:
        def run(exporter: GeminiExporter) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                exporter.export_all(output_dir=str(self.output_dir), chat_ids=chat_ids)
            return {
                "status": "ok",
                "user": self.user,
                "accountId": self._account_id(),
            }

        return self._run_with_auth_retry(run)

    def sync_full(self) -> Dict[str, Any]:
        success_ids: set[str] = set()
        failed_ids: set[str] = set()

        def _sync_batch_with_progress(label: str, conversation_ids: list[str]) -> None:
            total = len(conversation_ids)
            print(
                f"[sync_full:{label}] 当前对话任务列表更新进度: 0/{total}",
                file=sys.stderr,
                flush=True,
            )
            for idx, cid in enumerate(conversation_ids, start=1):
                try:
                    self.sync_conversation(cid)
                    success_ids.add(cid)
                    failed_ids.discard(cid)
                except Exception:
                    failed_ids.add(cid)
                print(
                    f"[sync_full:{label}] 当前对话任务列表更新进度: {idx}/{total}"
                    f" (success={len(success_ids)}, failed={len(failed_ids)}, cid={cid})",
                    file=sys.stderr,
                    flush=True,
                )

        # 1) 失败记录重试
        retry_failed_ids = self._collect_failed_conversation_ids()
        _sync_batch_with_progress("retry_failed", retry_failed_ids)

        # 2) 补齐 messageCount=0 的空会话
        empty_ids = [cid for cid in self._collect_empty_conversation_ids() if cid not in success_ids]
        _sync_batch_with_progress("sync_empty", empty_ids)

        # 3) 拉最新列表后同步新增
        before_ids = self._load_conversation_ids()
        before_set = set(before_ids)
        self.sync_list()
        after_ids = self._load_conversation_ids()

        new_ids: list[str] = []
        new_seen: set[str] = set()
        for cid in after_ids:
            if cid in before_set or cid in success_ids or cid in new_seen:
                continue
            new_seen.add(cid)
            new_ids.append(cid)
        _sync_batch_with_progress("sync_new", new_ids)

        # 4) 检查剩余老会话更新
        remaining_old_ids: list[str] = []
        old_seen: set[str] = set()
        for cid in after_ids:
            if cid not in before_set:
                continue
            if cid in success_ids or cid in old_seen:
                continue
            old_seen.add(cid)
            remaining_old_ids.append(cid)
        _sync_batch_with_progress("sync_old", remaining_old_ids)

        return {
            "status": "ok",
            "user": self.user,
            "accountId": self._account_id(),
            "total": len(after_ids),
            "failed": len(failed_ids),
            "failedConversationIds": sorted(failed_ids),
        }


def load_cookies(cookies_file: Optional[str]) -> Dict[str, str]:
    if cookies_file:
        with open(cookies_file, "r", encoding="utf-8") as f:
            cookies = json.load(f)
    else:
        with contextlib.redirect_stdout(sys.stderr):
            cookies = get_cookies_from_local_browser()

    key_cookies = ["__Secure-1PSID", "__Secure-1PSIDTS"]
    found = [k for k in key_cookies if k in cookies]
    if not found:
        raise RuntimeError(f"未找到关键 cookie ({', '.join(key_cookies)})")
    return cookies


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gemini 导出独立脚本（单用户模式）")
    parser.add_argument("--output-dir", "-o", required=True, help="输出目录（会创建 accounts/ 等文件）")
    parser.add_argument("--cookies-file", help="Cookie JSON 文件路径，不传则读取本机浏览器")
    parser.add_argument("--user", required=True, help="数字 authuser（例如: 0/1/4）")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync-list", aliases=["sync_list"], help="仅同步会话列表")

    p_sync_conv = sub.add_parser("sync-conversation", aliases=["sync_conversation"], help="同步单会话")
    p_sync_conv.add_argument("--conversation-id", required=True, help="会话 ID（bare 或 c_xxx）")

    sub.add_parser("sync-full", aliases=["sync_full"], help="全量同步（与 worker 编排一致）")

    sub.add_parser("sync-incremental", aliases=["sync_incremental"], help="增量同步")

    sub.add_parser("list-chats", aliases=["list_chats"], help="仅获取聊天列表（不落盘详情）")

    p_check = sub.add_parser("check-chat-updated", aliases=["check_chat_updated"], help="检查会话是否有更新")
    p_check.add_argument("--conversation-id", required=True, help="会话 ID（bare 或 c_xxx）")
    p_check.add_argument("--last-update-ts", required=True, type=int, help="上次记录的更新时间戳（秒）")

    p_export_all = sub.add_parser("export-all", aliases=["export_all"], help="执行导出全量流程")
    p_export_all.add_argument("--chat-ids", nargs="+", help="仅导出指定会话 ID")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    user = normalize_user(args.user)
    output_dir = Path(args.output_dir).expanduser().resolve()
    cookies = load_cookies(args.cookies_file)
    api = StandaloneExporter(output_dir=output_dir, user=user, cookies=cookies)

    cmd = args.command
    if cmd in {"sync-list", "sync_list"}:
        result = api.sync_list()
    elif cmd in {"sync-conversation", "sync_conversation"}:
        result = api.sync_conversation(conversation_id=args.conversation_id)
    elif cmd in {"sync-full", "sync_full"}:
        result = api.sync_full()
    elif cmd in {"sync-incremental", "sync_incremental"}:
        result = api.sync_incremental()
    elif cmd in {"list-chats", "list_chats"}:
        result = api.list_chats()
    elif cmd in {"check-chat-updated", "check_chat_updated"}:
        result = api.check_chat_updated(
            conversation_id=args.conversation_id,
            last_update_ts=args.last_update_ts,
        )
    elif cmd in {"export-all", "export_all"}:
        result = api.export_all(chat_ids=getattr(args, "chat_ids", None))
    else:
        raise RuntimeError(f"未知命令: {cmd}")

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
