#!/usr/bin/env python3
"""
Gemini 导出独立脚本（不依赖 App 进程）。

复用现有 gemini_export.py 的导出能力，并提供与当前 worker 一致的核心接口：
- list_users
- import_accounts
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
from typing import Any, Callable, Dict, Optional, Tuple

from gemini_export import (
    GeminiExporter,
    discover_email_authuser_mapping,
    get_cookies_from_local_browser,
)


class StandaloneExporter:
    def __init__(self, output_dir: Path, cookies: Dict[str, str]) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cookies = cookies
        self._exporters: Dict[str, GeminiExporter] = {}

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

    def _read_account_mapping(self, account_id: str) -> Tuple[Optional[str], Optional[str]]:
        accounts_file = self.output_dir / "accounts.json"
        if not accounts_file.exists():
            raise RuntimeError("accounts.json 不存在，请先执行 import_accounts")

        data = json.loads(accounts_file.read_text(encoding="utf-8"))
        rows = data.get("accounts", [])
        if not isinstance(rows, list):
            raise RuntimeError("accounts.json 格式错误：accounts 不是数组")

        match = None
        for item in rows:
            if isinstance(item, dict) and item.get("id") == account_id:
                match = item
                break
        if match is None:
            raise RuntimeError(f"未找到账号映射: {account_id}")

        authuser = match.get("authuser")
        email = match.get("email")
        authuser_str = str(authuser).strip() if authuser is not None else None
        if authuser_str == "":
            authuser_str = None
        email_str = str(email).strip().lower() if email is not None else None
        if email_str == "":
            email_str = None
        return authuser_str, email_str

    @staticmethod
    def _normalize_authuser(v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s if s.isdigit() else None

    def _exporter_key(self, account_id: str, authuser: Optional[str]) -> str:
        return f"{account_id}:{authuser or ''}"

    def _get_exporter(self, account_id: str, force_refresh: bool = False) -> GeminiExporter:
        authuser, email = self._read_account_mapping(account_id)
        cache_key = self._exporter_key(account_id, authuser)
        if force_refresh:
            self._exporters.pop(cache_key, None)

        exporter = self._exporters.get(cache_key)
        if exporter is not None:
            return exporter

        exporter = GeminiExporter(
            self.cookies,
            user=authuser,
            account_id=account_id,
            account_email=email,
        )
        with contextlib.redirect_stdout(sys.stderr):
            exporter.init_auth()
        self._exporters[cache_key] = exporter
        return exporter

    def _run_with_auth_retry(self, account_id: str, fn: Callable[[GeminiExporter], Dict[str, Any]]) -> Dict[str, Any]:
        exporter = self._get_exporter(account_id, force_refresh=False)
        try:
            return fn(exporter)
        except Exception as exc:
            if not self._is_auth_error(exc):
                raise
            exporter = self._get_exporter(account_id, force_refresh=True)
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

    def _load_conversation_items(self, account_id: str) -> list[dict[str, Any]]:
        conv_index = self.output_dir / "accounts" / account_id / "conversations.json"
        if not conv_index.exists():
            return []
        try:
            data = json.loads(conv_index.read_text(encoding="utf-8"))
        except Exception:
            return []
        items = data.get("items", []) if isinstance(data, dict) else []
        return [row for row in items if isinstance(row, dict)]

    def _load_conversation_ids(self, account_id: str) -> list[str]:
        out: list[str] = []
        for row in self._load_conversation_items(account_id):
            cid = row.get("id")
            if isinstance(cid, str) and cid.strip():
                out.append(cid.strip())
        return out

    def _collect_failed_conversation_ids(self, account_id: str) -> list[str]:
        account_dir = self.output_dir / "accounts" / account_id
        conv_dir = account_dir / "conversations"
        ids: set[str] = set()

        sync_state_file = account_dir / "sync_state.json"
        if sync_state_file.exists():
            try:
                sync_state = json.loads(sync_state_file.read_text(encoding="utf-8"))
            except Exception:
                sync_state = {}
            full_sync = sync_state.get("fullSync") if isinstance(sync_state, dict) else None
            failed = full_sync.get("conversationsFailed") if isinstance(full_sync, dict) else []
            if isinstance(failed, list):
                for cid in failed:
                    if isinstance(cid, str) and cid.strip():
                        ids.add(cid.strip())

        if conv_dir.exists():
            for jsonl_path in conv_dir.glob("*.jsonl"):
                if self._jsonl_has_failed_marker(jsonl_path):
                    ids.add(jsonl_path.stem)
        return sorted(ids)

    def _collect_empty_conversation_ids(self, account_id: str) -> list[str]:
        account_dir = self.output_dir / "accounts" / account_id
        conv_dir = account_dir / "conversations"
        out: list[str] = []
        for row in self._load_conversation_items(account_id):
            cid = row.get("id")
            if not isinstance(cid, str) or not cid.strip():
                continue
            cid = cid.strip()
            jsonl_path = conv_dir / f"{cid}.jsonl"
            synced_at = row.get("syncedAt")
            message_count = row.get("messageCount")
            index_empty = synced_at is None or message_count == 0
            local_empty = not self._jsonl_has_message_rows(jsonl_path)
            if index_empty or local_empty:
                out.append(cid)
        return out

    def list_users(self) -> Dict[str, Any]:
        with contextlib.redirect_stdout(sys.stderr):
            mappings = discover_email_authuser_mapping(self.cookies)
        return {"status": "ok", "items": mappings}

    def import_accounts(self, user: Optional[str] = None) -> Dict[str, Any]:
        with contextlib.redirect_stdout(sys.stderr):
            mappings = discover_email_authuser_mapping(self.cookies)

        def _build_account_info_from_hint(email_hint: Optional[str], authuser_hint: Optional[str]) -> Optional[Dict[str, Any]]:
            email = (email_hint or "").strip().lower()
            authuser_str = self._normalize_authuser(authuser_hint)
            if not email:
                return None
            name = email.split("@")[0]
            account_id = GeminiExporter.email_to_account_id(email)
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

        def _persist_account(account_info: Dict[str, Any]) -> Dict[str, Any]:
            account_id = account_info["id"]
            account_dir = self.output_dir / "accounts" / account_id
            account_dir.mkdir(parents=True, exist_ok=True)
            (account_dir / "conversations").mkdir(exist_ok=True)
            (account_dir / "media").mkdir(exist_ok=True)
            GeminiExporter._write_accounts_json(self.output_dir, account_info)
            GeminiExporter._write_account_meta(account_dir, account_info)
            return account_info

        imported_ids: list[str] = []
        failed: list[Dict[str, Any]] = []
        seen_ids: set[str] = set()

        if user:
            user_spec = str(user).strip().lower()
            if user_spec.isdigit():
                target = next((m for m in mappings if self._normalize_authuser(m.get("authuser")) == user_spec), None)
            else:
                target = next((m for m in mappings if (m.get("email") or "").strip().lower() == user_spec), None)
            if not target:
                failed.append({"user": user, "error": "账号不在 ListAccounts 结果中"})
            else:
                email = (target.get("email") or "").strip().lower()
                authuser = self._normalize_authuser(target.get("authuser"))
                info = _build_account_info_from_hint(email, authuser)
                if info is None:
                    failed.append({"user": user, "error": "账号缺少有效 authuser"})
                else:
                    _persist_account(info)
                    imported_ids.append(info["id"])
            status = "ok" if imported_ids else "failed"
            result: Dict[str, Any] = {"status": status, "imported": imported_ids}
            if failed:
                result["failed"] = failed
            return result

        for item in mappings:
            email = (item.get("email") or "").strip().lower()
            authuser = self._normalize_authuser(item.get("authuser"))
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
        return result

    def sync_list(self, account_id: str) -> Dict[str, Any]:
        def run(exporter: GeminiExporter) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                exporter.export_list_only(output_dir=str(self.output_dir))
            total = len(self._load_conversation_ids(account_id))
            return {"status": "ok", "total": total}
        return self._run_with_auth_retry(account_id, run)

    def sync_conversation(self, account_id: str, conversation_id: str) -> Dict[str, Any]:
        if not conversation_id or not str(conversation_id).strip():
            raise RuntimeError("sync_conversation 需要 conversation_id")
        conversation_id = str(conversation_id).strip()

        def run(exporter: GeminiExporter) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                exporter.sync_single_conversation(conversation_id, output_dir=str(self.output_dir))
            return {"status": "ok", "conversationId": conversation_id}
        return self._run_with_auth_retry(account_id, run)

    def sync_incremental(self, account_id: str) -> Dict[str, Any]:
        def run(exporter: GeminiExporter) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                exporter.export_incremental(output_dir=str(self.output_dir))
            return {"status": "ok"}
        return self._run_with_auth_retry(account_id, run)

    def list_chats(self, account_id: str) -> Dict[str, Any]:
        def run(exporter: GeminiExporter) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                chats = exporter.get_all_chats()
            return {"status": "ok", "count": len(chats), "items": chats}
        return self._run_with_auth_retry(account_id, run)

    def check_chat_updated(self, account_id: str, conversation_id: str, last_update_ts: int) -> Dict[str, Any]:
        if not conversation_id or not str(conversation_id).strip():
            raise RuntimeError("check_chat_updated 需要 conversation_id")
        norm_id = GeminiExporter.normalize_chat_id(conversation_id)

        def run(exporter: GeminiExporter) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                result = exporter.is_chat_updated(norm_id, int(last_update_ts))
            if isinstance(result, dict):
                result.setdefault("status", "ok")
                return result
            return {"status": "ok", "result": result}
        return self._run_with_auth_retry(account_id, run)

    def export_all(self, account_id: str, chat_ids: Optional[list[str]] = None) -> Dict[str, Any]:
        def run(exporter: GeminiExporter) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                exporter.export_all(output_dir=str(self.output_dir), chat_ids=chat_ids)
            return {"status": "ok"}
        return self._run_with_auth_retry(account_id, run)

    def sync_full(self, account_id: str) -> Dict[str, Any]:
        success_ids: set[str] = set()
        failed_ids: set[str] = set()

        retry_failed_ids = self._collect_failed_conversation_ids(account_id)
        for cid in retry_failed_ids:
            try:
                self.sync_conversation(account_id, cid)
                success_ids.add(cid)
                failed_ids.discard(cid)
            except Exception:
                failed_ids.add(cid)

        empty_ids = [cid for cid in self._collect_empty_conversation_ids(account_id) if cid not in success_ids]
        for cid in empty_ids:
            try:
                self.sync_conversation(account_id, cid)
                success_ids.add(cid)
                failed_ids.discard(cid)
            except Exception:
                failed_ids.add(cid)

        before_ids = self._load_conversation_ids(account_id)
        self.sync_list(account_id)
        after_ids = self._load_conversation_ids(account_id)
        before_set = set(before_ids)

        new_ids = [cid for cid in after_ids if cid not in before_set and cid not in success_ids]
        for cid in new_ids:
            try:
                self.sync_conversation(account_id, cid)
                success_ids.add(cid)
                failed_ids.discard(cid)
            except Exception:
                failed_ids.add(cid)

        remaining_old_ids = [cid for cid in after_ids if cid not in success_ids]
        for cid in remaining_old_ids:
            try:
                self.sync_conversation(account_id, cid)
                success_ids.add(cid)
                failed_ids.discard(cid)
            except Exception:
                failed_ids.add(cid)

        return {
            "status": "ok",
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
    parser = argparse.ArgumentParser(description="Gemini 导出独立脚本")
    parser.add_argument("--output-dir", "-o", required=True, help="输出目录（会创建 accounts/ 等文件）")
    parser.add_argument("--cookies-file", help="Cookie JSON 文件路径，不传则读取本机浏览器")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-users", aliases=["list_users"], help="列出本地账号邮箱与 authuser 映射")

    p_import = sub.add_parser("import-accounts", aliases=["import_accounts"], help="导入账号映射")
    p_import.add_argument("--user", help="仅导入单个用户（authuser 或 email）")

    p_sync_list = sub.add_parser("sync-list", aliases=["sync_list"], help="仅同步会话列表")
    p_sync_list.add_argument("--account-id", required=True, help="账号 ID")

    p_sync_conv = sub.add_parser("sync-conversation", aliases=["sync_conversation"], help="同步单会话")
    p_sync_conv.add_argument("--account-id", required=True, help="账号 ID")
    p_sync_conv.add_argument("--conversation-id", required=True, help="会话 ID（bare 或 c_xxx）")

    p_sync_full = sub.add_parser("sync-full", aliases=["sync_full"], help="全量同步（与 worker 编排一致）")
    p_sync_full.add_argument("--account-id", required=True, help="账号 ID")

    p_sync_inc = sub.add_parser("sync-incremental", aliases=["sync_incremental"], help="增量同步")
    p_sync_inc.add_argument("--account-id", required=True, help="账号 ID")

    p_list_chats = sub.add_parser("list-chats", aliases=["list_chats"], help="仅获取聊天列表（不落盘详情）")
    p_list_chats.add_argument("--account-id", required=True, help="账号 ID")

    p_check = sub.add_parser("check-chat-updated", aliases=["check_chat_updated"], help="检查会话是否有更新")
    p_check.add_argument("--account-id", required=True, help="账号 ID")
    p_check.add_argument("--conversation-id", required=True, help="会话 ID（bare 或 c_xxx）")
    p_check.add_argument("--last-update-ts", required=True, type=int, help="上次记录的更新时间戳（秒）")

    p_export_all = sub.add_parser("export-all", aliases=["export_all"], help="执行导出全量流程")
    p_export_all.add_argument("--account-id", required=True, help="账号 ID")
    p_export_all.add_argument("--chat-ids", nargs="+", help="仅导出指定会话 ID")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    cookies = load_cookies(args.cookies_file)
    api = StandaloneExporter(output_dir=output_dir, cookies=cookies)

    cmd = args.command
    if cmd in {"list-users", "list_users"}:
        result = api.list_users()
    elif cmd in {"import-accounts", "import_accounts"}:
        result = api.import_accounts(user=getattr(args, "user", None))
    elif cmd in {"sync-list", "sync_list"}:
        result = api.sync_list(account_id=args.account_id)
    elif cmd in {"sync-conversation", "sync_conversation"}:
        result = api.sync_conversation(account_id=args.account_id, conversation_id=args.conversation_id)
    elif cmd in {"sync-full", "sync_full"}:
        result = api.sync_full(account_id=args.account_id)
    elif cmd in {"sync-incremental", "sync_incremental"}:
        result = api.sync_incremental(account_id=args.account_id)
    elif cmd in {"list-chats", "list_chats"}:
        result = api.list_chats(account_id=args.account_id)
    elif cmd in {"check-chat-updated", "check_chat_updated"}:
        result = api.check_chat_updated(
            account_id=args.account_id,
            conversation_id=args.conversation_id,
            last_update_ts=args.last_update_ts,
        )
    elif cmd in {"export-all", "export_all"}:
        result = api.export_all(account_id=args.account_id, chat_ids=getattr(args, "chat_ids", None))
    else:
        raise RuntimeError(f"未知命令: {cmd}")

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
