#!/usr/bin/env python3
"""
Gemini worker (stdio JSON-lines).

Protocol:
- request:  {"id":"req_1","method":"enqueue_job","params":{...}}
- response: {"id":"req_1","ok":true,"result":{...}}
- event:    {"event":"job_state","payload":{...}}
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import queue
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional


JOB_TYPES = {
    "sync_list",
    "sync_conversation",
    "sync_full",
    "sync_incremental",
}


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class GeminiWorker:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._stdout_lock = threading.Lock()
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._running = True
        self._cookies: Optional[Dict[str, str]] = None
        self._exporters: Dict[str, Any] = {}
        self._exporter_cls: Optional[Any] = None
        self._cookies_loader: Optional[Callable[[], Dict[str, str]]] = None
        self._backoff_limit_error_cls: Optional[type[BaseException]] = None
        self._session_expired_error_cls: Optional[type[BaseException]] = None

        self._account_map: Dict[str, Any] = {}

        self._worker_thread = threading.Thread(target=self._job_loop, daemon=True)
        self._worker_thread.start()

    def _send_line(self, payload: Dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        with self._stdout_lock:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def _send_response(self, req_id: str, ok: bool, result: Optional[Dict[str, Any]] = None, error: Optional[Dict[str, Any]] = None) -> None:
        msg: Dict[str, Any] = {"id": req_id, "ok": ok}
        if ok:
            msg["result"] = result or {}
        else:
            msg["error"] = error or {"code": "SCRIPT_ERROR", "message": "unknown error", "retryable": False}
        self._send_line(msg)

    def _send_event(self, event: str, payload: Dict[str, Any]) -> None:
        self._send_line({"event": event, "payload": payload})

    def _emit_job_state(
        self,
        job: Dict[str, Any],
        state: str,
        *,
        phase: Optional[str] = None,
        progress: Optional[Dict[str, int]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "jobId": job["jobId"],
            "state": state,
            "type": job["type"],
            "accountId": job["accountId"],
        }
        if job.get("conversationId"):
            payload["conversationId"] = job["conversationId"]
        if phase:
            payload["phase"] = phase
        if progress:
            payload["progress"] = progress
        if error:
            payload["error"] = error
        self._send_event("job_state", payload)

    def _is_backoff_limit_error(self, exc: BaseException) -> bool:
        cls = self._backoff_limit_error_cls
        if cls is not None and isinstance(exc, cls):
            return True
        text = str(exc).lower()
        return (
            "请求连续失败达到退避上限" in text
            or "触发全局兜底提前结束" in text
            or "request backoff limit" in text
        )

    def _to_error(self, exc: BaseException) -> Dict[str, Any]:
        if self._is_backoff_limit_error(exc):
            return {
                "code": "REQUEST_BACKOFF_LIMIT",
                "message": str(exc) or exc.__class__.__name__,
                "retryable": False,
            }
        return {
            "code": "SCRIPT_ERROR",
            "message": str(exc) or exc.__class__.__name__,
            "retryable": False,
        }

    def _ensure_export_api(self) -> None:
        if (
            self._exporter_cls is not None
            and self._cookies_loader is not None
            and self._backoff_limit_error_cls is not None
        ):
            return
        try:
            from gemini_export import (
                GeminiExporter,
                RequestBackoffLimitReachedError,
                SessionExpiredError,
                get_cookies_from_local_browser,
            )
        except Exception as exc:
            raise RuntimeError(f"导出脚本不可用: {exc}") from exc
        self._exporter_cls = GeminiExporter
        self._backoff_limit_error_cls = RequestBackoffLimitReachedError
        self._session_expired_error_cls = SessionExpiredError
        self._cookies_loader = get_cookies_from_local_browser

    def _get_cookies(self) -> Dict[str, str]:
        self._ensure_export_api()
        if self._cookies is None:
            assert self._cookies_loader is not None
            with contextlib.redirect_stdout(sys.stderr):
                cookies = self._cookies_loader()
            if not cookies:
                raise RuntimeError("本机浏览器 cookies 读取失败")
            self._cookies = cookies
        return self._cookies

    def _read_account_mapping(self, account_id: str) -> tuple[Optional[str], Optional[str]]:
        if account_id in self._account_map:
            return self._account_map[account_id]

        accounts_file = self.output_dir / "accounts.json"
        if not accounts_file.exists():
            raise RuntimeError("accounts.json 不存在，请先导入账号")

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
        result = authuser_str, email_str
        self._account_map[account_id] = result
        return result

    def _exporter_key(self, account_id: str, authuser: Optional[str]) -> str:
        return f"{account_id}:{authuser or ''}"

    def _get_exporter(self, account_id: str) -> Any:
        self._ensure_export_api()
        authuser, email = self._read_account_mapping(account_id)
        cache_key = self._exporter_key(account_id, authuser)

        exporter = self._exporters.get(cache_key)
        if exporter is not None:
            return exporter

        cookies = self._get_cookies()
        assert self._exporter_cls is not None
        exporter = self._exporter_cls(
            cookies,
            user=authuser,
            account_id=account_id,
            account_email=email,
        )
        with contextlib.redirect_stdout(sys.stderr):
            exporter.init_auth()
        self._exporters[cache_key] = exporter
        return exporter

    def _refresh_exporter_session(self, account_id: str) -> Any:
        """销毁旧 exporter，重新读取 Chrome cookies，重建新 exporter（等价于重启 app）。"""
        self._log("reinit", f"session 过期，开始重建 exporter (account={account_id})")

        self._cookies = None  # 清空内存缓存，强制重读磁盘
        fresh_cookies = self._get_cookies()

        key_fields = [k for k in ("__Secure-1PSID", "__Secure-1PSIDTS") if k in fresh_cookies]
        self._log("reinit", f"已从 Chrome 读取到 {len(fresh_cookies)} 个 cookies"
                            f"，关键字段: {key_fields or '无'}")

        authuser, email = self._read_account_mapping(account_id)
        cache_key = self._exporter_key(account_id, authuser)
        self._exporters.pop(cache_key, None)  # 销毁旧对象

        assert self._exporter_cls is not None
        exporter = self._exporter_cls(
            fresh_cookies,
            user=authuser,
            account_id=account_id,
            account_email=email,
        )
        with contextlib.redirect_stdout(sys.stderr):
            exporter.init_auth()
        self._exporters[cache_key] = exporter

        at_preview = exporter.at[:24] + "..." if exporter.at else "N/A"
        self._log("reinit", f"重建成功 ✓  at={at_preview}  bl={exporter.bl}")
        return exporter

    def _run_with_session_retry(
        self,
        account_id: str,
        fn: Callable[[Any], Any],
    ) -> Any:
        """执行 fn(exporter)，遇到 SessionExpiredError 时刷新 session 后重试一次。"""
        exporter = self._get_exporter(account_id)
        try:
            return fn(exporter)
        except Exception as exc:
            sess_cls = self._session_expired_error_cls
            if sess_cls is None or not isinstance(exc, sess_cls):
                raise
            self._log("reinit", f"触发原因: {exc}")
            exporter = self._refresh_exporter_session(account_id)
            return fn(exporter)

    def _log(self, phase: str, message: str) -> None:
        print(f"[worker:{phase}] {message}", file=sys.stderr, flush=True)

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

    @staticmethod
    def _conversation_status(row: Dict[str, Any]) -> str:
        status = row.get("status")
        if isinstance(status, str):
            status = status.strip()
            if status:
                return status
        return "normal"

    @classmethod
    def _is_lost_conversation(cls, row: Dict[str, Any]) -> bool:
        return cls._conversation_status(row) == "lost"

    def _load_conversation_ids(self, account_id: str, items: Optional[list] = None) -> list[str]:
        if items is None:
            items = self._load_conversation_items(account_id)
        out: list[str] = []
        seen: set[str] = set()
        for row in items:
            if self._is_lost_conversation(row):
                continue
            cid = row.get("id")
            if isinstance(cid, str) and cid.strip():
                normalized = cid.strip()
                if normalized in seen:
                    continue
                seen.add(normalized)
                out.append(normalized)
        return out

    @staticmethod
    def _jsonl_has_failed_marker(jsonl_path: Path) -> bool:
        if not jsonl_path.exists():
            return False
        try:
            with jsonl_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if '"downloadFailed": true' in line:
                        return True
        except Exception:
            return False
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

    def _collect_failed_conversation_ids(self, account_id: str, items: Optional[list] = None) -> list[str]:
        if items is None:
            items = self._load_conversation_items(account_id)
        account_dir = self.output_dir / "accounts" / account_id
        conv_dir = account_dir / "conversations"
        ids: set[str] = set()
        item_map: Dict[str, Dict[str, Any]] = {}
        for row in items:
            cid = row.get("id")
            if isinstance(cid, str) and cid.strip():
                item_map[cid.strip()] = row

        if conv_dir.exists():
            for jsonl_path in conv_dir.glob("*.jsonl"):
                if self._jsonl_has_failed_marker(jsonl_path):
                    cid = jsonl_path.stem
                    row = item_map.get(cid)
                    if isinstance(row, dict) and self._is_lost_conversation(row):
                        continue
                    ids.add(cid)

        return sorted(ids)

    def _collect_empty_conversation_ids(self, account_id: str, items: Optional[list] = None) -> list[str]:
        if items is None:
            items = self._load_conversation_items(account_id)
        out: list[str] = []

        for row in items:
            if self._is_lost_conversation(row):
                continue
            cid = row.get("id")
            if not isinstance(cid, str) or not cid.strip():
                continue
            cid = cid.strip()
            message_count = row.get("messageCount")
            if isinstance(message_count, int) and message_count == 0:
                out.append(cid)

        return out

    @staticmethod
    def _merge_batch_result(
        result: Dict[str, Any],
        success_ids: set[str],
        failed_ids: set[str],
    ) -> None:
        success_ids.update(result["succeeded"])
        failed_ids.update(result["failed"])
        failed_ids.difference_update(result["succeeded"])

    def _sync_conversation_batch(
        self,
        job: Dict[str, Any],
        account_id: str,
        conv_ids: list[str],
        phase: str,
    ) -> Dict[str, Any]:
        total = len(conv_ids)
        if total == 0:
            self._log(phase, "进度: 0/0")
            self._emit_job_state(job, "running", phase=phase, progress={"current": 0, "total": 0})
            return {"succeeded": [], "failed": []}

        succeeded: list[str] = []
        failed: list[str] = []
        self._log(phase, f"进度: 0/{total}")

        for idx, cid in enumerate(conv_ids, start=1):
            sub_job = {
                "jobId": f"{job['jobId']}:conv:{phase}:{cid}",
                "type": "sync_conversation",
                "accountId": account_id,
                "conversationId": cid,
            }
            progress = {"current": idx - 1, "total": total}
            self._emit_job_state(sub_job, "running", phase=phase, progress=progress)
            try:
                self._execute_sync_conversation(sub_job)
                self._emit_job_state(sub_job, "done", phase=phase, progress={"current": idx, "total": total})
                succeeded.append(cid)
            except Exception as exc:
                self._emit_job_state(
                    sub_job,
                    "failed",
                    phase=phase,
                    progress={"current": idx, "total": total},
                    error=self._to_error(exc),
                )
                if self._is_backoff_limit_error(exc):
                    self._log(
                        phase,
                        "触发全局退避兜底，提前结束当前全量批次: "
                        f"{idx}/{total}, cid={cid}",
                    )
                    raise
                failed.append(cid)

            self._log(
                phase,
                f"进度: {idx}/{total} ok={len(succeeded)} fail={len(failed)} cid={cid}",
            )
            self._emit_job_state(
                job,
                "running",
                phase=phase,
                progress={"current": idx, "total": total},
            )

        return {"succeeded": succeeded, "failed": failed}

    def _execute_sync_list(self, job: Dict[str, Any], stop_on_unchanged: bool = False) -> Dict[str, Any]:
        account_id = job["accountId"]

        def run(exporter: Any) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                list_result = exporter.export_list_only(output_dir=str(self.output_dir), stop_on_unchanged=stop_on_unchanged)
            total = len(self._load_conversation_ids(account_id))
            updated_ids = list_result.get("updatedIds", []) if isinstance(list_result, dict) else []
            return {"total": total, "updatedIds": updated_ids}

        return self._run_with_session_retry(account_id, run)

    def _execute_sync_conversation(self, job: Dict[str, Any]) -> Dict[str, Any]:
        account_id = job["accountId"]
        conversation_id = job.get("conversationId")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise RuntimeError("sync_conversation 缺少 conversationId")
        conversation_id = conversation_id.strip()

        def run(exporter: Any) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                exporter.sync_single_conversation(conversation_id, output_dir=str(self.output_dir))
            return {"conversationId": conversation_id}

        return self._run_with_session_retry(account_id, run)

    def _execute_sync_incremental(self, job: Dict[str, Any]) -> Dict[str, Any]:
        account_id = job["accountId"]

        def run(exporter: Any) -> Dict[str, Any]:
            from gemini_export_cli import export_incremental
            with contextlib.redirect_stdout(sys.stderr):
                export_incremental(exporter, output_dir=str(self.output_dir))
            return {}

        return self._run_with_session_retry(account_id, run)

    def _execute_sync_full(self, job: Dict[str, Any]) -> Dict[str, Any]:
        account_id = job["accountId"]
        success_ids: set[str] = set()
        failed_ids: set[str] = set()

        self._log("sync_full", f"开始全量同步: account={account_id}")

        # 一次读取当前 items，供步骤 1/2/3 共享
        items_before = self._load_conversation_items(account_id)
        before_set = set(self._load_conversation_ids(account_id, items=items_before))

        # 1) 所有记录中的失败重试（对话、媒体等）
        retry_failed_ids = self._collect_failed_conversation_ids(account_id, items=items_before)
        self._log("retry_failed", f"失败记录重试: {len(retry_failed_ids)}")
        retry_result = self._sync_conversation_batch(job, account_id, retry_failed_ids, "retry_failed")
        self._merge_batch_result(retry_result, success_ids, failed_ids)

        # 2) 同步列表中 messageCount=0 的空对话
        empty_ids = [cid for cid in self._collect_empty_conversation_ids(account_id, items=items_before) if cid not in success_ids]
        self._log("sync_empty", f"空会话补齐(message=0): {len(empty_ids)}")
        empty_result = self._sync_conversation_batch(job, account_id, empty_ids, "sync_empty")
        self._merge_batch_result(empty_result, success_ids, failed_ids)

        # 3) 拉最新列表，识别新增 ID，并同步新增
        self._emit_job_state(job, "running", phase="refresh_list")
        self._log("refresh_list", "拉取最新列表并识别新增会话")
        list_job_result = self._execute_sync_list(job, stop_on_unchanged=True)
        after_ids = self._load_conversation_ids(account_id)
        updated_ids_from_list: set[str] = set(list_job_result.get("updatedIds", []))

        # after_ids 由 _load_conversation_ids 已去重，无需额外 seen 集合
        new_ids = [cid for cid in after_ids if cid not in before_set and cid not in success_ids]

        self._log("sync_new", f"新增会话同步: {len(new_ids)}")
        new_result = self._sync_conversation_batch(job, account_id, new_ids, "sync_new")
        self._merge_batch_result(new_result, success_ids, failed_ids)

        # 4) 检查有更新的老会话 detail
        # updated_ids_from_list 由列表扫描阶段识别（remote_ts > local_ts），
        # 下探提前终止后未扫描的老会话不在其中，无需重新拉取。
        remaining_old_ids = [
            cid for cid in after_ids
            if cid in before_set and cid not in success_ids and cid in updated_ids_from_list
        ]

        self._log("sync_old", f"剩余老会话检查更新: {len(remaining_old_ids)}")
        old_result = self._sync_conversation_batch(job, account_id, remaining_old_ids, "sync_old")
        self._merge_batch_result(old_result, success_ids, failed_ids)

        total = len(after_ids)
        self._emit_job_state(job, "running", phase="sync_old", progress={"current": total, "total": total})
        self._log("sync_full", f"全量同步结束: total={total}, failed={len(failed_ids)}")
        return {"total": total, "failed": len(failed_ids), "progress": {"current": total, "total": total}}

    def _execute_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        job_type = job["type"]
        if job_type == "sync_list":
            return self._execute_sync_list(job)
        if job_type == "sync_conversation":
            return self._execute_sync_conversation(job)
        if job_type == "sync_incremental":
            return self._execute_sync_incremental(job)
        if job_type == "sync_full":
            return self._execute_sync_full(job)
        raise RuntimeError(f"未知任务类型: {job_type}")

    def _job_loop(self) -> None:
        while self._running:
            try:
                job = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            self._emit_job_state(job, "running")
            try:
                result = self._execute_job(job)
                done_progress = result.get("progress") if isinstance(result, dict) else None
                self._emit_job_state(job, "done", progress=done_progress)
            except Exception as exc:
                if self._is_backoff_limit_error(exc):
                    self._log("job_loop", f"任务因全局退避兜底提前结束: {exc}")
                else:
                    traceback.print_exc(file=sys.stderr)
                self._emit_job_state(job, "failed", error=self._to_error(exc))
            finally:
                self._queue.task_done()

    def _enqueue(self, params: Dict[str, Any]) -> Dict[str, Any]:
        job_type = params.get("type")
        account_id = params.get("accountId")
        conversation_id = params.get("conversationId")

        if job_type not in JOB_TYPES:
            raise RuntimeError(f"不支持的任务类型: {job_type}")
        if not isinstance(account_id, str) or not account_id.strip():
            raise RuntimeError("accountId 不能为空")
        account_id = account_id.strip()
        if job_type == "sync_conversation":
            if not isinstance(conversation_id, str) or not conversation_id.strip():
                raise RuntimeError("sync_conversation 需要 conversationId")
            conversation_id = conversation_id.strip()
        else:
            conversation_id = None

        job = {
            "jobId": f"job_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}",
            "type": job_type,
            "accountId": account_id,
            "conversationId": conversation_id,
            "enqueuedAt": now_iso(),
        }
        self._queue.put(job)
        self._emit_job_state(job, "queued")
        return {"jobId": job["jobId"]}

    def handle_request(self, msg: Dict[str, Any]) -> None:
        req_id = msg.get("id")
        if not isinstance(req_id, str) or not req_id:
            return

        method = msg.get("method")
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}

        try:
            if method == "ping":
                self._send_response(req_id, True, {"pong": True, "ts": now_iso()})
                return
            if method == "shutdown":
                self._running = False
                self._send_response(req_id, True, {"status": "ok"})
                return
            if method == "enqueue_job":
                result = self._enqueue(params)
                self._send_response(req_id, True, result)
                return
            raise RuntimeError(f"未知方法: {method}")
        except Exception as exc:
            self._send_response(req_id, False, error=self._to_error(exc))

    def stop(self) -> None:
        self._running = False
        self._worker_thread.join(timeout=2.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini stdio worker")
    parser.add_argument("--output-dir", required=True, help="app data directory")
    args = parser.parse_args()

    worker = GeminiWorker(Path(args.output_dir))

    try:
        for line in sys.stdin:
            raw = line.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[worker:stdin] JSON 解析错误: {exc}", file=sys.stderr)
                continue

            if not isinstance(msg, dict):
                continue
            worker.handle_request(msg)
            if not worker._running:
                break
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()


if __name__ == "__main__":
    main()
