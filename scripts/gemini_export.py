#!/usr/bin/env python3
"""
Gemini 全量聊天导出工具

工作流程:
1. 从本机浏览器或 cookies 文件读取登录态
2. 获取聊天数据
3. 导出对话与媒体文件

依赖: pip3 install httpx browser-cookie3
"""

import json
import os
import random
import re
import sys
import time
import datetime
import uuid
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlencode, quote, urlparse, parse_qsl, urlunparse, urljoin

# Prepend bundled vendor directory (populated at DMG build time)
_vendor_dir = Path(__file__).parent / "_vendor"
if _vendor_dir.exists() and str(_vendor_dir) not in sys.path:
    sys.path.insert(0, str(_vendor_dir))

try:
    import httpx
except ImportError:
    print("缺少 httpx，正在安装...")
    os.system(f"{sys.executable} -m pip install httpx")
    import httpx

try:
    import browser_cookie3
except ImportError:
    print("缺少 browser_cookie3，正在安装...")
    os.system(f"{sys.executable} -m pip install browser-cookie3")
    import browser_cookie3

from gemini_protocol import (
    GEMINI_BASE, BATCH_SIZE, DETAIL_PAGE_SIZE,
    REQUEST_DELAY, REQUEST_JITTER_MIN, REQUEST_JITTER_MAX, REQUEST_JITTER_MODE,
    REQUEST_BACKOFF_MAX_SECONDS, REQUEST_BACKOFF_LIMIT_FAILURES,
    BROWSER_USER_AGENT, BROWSER_ACCEPT_LANGUAGE,
    RequestBackoffLimitReachedError, SessionExpiredError,
    timing_log, _to_iso_utc, _coerce_epoch_seconds, _iso_to_epoch_seconds,
    _summary_to_epoch_seconds, email_to_account_id, normalize_chat_id,
    _diagnose_auth_page, _extract_chat_latest_update, _request_backoff_seconds,
    parse_batchexecute_response, has_batchexecute_session_error,
    mask_email,
)
from gemini_cookies import (
    GOOGLE_MEDIA_COOKIE_NAMES,
    get_cookies_from_local_browser, discover_email_authuser_mapping,
)
from gemini_turn_parser import parse_turn, normalize_turn_media_first_seen
from gemini_media import (
    PROTECTED_MEDIA_HOSTS,
    _video_preview_name, _generate_video_preview, _ensure_video_previews_from_turns,
    _infer_media_type, _media_log_fields, _append_authuser,
)
from gemini_storage import (
    CONVERSATION_STATUS_NORMAL, CONVERSATION_STATUS_LOST, CONVERSATION_STATUS_HIDDEN,
    _read_jsonl_rows, _write_jsonl_rows,
    _turn_id_from_raw, _dedupe_raw_turns_by_id, _dedupe_message_rows_by_id,
    _merge_message_rows_for_write, _is_media_file_ready,
    _build_media_id_to_url_map, _scan_failed_media_from_rows,
    _update_jsonl_media_failure_flags,
    _build_existing_turn_id_set, _latest_ts_from_rows,
    _load_media_manifest, _save_media_manifest,
    _load_media_manifest_new, _save_media_manifest_new,
    _sort_parsed_turns_by_timestamp, _turns_to_jsonl_rows,
    _build_existing_turn_id_set_new, _count_message_rows_new,
    _count_media_types_from_rows, _rows_has_failed_data, _remote_hash_from_jsonl,
    _filter_display_rows,
    _write_accounts_json, _write_account_meta,
    _write_conversations_index, _write_sync_state, _load_sync_state,
    _load_conversations_index,
    _normalize_conversation_status, _status_for_remote_summary,
    _build_lost_summary, _build_summary_from_chat_listing,
)

OUTPUT_DIR = Path("gemini_export_output")


