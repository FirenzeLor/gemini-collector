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
from typing import Any, Callable, Dict, Optional, Tuple


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

    def _to_error(self, exc: BaseException) -> Dict[str, Any]:
        is_auth = self._is_auth_error(exc)
        code = "AUTH_EXPIRED" if is_auth else "SCRIPT_ERROR"
        return {
            "code": code,
            "message": str(exc) or exc.__class__.__name__,
            "retryable": bool(is_auth),
        }

    def _ensure_export_api(self) -> None:
        if self._exporter_cls is not None and self._cookies_loader is not None:
            return
        try:
            from gemini_export import GeminiExporter, get_cookies_from_local_browser
        except Exception as exc:
            raise RuntimeError(f"导出脚本不可用: {exc}") from exc
        self._exporter_cls = GeminiExporter
        self._cookies_loader = get_cookies_from_local_browser

    def _get_cookies(self, force_refresh: bool = False) -> Dict[str, str]:
        self._ensure_export_api()
        if force_refresh:
            self._cookies = None
        if self._cookies is None:
            assert self._cookies_loader is not None
            with contextlib.redirect_stdout(sys.stderr):
                cookies = self._cookies_loader()
            if not cookies:
                raise RuntimeError("本机浏览器 cookies 读取失败")
            self._cookies = cookies
        return self._cookies

    def _read_account_mapping(self, account_id: str) -> Tuple[Optional[str], Optional[str]]:
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
        return authuser_str, email_str

    def _exporter_key(self, account_id: str, authuser: Optional[str]) -> str:
        return f"{account_id}:{authuser or ''}"

    def _get_exporter(
        self,
        account_id: str,
        force_refresh: bool = False,
    ) -> Any:
        self._ensure_export_api()
        authuser, email = self._read_account_mapping(account_id)
        cache_key = self._exporter_key(account_id, authuser)
        if force_refresh:
            self._exporters.pop(cache_key, None)

        exporter = self._exporters.get(cache_key)
        if exporter is not None:
            return exporter

        cookies = self._get_cookies(force_refresh=force_refresh)
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

    def _run_with_auth_retry(
        self,
        account_id: str,
        fn: Callable[[Any], Dict[str, Any]],
    ) -> Dict[str, Any]:
        exporter = self._get_exporter(account_id, force_refresh=False)
        try:
            return fn(exporter)
        except Exception as exc:
            if not self._is_auth_error(exc):
                raise
            exporter = self._get_exporter(account_id, force_refresh=True)
            return fn(exporter)

    def _load_conversation_ids(self, account_id: str) -> list[str]:
        conv_index = self.output_dir / "accounts" / account_id / "conversations.json"
        if not conv_index.exists():
            return []
        try:
            data = json.loads(conv_index.read_text(encoding="utf-8"))
        except Exception:
            return []
        items = data.get("items", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for row in items:
            if not isinstance(row, dict):
                continue
            cid = row.get("id")
            if isinstance(cid, str) and cid.strip():
                out.append(cid.strip())
        return out

    def _execute_sync_list(self, job: Dict[str, Any]) -> Dict[str, Any]:
        account_id = job["accountId"]

        def run(exporter: Any) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                exporter.export_list_only(output_dir=str(self.output_dir))
            total = len(self._load_conversation_ids(account_id))
            return {"total": total}

        return self._run_with_auth_retry(account_id, run)

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

        return self._run_with_auth_retry(account_id, run)

    def _execute_sync_incremental(self, job: Dict[str, Any]) -> Dict[str, Any]:
        account_id = job["accountId"]

        def run(exporter: Any) -> Dict[str, Any]:
            with contextlib.redirect_stdout(sys.stderr):
                exporter.export_incremental(output_dir=str(self.output_dir))
            return {}

        return self._run_with_auth_retry(account_id, run)

    def _execute_sync_full(self, job: Dict[str, Any]) -> Dict[str, Any]:
        account_id = job["accountId"]

        self._emit_job_state(job, "running", phase="list")
        self._execute_sync_list(job)

        conv_ids = self._load_conversation_ids(account_id)
        total = len(conv_ids)
        failed = 0

        for idx, cid in enumerate(conv_ids, start=1):
            sub_job = {
                "jobId": f"{job['jobId']}:conv:{cid}",
                "type": "sync_conversation",
                "accountId": account_id,
                "conversationId": cid,
            }
            progress = {"current": idx - 1, "total": total}
            self._emit_job_state(sub_job, "running", phase="conversation", progress=progress)
            try:
                self._execute_sync_conversation(sub_job)
                progress = {"current": idx, "total": total}
                self._emit_job_state(sub_job, "done", phase="conversation", progress=progress)
            except Exception as exc:
                failed += 1
                progress = {"current": idx, "total": total}
                self._emit_job_state(
                    sub_job,
                    "failed",
                    phase="conversation",
                    progress=progress,
                    error=self._to_error(exc),
                )

            self._emit_job_state(
                job,
                "running",
                phase="conversation",
                progress={"current": idx, "total": total},
            )

        return {"total": total, "failed": failed}

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
        if job_type == "sync_conversation":
            if not isinstance(conversation_id, str) or not conversation_id.strip():
                raise RuntimeError("sync_conversation 需要 conversationId")
            conversation_id = conversation_id.strip()
        else:
            conversation_id = None

        job = {
            "jobId": f"job_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}",
            "type": job_type,
            "accountId": account_id.strip(),
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
                print(f"[worker] request json parse error: {exc}", file=sys.stderr)
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