# ============================================================================
# 主导出类
# ============================================================================
class GeminiExporter:

    def __init__(self, cookies: dict, user=None, account_id=None, account_email=None):
        self.cookies = cookies
        self.user_spec = str(user).strip() if user is not None else None
        self.account_id_override = str(account_id).strip() if account_id else None
        if self.account_id_override == "":
            self.account_id_override = None
        self.account_email_override = str(account_email).strip().lower() if account_email else None
        if self.account_email_override == "":
            self.account_email_override = None
        self.authuser = None
        self.client = self._create_http_client()
        self.at = None   # CSRF token
        self.bl = None   # 服务器版本
        self.fsid = None # session ID
        self.reqid = 100000
        self._request_started = False
        self._request_consecutive_failures = 0
        self._limit_probe_consumed = False
        self._last_delay_sec = 0.0
        self._request_state_account_dir = None

    def _create_http_client(self, cookies=None):
        cookie_payload = self.cookies if cookies is None else cookies
        return httpx.Client(
            cookies=cookie_payload,
            headers={
                "User-Agent": BROWSER_USER_AGENT,
                "Accept-Language": BROWSER_ACCEPT_LANGUAGE,
            },
            follow_redirects=True,
            timeout=60.0,
        )

    def _resolve_authuser(self):
        """解析用户指定账号：支持索引(0/1/2...)或邮箱"""
        if not self.user_spec:
            return

        if self.user_spec.isdigit():
            self.authuser = self.user_spec
            return

        email = self.user_spec.lower()
        # 优先走 ListAccounts 映射
        try:
            mappings = discover_email_authuser_mapping(self.cookies)
            for item in mappings:
                if item.get("email") == email and item.get("authuser") is not None:
                    self.authuser = str(item["authuser"])
                    print(f"  authuser: {self.authuser} (ListAccounts 映射)")
                    return
        except Exception:
            pass

        # 尝试通过页面内容匹配邮箱 -> authuser 索引
        for idx in range(10):
            try:
                self._before_request("resolve_authuser_probe")
                resp = self.client.get(f"{GEMINI_BASE}/app", params={"authuser": str(idx)})
                if email in resp.text.lower():
                    self.authuser = str(idx)
                    print(f"  authuser: {self.authuser} (邮箱匹配)")
                    return
            except Exception as e:
                if isinstance(e, RequestBackoffLimitReachedError):
                    raise
                pass

        # 无法匹配索引时，直接透传邮箱给 authuser 参数
        self.authuser = self.user_spec
        print("  [warn] 未匹配到邮箱对应索引，改为直接使用邮箱作为 authuser")

    def _authuser_params(self):
        if self.authuser is None:
            self._resolve_authuser()
        if self.authuser:
            return {"authuser": self.authuser}
        return {}

    def list_user_options(self):
        """列出可选邮箱及其 authuser 映射。"""
        mappings = discover_email_authuser_mapping(self.cookies)

        # 去重（同邮箱保留首个可用映射）
        dedup = {}
        for item in mappings:
            email = item.get("email")
            if not email:
                continue
            if email not in dedup:
                dedup[email] = item
            elif dedup[email].get("authuser") is None and item.get("authuser") is not None:
                dedup[email] = item

        result = []
        authuser_ok_cache = {}
        for email, item in dedup.items():
            authuser = item.get("authuser")
            gemini_ok = None
            fsid = None

            if authuser is not None:
                if authuser not in authuser_ok_cache:
                    probe = GeminiExporter(self.cookies, user=str(authuser))
                    try:
                        probe.init_auth()
                        authuser_ok_cache[authuser] = (True, probe.fsid)
                    except Exception:
                        authuser_ok_cache[authuser] = (False, None)
                gemini_ok, fsid = authuser_ok_cache[authuser]

            result.append({
                "email": email,
                "authuser": authuser,
                "gemini_ok": gemini_ok,
                "f_sid": fsid,
                "redirect_url": item.get("redirect_url"),
            })

        result.sort(key=lambda x: (x.get("authuser") is None, int(x["authuser"]) if str(x.get("authuser") or "").isdigit() else 999, x.get("email", "")))
        return result

    def _next_reqid(self):
        self.reqid += 100000
        return str(self.reqid)

    def _request_backoff_ms(self):
        return int(round(_request_backoff_seconds(self._request_consecutive_failures) * 1000))

    def _current_request_state(self, updated_at=None):
        now_iso = updated_at or datetime.datetime.now(datetime.UTC).isoformat()
        return {
            "consecutiveFailures": self._request_consecutive_failures,
            "backoffMs": self._request_backoff_ms(),
            "updatedAt": now_iso,
        }

    def _sync_request_state_file(self):
        account_dir = self._request_state_account_dir
        if account_dir is None:
            return
        state = _load_sync_state(account_dir)
        if not isinstance(state, dict):
            state = {}
        now_iso = datetime.datetime.now(datetime.UTC).isoformat()
        state["version"] = int(state.get("version") or 1)
        state["accountId"] = state.get("accountId") or account_dir.name
        state["updatedAt"] = now_iso
        state["requestState"] = self._current_request_state(now_iso)
        _write_sync_state(account_dir, state)

    def _set_request_state_scope(self, account_dir):
        self._request_state_account_dir = Path(account_dir)
        # 新任务作用域开始时重置请求起始标记。
        self._request_started = False
        state = _load_sync_state(self._request_state_account_dir)
        if not isinstance(state, dict):
            return
        request_state = state.get("requestState")
        if isinstance(request_state, dict):
            count = request_state.get("consecutiveFailures")
            if isinstance(count, int) and count >= 0:
                self._request_consecutive_failures = min(count, REQUEST_BACKOFF_LIMIT_FAILURES)
                return
        full_sync = state.get("fullSync")
        if isinstance(full_sync, dict):
            count = full_sync.get("listingConsecutiveFailures")
            if isinstance(count, int) and count >= 0:
                self._request_consecutive_failures = min(count, REQUEST_BACKOFF_LIMIT_FAILURES)

    def _before_request(self, label, verbose=True):
        backoff_sec = _request_backoff_seconds(self._request_consecutive_failures)
        if backoff_sec >= REQUEST_BACKOFF_MAX_SECONDS:
            if not self._request_started and not self._limit_probe_consumed:
                self._limit_probe_consumed = True
                if verbose:
                    print(
                        "  [backoff] 连续失败达到上限，放行一次启动探测请求:"
                        f" failures={self._request_consecutive_failures}, op={label}"
                    )
            else:
                self._sync_request_state_file()
                raise RequestBackoffLimitReachedError(
                    "请求连续失败达到退避上限，触发全局兜底提前结束: "
                    f"failures={self._request_consecutive_failures}, "
                    f"wait={backoff_sec:.0f}s, op={label}"
                )
        if self._request_started:
            delay_sec = REQUEST_DELAY + random.triangular(
                REQUEST_JITTER_MIN,
                REQUEST_JITTER_MAX,
                REQUEST_JITTER_MODE,
            )
            self._last_delay_sec = delay_sec
            time.sleep(delay_sec)
        if 0 < backoff_sec < REQUEST_BACKOFF_MAX_SECONDS:
            self._sync_request_state_file()
            if verbose:
                print(
                    "  [backoff] 连续失败退避等待:"
                    f" failures={self._request_consecutive_failures}, wait={backoff_sec:.2f}s, op={label}"
                )
            time.sleep(backoff_sec)

        self._request_started = True

    def _mark_request_success(self):
        if self._request_consecutive_failures == 0:
            return
        self._request_consecutive_failures = 0
        self._limit_probe_consumed = False
        self._sync_request_state_file()

    def _mark_request_failure(self):
        self._request_consecutive_failures = min(
            self._request_consecutive_failures + 1,
            REQUEST_BACKOFF_LIMIT_FAILURES,
        )
        self._sync_request_state_file()

    def _client_get_with_retry(self, url, params=None, attempts=6, count_as_business_request=True):
        last_err = None
        for _ in range(attempts):
            try:
                self._before_request("http_get")
                resp = self.client.get(url, params=params)
                if count_as_business_request:
                    self._mark_request_success()
                return resp
            except Exception as e:
                if isinstance(e, RequestBackoffLimitReachedError):
                    raise
                if count_as_business_request:
                    self._mark_request_failure()
                last_err = e
        if last_err:
            raise last_err
        raise RuntimeError("GET 请求失败")

    # 初始化认证参数
    # ------------------------------------------------------------------
    def init_auth(self):
        """从 Gemini 页面提取认证参数 (at, bl, f.sid)"""
        print("[*] 获取认证参数...")
        params = self._authuser_params()
        if params.get("authuser") is not None:
            print(f"  使用 authuser: {params['authuser']}")
        # init_auth 只做认证参数刷新，不参与业务请求成功/失败计数。
        resp = self._client_get_with_retry(
            f"{GEMINI_BASE}/app",
            params=params,
            count_as_business_request=False,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"获取 Gemini 页面失败: HTTP {resp.status_code}")

        html = resp.text

        # 提取 SNlM0e (at token)
        at_match = re.search(r'"SNlM0e":"([^"]+)"', html)
        if not at_match:
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
            page_title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else "-"
            final_url = str(resp.url)
            diagnosis = _diagnose_auth_page(html, final_url)
            raise RuntimeError(
                "无法提取 CSRF token (SNlM0e); "
                f"url={final_url}; title={page_title}; hint={diagnosis}"
            )
        self.at = at_match.group(1)

        # 提取 cfb2h (bl - server version)
        bl_match = re.search(r'"cfb2h":"([^"]+)"', html)
        if bl_match:
            self.bl = bl_match.group(1)
        else:
            self.bl = "boq_assistant-bard-web-server_20260210.04_p0"

        # 提取 FdrFJe (f.sid)
        fsid_match = re.search(r'"FdrFJe":"(-?\d+)"', html)
        if fsid_match:
            self.fsid = fsid_match.group(1)
        else:
            self.fsid = "0"

        print(f"  at: {self.at[:20]}...")
        print(f"  bl: {self.bl}")
        print(f"  f.sid: {self.fsid}")

    # ------------------------------------------------------------------
    # batchexecute 请求
    # ------------------------------------------------------------------
    def _batchexecute(self, rpcid, payload_json, source_path=""):
        """发送 batchexecute 请求"""
        f_req = json.dumps([[[rpcid, payload_json, None, "generic"]]])

        params = {
            "rpcids": rpcid,
            "bl": self.bl,
            "f.sid": self.fsid,
            "hl": "zh-CN",
            "_reqid": self._next_reqid(),
            "rt": "c",
        }
        params.update(self._authuser_params())
        if source_path:
            params["source-path"] = source_path

        data = {
            "f.req": f_req,
            "at": self.at,
        }

        url = f"{GEMINI_BASE}/_/BardChatUi/data/batchexecute"
        req_start = time.perf_counter()
        self._before_request(f"batchexecute:{rpcid}")
        try:
            resp = self.client.post(
                url,
                params=params,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "x-goog-ext-73010989-jspb": "[0]",
                    "x-goog-ext-525001261-jspb": "[1,null,null,null,null,null,null,null,[4]]",
                },
            )
        except Exception:
            self._mark_request_failure()
            raise
        timing_log(
            "_batchexecute",
            req_start,
            rpc=rpcid,
            status=resp.status_code,
            source=source_path or "/app",
            delay=f"{self._last_delay_sec*1000:.0f}ms",
        )

        if resp.status_code != 200:
            self._mark_request_failure()
            print(f"  [debug] 响应内容: {resp.text[:500]}")
            raise RuntimeError(f"batchexecute 失败: HTTP {resp.status_code}")

        results = parse_batchexecute_response(resp.text)
        for rid, data_inner in results:
            if rid == rpcid:
                self._mark_request_success()
                return data_inner

        self._mark_request_failure()
        print(f"  [debug] 响应中未找到 {rpcid}，已解析 {len(results)} 项: "
              f"{[r[0] for r in results]}")
        print(f"  [debug] 原始响应 (前500字符): {resp.text[:500]}")
        if has_batchexecute_session_error(resp.text, rpcid):
            raise SessionExpiredError(
                f"会话已过期（服务端返回空数据）: rpcid={rpcid}"
            )
        raise RuntimeError(f"响应中未找到 {rpcid} 数据")

    # ------------------------------------------------------------------
    # 获取聊天列表
    # ------------------------------------------------------------------
    def get_chats_page(self, cursor=None):
        """
        拉取单页聊天列表。

        cursor 为 None 时拉第一页；否则按 next token 拉后续页。
        返回: (items, next_cursor)
        """
        if cursor is None:
            payload = json.dumps([BATCH_SIZE, None, [0, None, 1]])
        else:
            payload = json.dumps([BATCH_SIZE, cursor])

        step_start = time.perf_counter()
        result = self._batchexecute("MaZiqc", payload, source_path="/app")

        if not result or not isinstance(result, list):
            return [], None

        next_token = result[1] if len(result) > 1 and isinstance(result[1], str) and result[1] else None
        raw_chats = result[2] if len(result) > 2 and isinstance(result[2], list) else []

        items = []
        for chat in raw_chats:
            if isinstance(chat, list) and len(chat) > 1:
                conv_id = chat[0]
                title = chat[1] if len(chat) > 1 else ""
                latest_update_ts = _extract_chat_latest_update(chat)
                items.append({
                    "id": conv_id,
                    "title": title,
                    "latest_update_ts": latest_update_ts,
                    "latest_update_iso": _to_iso_utc(latest_update_ts),
                })

        timing_log(
            "get_chats_page",
            step_start,
            cursor="init" if cursor is None else "next",
            items=len(items),
            has_next=bool(next_token),
            delay=f"{self._last_delay_sec*1000:.0f}ms",
        )
        return items, next_token

    def get_all_chats(self):
        """获取所有聊天列表（含分页）"""
        print("[*] 获取聊天列表...")
        all_chats = []
        page = 0
        cursor = None

        while True:
            page += 1
            items, next_token = self.get_chats_page(cursor)
            if not items and not next_token:
                if page == 1:
                    print("  [debug] 首屏未拿到聊天列表")
                break

            all_chats.extend(items)
            print(f"  第 {page} 页: {len(items)} 个对话 (累计 {len(all_chats)})")

            if not next_token:
                break

            cursor = next_token

        print(f"  共 {len(all_chats)} 个对话")
        return all_chats

    # ------------------------------------------------------------------
    # 获取对话详情
    # ------------------------------------------------------------------
    def get_chat_detail_page(self, conv_id, cursor=None):
        """
        拉取单页会话详情。

        cursor 为 None 时拉第一页；否则按分页 token 拉后续页。
        返回: (turns, next_cursor)
        """
        source_path = f"/app/{conv_id.replace('c_', '')}"
        payload = json.dumps(
            [conv_id, DETAIL_PAGE_SIZE, cursor, 1, [1], [4], None, 1]
        )
        step_start = time.perf_counter()
        result = self._batchexecute("hNvQHb", payload, source_path=source_path)

        if not result or not isinstance(result, list):
            return [], None

        turns = result[0] if len(result) > 0 and isinstance(result[0], list) else []
        next_cursor = result[1] if len(result) > 1 and isinstance(result[1], str) and result[1] else None
        timing_log(
            "get_chat_detail_page",
            step_start,
            conversation=conv_id.replace("c_", ""),
            cursor="init" if cursor is None else "next",
            turns=len(turns),
            has_next=bool(next_cursor),
            delay=f"{self._last_delay_sec*1000:.0f}ms",
        )
        return turns, next_cursor

    def get_chat_detail(self, conv_id):
        """获取单个对话的完整内容（含分页）"""
        all_turns = []
        page = 0
        cursor = None

        while True:
            page += 1
            turns, next_cursor = self.get_chat_detail_page(conv_id, cursor)
            if not turns and not next_cursor:
                break

            all_turns.extend(turns)
            if not next_cursor:
                break

            cursor = next_cursor

        return all_turns

    def get_chat_latest_update(self, chat_id):
        """按 chat_id 查询会话最新更新时间（秒级时间戳）"""
        page = 0

        payload = json.dumps([BATCH_SIZE, None, [0, None, 1]])
        result = self._batchexecute("MaZiqc", payload, source_path="/app")

        while True:
            page += 1

            if not result or not isinstance(result, list) or len(result) < 3 or not result[2]:
                return None

            chats = result[2]
            for chat in chats:
                if not isinstance(chat, list) or len(chat) == 0:
                    continue
                if chat[0] == chat_id:
                    return _extract_chat_latest_update(chat)

            next_token = result[1] if len(result) > 1 else None
            if not next_token or not isinstance(next_token, str):
                return None

            payload = json.dumps([BATCH_SIZE, next_token])
            result = self._batchexecute("MaZiqc", payload, source_path="/app")

    def is_chat_updated(self, chat_id, last_update_ts):
        """比较会话最新更新时间，返回是否有更新"""
        try:
            previous_ts = int(last_update_ts)
        except (TypeError, ValueError):
            raise ValueError("last_update_ts 必须是秒级时间戳（整数）")

        latest_ts = self.get_chat_latest_update(chat_id)
        updated = latest_ts is not None and latest_ts > previous_ts

        return {
            "chat_id": chat_id,
            "previous_update_ts": previous_ts,
            "previous_update_iso": _to_iso_utc(previous_ts),
            "latest_update_ts": latest_ts,
            "latest_update_iso": _to_iso_utc(latest_ts),
            "updated": updated,
            "found": latest_ts is not None,
        }


    # ------------------------------------------------------------------
    # 批量下载媒体文件（无 CDP）
    # ------------------------------------------------------------------
    def download_media_batch(self, media_list, media_dir, stats):
        """
        批量下载媒体文件（按用户上下文顺序下载）
        media_list: [{"url": ..., "filepath": Path}, ...]
        """
        return self._download_media_batch_no_cdp(media_list, stats)

    def _build_media_cookie_header(self):
        return "; ".join(
            f"{k}={self.cookies[k]}" for k in GOOGLE_MEDIA_COOKIE_NAMES if k in self.cookies
        )

    def _download_one_media_no_cdp(self, url, cookie_header, referer, media_type=None, media_hint=None):
        base_headers = {
            "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "accept-language": BROWSER_ACCEPT_LANGUAGE,
            "referer": referer,
            "user-agent": BROWSER_USER_AGENT,
        }

        current_url = url
        for hop in range(8):
            headers = dict(base_headers)
            host = (urlparse(current_url).hostname or "").lower()
            if host in PROTECTED_MEDIA_HOSTS:
                headers["cookie"] = cookie_header

            try:
                self._before_request("media_http_get", verbose=False)
                resp = self.client.get(
                    current_url,
                    headers=headers,
                    follow_redirects=False,
                    timeout=45.0,
                )
            except Exception as e:
                if isinstance(e, RequestBackoffLimitReachedError):
                    raise
                self._mark_request_failure()
                media_fields = _media_log_fields(current_url, media_type=media_type, media_hint=media_hint)
                print(
                    f"  [media-fail] httpx 下载异常: {e}"
                    f" | media={media_fields['media']} domain={media_fields['domain']}"
                )
                return None
            media_fields = _media_log_fields(current_url, media_type=media_type, media_hint=media_hint)

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    self._mark_request_failure()
                    print(
                        "  [media-fail] 重定向缺少 location"
                        f" | media={media_fields['media']} domain={media_fields['domain']}"
                    )
                    return None
                self._mark_request_success()
                current_url = urljoin(current_url, location)
                continue

            if resp.status_code == 200:
                self._mark_request_success()
                return resp.content

            self._mark_request_failure()
            print(
                f"  [media-fail] 非200状态码={resp.status_code}"
                f" | media={media_fields['media']} domain={media_fields['domain']}"
            )
            return None

        self._mark_request_failure()
        media_fields = _media_log_fields(url, media_type=media_type, media_hint=media_hint)
        print(
            "  [media-fail] 重定向次数超限"
            f" | media={media_fields['media']} domain={media_fields['domain']}"
        )
        return None

    def _download_media_batch_no_cdp(self, media_list, stats):
        failed_items = []
        if not media_list:
            return failed_items

        authuser = self._authuser_params().get("authuser")

        cookie_header = self._build_media_cookie_header()
        referer = f"{GEMINI_BASE}/u/{authuser}/app" if authuser is not None else f"{GEMINI_BASE}/app"

        for item in media_list:
            item_start = time.perf_counter()
            filepath = item["filepath"]
            url = item["url"]
            media_id = item.get("media_id") or filepath.name
            media_type = item.get("media_type")
            media_hint = media_id or filepath.name

            if filepath.exists():
                stats["media_downloaded"] += 1
                timing_log("_download_media_batch_no_cdp", item_start, media_id=media_id, status="skip_exists", size=f"{filepath.stat().st_size / 1024:.1f}KB")
                continue

            # 多账号登录下媒体权限与 authuser 强相关；直接使用带 authuser 的 URL，
            # 避免先请求裸链接产生一次确定性的 403/失败开销。
            candidates = [_append_authuser(url, authuser)] if authuser is not None else [url]

            content = None
            for candidate_url in candidates:
                try:
                    content = self._download_one_media_no_cdp(
                        candidate_url,
                        cookie_header,
                        referer,
                        media_type=media_type,
                        media_hint=media_hint,
                    )
                except Exception as e:
                    if isinstance(e, RequestBackoffLimitReachedError):
                        raise
                    media_fields = _media_log_fields(candidate_url, media_type=media_type, media_hint=media_hint)
                    print(
                        f"  [media-fail] 媒体下载异常: {e}"
                        f" | media={media_fields['media']} domain={media_fields['domain']}"
                    )
                    content = None
                if content:
                    break

            if content:
                filepath.parent.mkdir(parents=True, exist_ok=True)
                filepath.write_bytes(content)
                stats["media_downloaded"] += 1
                timing_log("_download_media_batch_no_cdp", item_start, media_id=media_id, status="ok", size=f"{len(content) / 1024:.1f}KB")
            else:
                media_fields = _media_log_fields(url, media_type=media_type, media_hint=media_hint)
                print(
                    f"  [media-fail] 媒体下载失败，已跳过: {filepath.name}"
                    f" | media={media_fields['media']} domain={media_fields['domain']}"
                )
                stats["media_failed"] += 1
                timing_log("_download_media_batch_no_cdp", item_start, media_id=media_id, status="failed")
                failed_items.append({
                    "media_id": media_id,
                    "url": url,
                    "error": "download_failed",
                })

        return failed_items

    # ------------------------------------------------------------------
    def _resolve_account_info(self):
        """解析当前账号信息，返回 {id, email, name, ...}"""
        email = None
        authuser_value = self.authuser
        if authuser_value is None:
            try:
                self._resolve_authuser()
                authuser_value = self.authuser
            except Exception:
                authuser_value = self.authuser

        authuser_str = None
        if authuser_value is not None:
            authuser_candidate = str(authuser_value).strip()
            if authuser_candidate.isdigit():
                authuser_str = authuser_candidate

        if self.account_id_override:
            if self.account_email_override:
                email = self.account_email_override
            elif self.user_spec and "@" in self.user_spec:
                email = self.user_spec.lower()

            name = email.split("@")[0] if email else self.account_id_override
            avatar_text = (name[0].upper() if name else "?")
            return {
                "id": self.account_id_override,
                "email": email or "",
                "name": name,
                "avatarText": avatar_text,
                "avatarColor": "#667eea",
                "conversationCount": 0,
                "remoteConversationCount": None,
                "lastSyncAt": None,
                "lastSyncResult": None,
                "authuser": authuser_str,
            }

        if self.user_spec and "@" in self.user_spec:
            email = self.user_spec.lower()
        else:
            try:
                mappings = discover_email_authuser_mapping(self.cookies)
                if authuser_str is not None:
                    for m in mappings:
                        if m.get("authuser") == authuser_str:
                            email = m.get("email")
                            break
                if not email and mappings:
                    email = mappings[0].get("email")
            except Exception:
                pass

        if email:
            safe_id = email_to_account_id(email)
            name = email.split("@")[0]
            return {
                "id": safe_id,
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
        else:
            authuser = authuser_str or "0"
            acc_id = f"user_{authuser}"
            return {
                "id": acc_id,
                "email": "",
                "name": acc_id,
                "avatarText": "U",
                "avatarColor": "#667eea",
                "conversationCount": 0,
                "remoteConversationCount": None,
                "lastSyncAt": None,
                "lastSyncResult": None,
                "authuser": authuser_str,
            }

    def _assign_media_ids_and_collect_downloads(self, parsed_turns, global_media_dir, global_seen_urls, global_used_names):
        batch_list = []

        for parsed in parsed_turns:
            for file_list in [parsed["user"]["files"], parsed["assistant"]["files"]]:
                for f in file_list:
                    url = f.get("url")
                    if not url:
                        continue

                    if url in global_seen_urls:
                        fname = global_seen_urls[url]
                        media_id = fname
                    else:
                        if f.get("type") == "video":
                            ext = "mp4"
                        elif f.get("type") == "audio":
                            ext = "mp3"
                        else:
                            ext = "jpg"
                        raw_name = f.get("filename") or ""
                        raw_suffix = Path(raw_name).suffix.lower()
                        if raw_suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".mp4", ".mov", ".webm", ".mkv", ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"}:
                            ext = raw_suffix.lstrip(".")

                        while True:
                            media_stem = uuid.uuid4().hex
                            fname = f"{media_stem}.{ext}"
                            if fname not in global_used_names:
                                break

                        global_used_names.add(fname)
                        global_seen_urls[url] = fname
                        media_id = fname

                    f["media_id"] = media_id
                    if f.get("type") == "video":
                        f["preview_media_id"] = _video_preview_name(media_id)
                    target = global_media_dir / fname
                    if not target.exists() and not any(item["filepath"] == target for item in batch_list):
                        batch_list.append({
                            "url": url,
                            "filepath": target,
                            "media_id": media_id,
                            "media_type": f.get("type"),
                        })

        return batch_list

    def get_chat_detail_incremental(self, conv_id, existing_turn_ids):
        """增量抓取单个对话：遇到已存在 turn_id 即停止向旧页翻。"""
        all_new_turns = []

        payload = json.dumps([conv_id, DETAIL_PAGE_SIZE, None, 1, [1], [4], None, 1])
        source_path = f"/app/{conv_id.replace('c_', '')}"
        result = self._batchexecute("hNvQHb", payload, source_path=source_path)

        while True:
            if not result or not result[0]:
                break

            hit_existing = False
            for turn in result[0]:
                tid = _turn_id_from_raw(turn)
                if tid and tid in existing_turn_ids:
                    hit_existing = True
                    break
                all_new_turns.append(turn)

            if hit_existing:
                break

            next_token = result[1] if len(result) > 1 and isinstance(result[1], str) else None
            if not next_token:
                break

            payload = json.dumps([conv_id, DETAIL_PAGE_SIZE, next_token, 1, [1], [4], None, 1])
            result = self._batchexecute("hNvQHb", payload, source_path=source_path)

        return all_new_turns

    def _retry_failed_media_for_conversation(self, jsonl_file, account_dir, media_dir, stats):
        if not Path(jsonl_file).exists():
            return {"attempted": 0, "recovered": 0, "failed": 0, "missingUrl": 0}

        rows = _read_jsonl_rows(jsonl_file)
        if not rows:
            return {"attempted": 0, "recovered": 0, "failed": 0, "missingUrl": 0}

        media_id_to_url = _build_media_id_to_url_map(account_dir)
        pending, recovered_existing = _scan_failed_media_from_rows(rows, media_dir, media_id_to_url)
        if not pending and not recovered_existing:
            return {"attempted": 0, "recovered": 0, "failed": 0, "missingUrl": 0}

        downloadable = [p for p in pending if isinstance(p.get("url"), str) and p["url"]]
        missing_url = [p for p in pending if not p.get("url")]

        retry_batch = [
            {
                "url": item["url"],
                "filepath": Path(media_dir) / item["media_id"],
                "media_id": item["media_id"],
                "media_type": _infer_media_type(item["media_id"]),
            }
            for item in downloadable
        ]
        failed_items = self.download_media_batch(retry_batch, media_dir, stats) if retry_batch else []

        failed_map = {item["media_id"]: (item.get("error") or "download_failed") for item in failed_items}
        for item in missing_url:
            failed_map[item["media_id"]] = "missing_manifest_url"

        attempted_ids = {item["media_id"] for item in downloadable}
        recovered_ids = set(recovered_existing) | (attempted_ids - set(failed_map.keys()))

        flag_stats = _update_jsonl_media_failure_flags(jsonl_file, failed_map, recovered_ids)

        return {
            "attempted": len(attempted_ids),
            "recovered": len(recovered_ids),
            "failed": len(failed_map),
            "missingUrl": len(missing_url),
            "flagMarked": flag_stats.get("marked", 0),
            "flagCleared": flag_stats.get("cleared", 0),
        }

    # ------------------------------------------------------------------
    # 主导出流程
    # ------------------------------------------------------------------
    def export_list_only(self, output_dir=None, stop_on_unchanged: bool = False):
        """
        仅同步会话列表（分页），不拉取对话详情。
        规则：
        - 按 cursor 连续分页拉取，异常时记录当前 cursor 并标记失败
        - 正常拉完后 cursor 清空
        - 本地索引始终做并集更新，不强制覆盖已有落盘数据
        - stop_on_unchanged=True：命中首个本地已有且时间戳相同的会话时提前终止，与增量逻辑一致
        """
        base_dir = Path(output_dir) if output_dir else OUTPUT_DIR
        base_dir.mkdir(parents=True, exist_ok=True)

        account_info = self._resolve_account_info()
        account_id = account_info["id"]
        account_dir = base_dir / "accounts" / account_id
        conv_dir = account_dir / "conversations"
        media_dir = account_dir / "media"

        account_dir.mkdir(parents=True, exist_ok=True)
        conv_dir.mkdir(parents=True, exist_ok=True)
        media_dir.mkdir(parents=True, exist_ok=True)
        self._set_request_state_scope(account_dir)

        print(f"[*] 账号: {mask_email(account_info['email']) or account_id}")
        print(f"[*] 仅同步列表到: {account_dir.absolute()}")

        existing_order, existing_index = _load_conversations_index(account_dir)
        sync_state = _load_sync_state(account_dir)
        full_sync = sync_state.get("fullSync") if isinstance(sync_state, dict) else None

        def _normalize_id_list(raw):
            if not isinstance(raw, list):
                return []
            out = []
            seen = set()
            for cid in raw:
                if not isinstance(cid, str):
                    continue
                normalized = cid.strip()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                out.append(normalized)
            return out

        started_at = datetime.datetime.now(datetime.UTC).isoformat()
        baseline_existing_ids = _normalize_id_list(existing_order)
        fetched_order = []
        resume_cursor = None

        if isinstance(full_sync, dict) and full_sync.get("phase") == "listing":
            started_at = full_sync.get("startedAt") or started_at
            cursor_candidate = full_sync.get("listingCursor")
            if isinstance(cursor_candidate, str) and cursor_candidate:
                resume_cursor = cursor_candidate
            fetched_order = _normalize_id_list(full_sync.get("listingFetchedIds"))
            baseline_candidate = _normalize_id_list(full_sync.get("baselineIds"))
            if baseline_candidate:
                baseline_existing_ids = baseline_candidate

        conv_index = dict(existing_index)
        fetched_seen = set()
        for cid in fetched_order:
            fetched_seen.add(cid)

        if resume_cursor:
            print("[*] 检测到上次列表同步中断，继续从 cursor 拉取...")
        else:
            if self._request_consecutive_failures > 0:
                print("[*] 检测到列表请求连续失败，按失败计数从第一页重试...")
            else:
                print("[*] 从第一页开始拉取列表...")
            fetched_order = []
            fetched_seen = set()

        def _build_partial_summaries():
            summaries = []
            seen = set()
            for cid in fetched_order:
                summary = conv_index.get(cid) or existing_index.get(cid)
                if not isinstance(summary, dict) or cid in seen:
                    continue
                summaries.append(summary)
                seen.add(cid)
            for cid in baseline_existing_ids:
                if cid in seen:
                    continue
                summary = existing_index.get(cid)
                if not isinstance(summary, dict):
                    continue
                summaries.append(summary)
                seen.add(cid)
            return summaries

        def _persist_state(phase, cursor, error=None, stopped_early=False):
            now_iso = datetime.datetime.now(datetime.UTC).isoformat()
            remote_count = len(fetched_order)
            lost_count = 0
            failure_count = self._request_consecutive_failures
            backoff_ms = self._request_backoff_ms()

            if phase == "done":
                if stopped_early:
                    # 提前终止：未扫描的旧会话仍存在，直接用并集写盘，不标记 lost
                    summaries = _build_partial_summaries()
                else:
                    summaries = []
                    remote_set = set()
                    for cid in fetched_order:
                        summary = conv_index.get(cid) or existing_index.get(cid)
                        if not isinstance(summary, dict):
                            continue
                        summaries.append(summary)
                        remote_set.add(cid)
                    for cid in baseline_existing_ids:
                        if cid in remote_set:
                            continue
                        summaries.append(_build_lost_summary(cid, existing_index.get(cid)))
                        lost_count += 1
                listing_cursor = None
                listing_fetched_ids = []
            else:
                summaries = _build_partial_summaries()
                listing_cursor = cursor
                listing_fetched_ids = list(fetched_order)

            current_state = _load_sync_state(account_dir)
            pending_conversations = (
                current_state.get("pendingConversations")
                if isinstance(current_state, dict) else []
            )
            if not isinstance(pending_conversations, list):
                pending_conversations = []

            _write_conversations_index(account_dir, account_id, now_iso, summaries)
            _write_sync_state(account_dir, {
                "version": 1,
                "accountId": account_id,
                "updatedAt": now_iso,
                "requestState": self._current_request_state(now_iso),
                "concurrency": 1,
                "fullSync": {
                    "phase": phase,
                    "startedAt": started_at,
                    "listingCursor": listing_cursor,
                    "listingTotal": remote_count if phase == "done" else None,
                    "listingFetched": remote_count,
                    "listingFetchedIds": listing_fetched_ids,
                    "listingConsecutiveFailures": failure_count,
                    "listingBackoffMs": backoff_ms,
                    "conversationsToFetch": [],
                    "conversationsFetched": 0,
                    "conversationsFailed": [],
                    "completedAt": now_iso if phase == "done" else None,
                    "errorMessage": error,
                    "baselineIds": baseline_existing_ids,
                    "lostCount": lost_count if phase == "done" else None,
                },
                "pendingConversations": pending_conversations,
            })

            account_info["conversationCount"] = len(summaries)
            if phase == "done":
                account_info["remoteConversationCount"] = remote_count
                account_info["lastSyncResult"] = "success"
            elif error:
                account_info["lastSyncResult"] = "partial" if summaries else "failed"
            else:
                account_info["lastSyncResult"] = "partial" if summaries else account_info.get("lastSyncResult")
            account_info["lastSyncAt"] = now_iso
            _write_accounts_json(base_dir, account_info)
            _write_account_meta(account_dir, account_info)
            return {"remoteCount": remote_count, "lostCount": lost_count}

        updated_ids: list[str] = []
        stop_early = False
        cursor = resume_cursor
        page = 0
        while True:
            page += 1
            try:
                chats, next_cursor = self.get_chats_page(cursor)
            except Exception as e:
                _persist_state("listing", cursor, str(e))
                raise

            if not chats and not next_cursor:
                result = _persist_state("done", None, None)
                if result["lostCount"] > 0:
                    print(f"  [lost] 标记已丢失会话: {result['lostCount']} 个")
                print("[*] 列表同步完成（无更多分页）")
                break

            for chat in chats:
                bare_id = str(chat.get("id", "")).replace("c_", "")
                if not bare_id:
                    continue
                existing = conv_index.get(bare_id) or existing_index.get(bare_id)
                conv_index[bare_id] = _build_summary_from_chat_listing(chat, existing)
                if bare_id not in fetched_seen:
                    fetched_seen.add(bare_id)
                    fetched_order.append(bare_id)

                remote_ts = chat.get("latest_update_ts")
                local_ts = _summary_to_epoch_seconds(existing_index.get(bare_id))
                if isinstance(remote_ts, int) and isinstance(local_ts, int):
                    if int(remote_ts) > int(local_ts):
                        updated_ids.append(bare_id)
                    elif stop_on_unchanged and int(remote_ts) == int(local_ts):
                        print(f"  [stop] 命中未更新会话，停止列表扫描: {bare_id}")
                        stop_early = True
                        break
                # remote_ts 或 local_ts 无法提取时不加入 updated_ids；
                # 新会话由 sync_new 处理，无内容会话由 sync_empty 处理。

            if stop_early:
                result = _persist_state("done", None, stopped_early=True)
                print(f"  第 {page} 页: {len(chats)} 个对话 (累计 {result['remoteCount']}, 提前终止)")
                break

            phase = "done" if not next_cursor else "listing"
            result = _persist_state(phase, next_cursor, None)
            print(f"  第 {page} 页: {len(chats)} 个对话 (累计 {result['remoteCount']})")

            if not next_cursor:
                if result["lostCount"] > 0:
                    print(f"  [lost] 标记已丢失会话: {result['lostCount']} 个")
                print("[*] 列表同步完成")
                break

            cursor = next_cursor

        return {"updatedIds": updated_ids}

    def sync_single_conversation(self, conversation_id, output_dir=None):
        """
        同步单个会话详情（含媒体），并更新该账号本地索引。

        复用现有 hNvQHb / 媒体下载 / JSONL 输出逻辑，不引入新协议。
        调用前需先完成 init_auth（由上层统一初始化）。
        """
        base_dir = Path(output_dir) if output_dir else OUTPUT_DIR
        base_dir.mkdir(parents=True, exist_ok=True)

        account_info = self._resolve_account_info()
        account_id = account_info["id"]
        account_dir = base_dir / "accounts" / account_id
        conv_dir = account_dir / "conversations"
        media_dir = account_dir / "media"
        account_dir.mkdir(parents=True, exist_ok=True)
        conv_dir.mkdir(parents=True, exist_ok=True)
        media_dir.mkdir(parents=True, exist_ok=True)
        self._set_request_state_scope(account_dir)
        conv_start = time.perf_counter()

        conv_id = normalize_chat_id(conversation_id)
        bare_id = conv_id.replace("c_", "")
        jsonl_file = conv_dir / f"{bare_id}.jsonl"
        tmp_turns_file = conv_dir / f".tmp_{bare_id}.turns.json"
        local_jsonl_exists = jsonl_file.exists()
        detail_mode = "incremental" if local_jsonl_exists else "full"
        pre_sync_media_stats = {
            "media_downloaded": 0,
            "media_failed": 0,
        }

        print(f"[*] 账号: {mask_email(account_info['email']) or account_id}")
        print(f"[*] 同步单会话: {conv_id}")

        retry_stats = self._retry_failed_media_for_conversation(
            jsonl_file=jsonl_file,
            account_dir=account_dir,
            media_dir=media_dir,
            stats=pre_sync_media_stats,
        )
        if retry_stats["attempted"] > 0 or retry_stats["missingUrl"] > 0:
            print(
                "  [media-retry] 历史失败媒体重试:"
                f" attempted={retry_stats['attempted']},"
                f" recovered={retry_stats['recovered']},"
                f" failed={retry_stats['failed']},"
                f" missing_url={retry_stats['missingUrl']}"
            )

        _, existing_index = _load_conversations_index(account_dir)
        existing_summary = existing_index.get(bare_id, {}) if isinstance(existing_index, dict) else {}
        existing_status = _normalize_conversation_status(
            existing_summary.get("status"),
            CONVERSATION_STATUS_NORMAL,
        )

        latest_update_ts = _coerce_epoch_seconds(existing_summary.get("remoteHash"))

        chat_info = {
            "id": conv_id,
            "title": existing_summary.get("title", ""),
            "latest_update_ts": latest_update_ts,
            "latest_update_iso": existing_summary.get("updatedAt"),
        }
        title = chat_info.get("title") or bare_id
        chat_info["title"] = title

        def _safe_int(value, default=0):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def _updated_sort_num(iso_str):
            if not isinstance(iso_str, str) or not iso_str:
                return 0.0
            try:
                return datetime.datetime.fromisoformat(iso_str).timestamp()
            except Exception:
                return 0.0

        def _load_cached_turns():
            if not tmp_turns_file.exists():
                return []
            try:
                data = json.loads(tmp_turns_file.read_text(encoding="utf-8"))
            except Exception:
                return []
            return data if isinstance(data, list) else []

        def _load_sync_state_for_conversation():
            state = _load_sync_state(account_dir)
            if not isinstance(state, dict):
                state = {}
            pending = state.get("pendingConversations")
            if not isinstance(pending, list):
                pending = []
            state["pendingConversations"] = pending
            if "fullSync" not in state:
                state["fullSync"] = None
            state["version"] = _safe_int(state.get("version"), 1) or 1
            state["accountId"] = account_id
            return state

        def _persist_conversation_state(
            state,
            phase,
            detail_cursor,
            detail_mode_value,
            fetched_pages,
            fetched_turns,
            started_at,
            error_message=None,
        ):
            now_iso_local = datetime.datetime.now(datetime.UTC).isoformat()
            pending = state.get("pendingConversations", [])
            if not isinstance(pending, list):
                pending = []

            updated_pending = []
            for item in pending:
                if isinstance(item, dict) and item.get("id") == bare_id:
                    continue
                updated_pending.append(item)

            if phase != "done":
                relative_tmp_path = str(
                    Path("accounts") / account_id / "conversations" / tmp_turns_file.name
                )
                updated_pending.append({
                    "id": bare_id,
                    "phase": phase,
                    "detailMode": detail_mode_value,
                    "startedAt": started_at,
                    "updatedAt": now_iso_local,
                    "detailCursor": detail_cursor,
                    "detailFetchedPages": fetched_pages,
                    "detailFetchedTurns": fetched_turns,
                    "tempFile": relative_tmp_path,
                    "completedAt": None,
                    "errorMessage": error_message,
                })

            state["updatedAt"] = now_iso_local
            state["requestState"] = self._current_request_state(now_iso_local)
            state["pendingConversations"] = updated_pending
            _write_sync_state(account_dir, state)

        sync_state = _load_sync_state_for_conversation()
        pending_entry = next(
            (
                p for p in sync_state.get("pendingConversations", [])
                if isinstance(p, dict) and p.get("id") == bare_id and p.get("phase") != "done"
            ),
            None,
        )

        started_at = datetime.datetime.now(datetime.UTC).isoformat()
        cursor = None
        fetched_pages = 0
        raw_turns = []
        existing_turn_ids = _build_existing_turn_id_set_new(jsonl_file) if local_jsonl_exists else set()

        if pending_entry:
            started_at = pending_entry.get("startedAt") or started_at
            mode_candidate = pending_entry.get("detailMode")
            if mode_candidate in {"full", "incremental"}:
                detail_mode = mode_candidate
            cursor_candidate = pending_entry.get("detailCursor")
            if isinstance(cursor_candidate, str) and cursor_candidate:
                cursor = cursor_candidate
            fetched_pages = _safe_int(pending_entry.get("detailFetchedPages"), 0)
            raw_turns = _load_cached_turns()
            print("[*] 检测到未完成单会话同步，继续断点拉取...")
        else:
            if detail_mode == "incremental":
                print("[*] 本地已存在会话详情，执行增量同步...")
            else:
                print("[*] 本地无会话详情，执行全量拉取...")

        base_message_count = 0
        if detail_mode == "incremental" and local_jsonl_exists:
            base_message_count = _count_message_rows_new(jsonl_file)

        progress_index = dict(existing_index)

        def _persist_progress_summary(parsed_turn_count):
            now_iso_local = datetime.datetime.now(datetime.UTC).isoformat()
            incremental_base = base_message_count if detail_mode == "incremental" and local_jsonl_exists else 0
            progress_count = max(0, incremental_base + parsed_turn_count * 2)
            progress_remote_ts = _coerce_epoch_seconds(chat_info.get("latest_update_ts"))
            progress_remote_iso = _to_iso_utc(progress_remote_ts) if progress_remote_ts is not None else None

            progress_summary = dict(existing_summary) if isinstance(existing_summary, dict) else {}
            if not progress_summary:
                progress_summary = {
                    "id": bare_id,
                    "title": title,
                    "lastMessage": "",
                    "messageCount": 0,
                    "hasMedia": False,
                    "hasFailedData": False,
                    "imageCount": 0,
                    "videoCount": 0,
                    "updatedAt": progress_remote_iso or chat_info.get("latest_update_iso"),
                    "remoteHash": str(progress_remote_ts) if progress_remote_ts is not None else None,
                    "status": existing_status,
                }
            progress_summary["id"] = bare_id
            progress_summary["title"] = progress_summary.get("title") or title
            progress_summary["messageCount"] = progress_count
            progress_summary["imageCount"] = _safe_int(progress_summary.get("imageCount"), 0)
            if progress_summary["imageCount"] < 0:
                progress_summary["imageCount"] = 0
            progress_summary["videoCount"] = _safe_int(progress_summary.get("videoCount"), 0)
            if progress_summary["videoCount"] < 0:
                progress_summary["videoCount"] = 0
            progress_summary["hasFailedData"] = bool(progress_summary.get("hasFailedData", False))
            if progress_remote_ts is not None:
                progress_summary["updatedAt"] = progress_remote_iso
                progress_summary["remoteHash"] = str(progress_remote_ts)
            elif not progress_summary.get("updatedAt") and chat_info.get("latest_update_iso"):
                progress_summary["updatedAt"] = chat_info.get("latest_update_iso")
            progress_summary["status"] = _normalize_conversation_status(
                progress_summary.get("status"),
                existing_status,
            )

            progress_index[bare_id] = progress_summary
            progress_summaries = list(progress_index.values())
            progress_summaries.sort(key=lambda s: _updated_sort_num(s.get("updatedAt")), reverse=True)
            _write_conversations_index(account_dir, account_id, now_iso_local, progress_summaries)

        fetch_start = time.perf_counter()
        try:
            while True:
                page_start = time.perf_counter()
                fetched_pages += 1
                turns, next_cursor = self.get_chat_detail_page(conv_id, cursor)
                if not turns and not next_cursor:
                    break

                hit_existing = False
                page_turns = []
                if detail_mode == "incremental":
                    for turn in turns:
                        tid = _turn_id_from_raw(turn)
                        if tid and tid in existing_turn_ids:
                            hit_existing = True
                            break
                        page_turns.append(turn)
                else:
                    page_turns = turns

                raw_turns.extend(page_turns)
                cursor = next_cursor

                tmp_turns_file.write_text(
                    json.dumps(raw_turns, ensure_ascii=False), encoding="utf-8"
                )
                _persist_conversation_state(
                    sync_state,
                    phase="downloading",
                    detail_cursor=cursor,
                    detail_mode_value=detail_mode,
                    fetched_pages=fetched_pages,
                    fetched_turns=len(raw_turns),
                    started_at=started_at,
                    error_message=None,
                )
                _persist_progress_summary(len(raw_turns))
                page_ms = (time.perf_counter() - page_start) * 1000
                print(f"  第 {fetched_pages} 页: {len(page_turns)} 轮 (累计 {len(raw_turns)}) {page_ms:.0f}ms")

                if hit_existing or not next_cursor:
                    break
        except Exception as e:
            try:
                tmp_turns_file.write_text(
                    json.dumps(raw_turns, ensure_ascii=False), encoding="utf-8"
                )
                _persist_conversation_state(
                    sync_state,
                    phase="downloading",
                    detail_cursor=cursor,
                    detail_mode_value=detail_mode,
                    fetched_pages=fetched_pages,
                    fetched_turns=len(raw_turns),
                    started_at=started_at,
                    error_message=str(e),
                )
            except Exception:
                pass
            raise

        raw_turns, removed_turns = _dedupe_raw_turns_by_id(raw_turns)
        if removed_turns > 0:
            print(f"  [dedupe] 当前会话分页结果去重: {removed_turns} 个重复 turn")

        fetch_elapsed = time.perf_counter() - fetch_start
        media_start = time.perf_counter()

        global_seen_urls = _load_media_manifest_new(account_dir)
        global_used_names = set(global_seen_urls.values())
        for f in media_dir.iterdir():
            if f.is_file():
                global_used_names.add(f.name)

        media_stats = {
            "media_downloaded": pre_sync_media_stats["media_downloaded"],
            "media_failed": pre_sync_media_stats["media_failed"],
            "preview_generated": 0,
            "preview_failed": 0,
        }
        merged = dict(existing_index)

        if detail_mode == "incremental" and local_jsonl_exists:
            if raw_turns:
                parsed_new_turns = [parse_turn(turn) for turn in raw_turns]
                parsed_new_turns = normalize_turn_media_first_seen(parsed_new_turns)

                batch_list = self._assign_media_ids_and_collect_downloads(
                    parsed_new_turns, media_dir, global_seen_urls, global_used_names,
                )
                new_rows_full = _turns_to_jsonl_rows(parsed_new_turns, conv_id, account_id, title, chat_info)
                new_meta = new_rows_full[0]
                new_msg_rows = new_rows_full[1:]

                existing_msg_rows = []
                if jsonl_file.exists():
                    with open(jsonl_file, "r", encoding="utf-8") as fh:
                        for i, line in enumerate(fh):
                            if i == 0:
                                continue
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                existing_msg_rows.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue

                merged_msg_rows, removed_msg_rows = _merge_message_rows_for_write(
                    new_msg_rows, existing_msg_rows
                )
                if removed_msg_rows > 0:
                    print(f"  [dedupe] 合并写盘去重: {removed_msg_rows} 行")
                all_rows = [new_meta] + merged_msg_rows
                _write_jsonl_rows(jsonl_file, all_rows)

                failed_items = []
                if batch_list:
                    print(f"  媒体文件: {len(batch_list)} 个（去重后）")
                    failed_items = self.download_media_batch(batch_list, media_dir, media_stats)
                    _save_media_manifest_new(account_dir, global_seen_urls)
                preview_stats = _ensure_video_previews_from_turns(parsed_new_turns, media_dir)
                media_stats["preview_generated"] += preview_stats["preview_generated"]
                media_stats["preview_failed"] += preview_stats["preview_failed"]

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
                display_rows = _filter_display_rows(all_msg_rows)
                has_media = any(r.get("attachments") for r in display_rows)
                has_failed_data = _rows_has_failed_data(display_rows)
                image_count, video_count, _audio_count = _count_media_types_from_rows(display_rows)
                last_text = ""
                for r in reversed(display_rows):
                    if r.get("text"):
                        last_text = r["text"][:80]
                        break

                summary = {
                    "id": bare_id,
                    "title": title,
                    "lastMessage": last_text,
                    "messageCount": len(display_rows),
                    "hasMedia": has_media,
                    "hasFailedData": has_failed_data,
                    "imageCount": image_count,
                    "videoCount": video_count,
                    "updatedAt": meta_row.get("updatedAt"),
                    "remoteHash": meta_row.get("remoteHash"),
                    "status": existing_status,
                }
            else:
                remote_ts = _coerce_epoch_seconds(chat_info.get("latest_update_ts"))
                remote_iso = _to_iso_utc(remote_ts) if remote_ts is not None else None
                summary = dict(existing_summary) if isinstance(existing_summary, dict) else {}
                if not summary:
                    summary = {
                        "id": bare_id,
                        "title": title,
                        "lastMessage": "",
                        "messageCount": 0,
                        "hasMedia": False,
                        "hasFailedData": False,
                        "imageCount": 0,
                        "videoCount": 0,
                        "updatedAt": remote_iso or chat_info.get("latest_update_iso"),
                        "remoteHash": str(remote_ts) if remote_ts is not None else None,
                        "status": existing_status,
                    }
                summary["id"] = bare_id
                summary["title"] = summary.get("title") or title
                image_count = summary.get("imageCount")
                if not isinstance(image_count, int) or image_count < 0:
                    image_count = 0
                summary["imageCount"] = image_count
                video_count = summary.get("videoCount")
                if not isinstance(video_count, int) or video_count < 0:
                    video_count = 0
                summary["videoCount"] = video_count
                if remote_ts is not None:
                    summary["updatedAt"] = remote_iso
                    summary["remoteHash"] = str(remote_ts)
                elif chat_info.get("latest_update_iso") and not summary.get("updatedAt"):
                    summary["updatedAt"] = chat_info.get("latest_update_iso")
                summary["hasFailedData"] = bool(summary.get("hasFailedData", False))
        else:
            print(f"  轮次: {len(raw_turns)}")
            parsed_turns = [parse_turn(turn) for turn in raw_turns]
            parsed_turns = normalize_turn_media_first_seen(parsed_turns)

            batch_list = self._assign_media_ids_and_collect_downloads(
                parsed_turns, media_dir, global_seen_urls, global_used_names,
            )
            rows = _turns_to_jsonl_rows(parsed_turns, conv_id, account_id, title, chat_info)
            _write_jsonl_rows(jsonl_file, rows)

            failed_items = []
            if batch_list:
                print(f"  媒体文件: {len(batch_list)} 个（去重后）")
                failed_items = self.download_media_batch(batch_list, media_dir, media_stats)
                _save_media_manifest_new(account_dir, global_seen_urls)
            preview_stats = _ensure_video_previews_from_turns(parsed_turns, media_dir)
            media_stats["preview_generated"] += preview_stats["preview_generated"]
            media_stats["preview_failed"] += preview_stats["preview_failed"]

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

            rows_after = _read_jsonl_rows(jsonl_file)
            meta_row = (
                next(
                    (r for r in rows_after if isinstance(r, dict) and r.get("type") == "meta"),
                    None,
                )
                or (rows[0] if rows else {})
            )
            msg_rows = [
                r for r in rows_after if isinstance(r, dict) and r.get("type") == "message"
            ]
            display_rows = _filter_display_rows(msg_rows)
            has_media = any(r.get("attachments") for r in display_rows)
            has_failed_data = _rows_has_failed_data(display_rows)
            image_count, video_count, _audio_count = _count_media_types_from_rows(display_rows)
            last_text = ""
            for r in reversed(display_rows):
                if r.get("text"):
                    last_text = r["text"][:80]
                    break

            summary = {
                "id": bare_id,
                "title": title,
                "lastMessage": last_text,
                "messageCount": len(display_rows),
                "hasMedia": has_media,
                "hasFailedData": has_failed_data,
                "imageCount": image_count,
                "videoCount": video_count,
                "updatedAt": meta_row.get("updatedAt") or chat_info.get("latest_update_iso"),
                "remoteHash": meta_row.get("remoteHash"),
                "status": existing_status,
            }

        summary["status"] = _normalize_conversation_status(
            summary.get("status"),
            existing_status,
        )
        merged[bare_id] = summary
        summaries = list(merged.values())
        summaries.sort(key=lambda s: _updated_sort_num(s.get("updatedAt")), reverse=True)

        now_iso = datetime.datetime.now(datetime.UTC).isoformat()
        account_info["conversationCount"] = len(summaries)
        account_info["remoteConversationCount"] = max(
            len(summaries),
            account_info.get("remoteConversationCount") or 0,
        )
        account_info["lastSyncAt"] = now_iso
        account_info["lastSyncResult"] = "success"

        _write_accounts_json(base_dir, account_info)
        _write_account_meta(account_dir, account_info)
        _write_conversations_index(account_dir, account_id, now_iso, summaries)
        _persist_conversation_state(
            sync_state,
            phase="done",
            detail_cursor=None,
            detail_mode_value=detail_mode,
            fetched_pages=fetched_pages,
            fetched_turns=len(raw_turns),
            started_at=started_at,
            error_message=None,
        )

        if tmp_turns_file.exists():
            try:
                tmp_turns_file.unlink()
            except OSError:
                pass

        media_elapsed = time.perf_counter() - media_start
        total_elapsed = time.perf_counter() - conv_start
        img = summary.get("imageCount", 0)
        vid = summary.get("videoCount", 0)
        print(
            f"[*] 单会话完成: turns={len(raw_turns)}"
            f" media={img}(img)/{vid}(vid)"
            f" text={fetch_elapsed:.1f}s media_dl={media_elapsed:.1f}s total={total_elapsed:.1f}s"
        )



if __name__ == "__main__":
    from gemini_export_cli import main
    main()
