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
import codecs
import uuid
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlencode, quote, urlparse, parse_qsl, urlunparse, urljoin

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

# ============================================================================
# 配置
# ============================================================================
GEMINI_BASE = "https://gemini.google.com"
OUTPUT_DIR = Path("gemini_export_output")
BATCH_SIZE = 20          # MaZiqc 每页数量
DETAIL_PAGE_SIZE = 10    # hNvQHb 每页数量
# 随机请求间隔(秒): 总范围 300~600ms，区间内分布更集中
REQUEST_DELAY = 0.30
REQUEST_JITTER_MIN = 0.00
REQUEST_JITTER_MAX = 0.30
REQUEST_JITTER_MODE = 0.14
REQUEST_BACKOFF_MAX_SECONDS = 120.0
REQUEST_BACKOFF_LIMIT_FAILURES = 10
REQUEST_REINIT_BACKOFF_SECONDS = 4.0
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
BROWSER_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"

GOOGLE_MEDIA_COOKIE_NAMES = [
    "AEC", "__Secure-BUCKET", "SID", "__Secure-1PSID", "__Secure-3PSID",
    "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PAPISID",
    "__Secure-3PAPISID", "NID", "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "GOOGLE_ABUSE_EXEMPTION", "SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC",
]

PROTECTED_MEDIA_HOSTS = {
    "lh3.google.com",
    "lh3.googleusercontent.com",
    "contribution.usercontent.google.com",
}

INTERNAL_PLACEHOLDER_PATH_RE = re.compile(r"(?:^|/)[a-z0-9_]+_content(?:/|$)")


class RequestBackoffLimitReachedError(RuntimeError):
    """请求连续失败触发最终退避兜底时抛出。"""
    pass


def timing_log(action: str, start_perf: float, **fields) -> None:
    elapsed_ms = (time.perf_counter() - start_perf) * 1000.0
    detail = " ".join(f"{k}={v}" for k, v in fields.items())
    if detail:
        print(f"  [timing] {action} {detail} elapsed={elapsed_ms:.1f}ms")
    else:
        print(f"  [timing] {action} elapsed={elapsed_ms:.1f}ms")


def _is_internal_placeholder_content_url(url_text):
    if not isinstance(url_text, str):
        return False
    candidate = url_text.strip().rstrip("。.,;，；）)]}\"'")
    if not candidate.startswith(("https://", "http://")):
        return False

    try:
        parsed = urlparse(candidate)
    except Exception:
        return False

    host = (parsed.hostname or "").lower()
    if not (host == "googleusercontent.com" or host.endswith(".googleusercontent.com")):
        return False

    path = (parsed.path or "").lower()
    return bool(INTERNAL_PLACEHOLDER_PATH_RE.search(path))


def _contains_internal_placeholder_content_url(text_line):
    if not isinstance(text_line, str) or not text_line:
        return False
    urls = re.findall(r"https?://\S+", text_line)
    for url_text in urls:
        if _is_internal_placeholder_content_url(url_text):
            return True
    return False


def sanitize_generation_placeholder_text(text, has_attachments):
    """
    在已提取到附件时移除旧占位 URL 文本，避免污染 assistant 正文。
    """
    if not isinstance(text, str):
        return text
    if "_content/" not in text or "googleusercontent.com" not in text:
        return text

    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _contains_internal_placeholder_content_url(stripped):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _normalize_cookie_domain(domain):
    return (domain or "").lower().lstrip(".")


def _is_google_domain(domain):
    norm = _normalize_cookie_domain(domain)
    return norm == "google.com" or norm.endswith(".google.com")


def _cookie_domain_priority(domain):
    """优先选择 .google.com，其次 *.google.com，最后其他域。"""
    norm = _normalize_cookie_domain(domain)
    if norm == "google.com":
        return 0
    if norm.endswith(".google.com"):
        return 1
    return 9


def _select_preferred_google_cookies(cookie_items):
    """
    从 cookie 列表中选择更稳定的 Google 会话值。

    cookie_items 支持两种结构：
    - dict: {"name": ..., "value": ..., "domain": ...}
    - Cookie 对象: .name/.value/.domain
    """
    selected = {}
    selected_priority = {}

    for item in cookie_items:
        if isinstance(item, dict):
            name = item.get("name")
            value = item.get("value")
            domain = item.get("domain", "")
        else:
            name = getattr(item, "name", None)
            value = getattr(item, "value", None)
            domain = getattr(item, "domain", "")

        if not name or value is None or not _is_google_domain(domain):
            continue

        prio = _cookie_domain_priority(domain)
        prev_prio = selected_priority.get(name)
        if prev_prio is None or prio < prev_prio:
            selected[name] = value
            selected_priority[name] = prio

    return selected


def get_cookies_from_local_browser():
    """优先从本机常用浏览器读取 Google/Gemini cookies"""
    print("[*] 尝试从本机浏览器读取 cookies...")

    key_cookies = {"__Secure-1PSID", "__Secure-1PSIDTS"}
    cookie_loaders = [
        ("Chrome", getattr(browser_cookie3, "chrome", None)),
        ("Chromium", getattr(browser_cookie3, "chromium", None)),
        ("Brave", getattr(browser_cookie3, "brave", None)),
        ("Edge", getattr(browser_cookie3, "edge", None)),
    ]

    for browser_name, loader in cookie_loaders:
        if loader is None:
            continue

        try:
            collected_items = []
            for domain_name in [".google.com", "accounts.google.com", "gemini.google.com"]:
                jar = loader(domain_name=domain_name)
                for c in jar:
                    collected_items.append(c)

            collected = _select_preferred_google_cookies(collected_items)

            if not collected:
                print(f"  - {browser_name}: 未读取到可用 cookie")
                continue

            found = [k for k in key_cookies if k in collected]
            if found:
                print(f"  - {browser_name}: 成功读取 {len(collected)} 个 cookies")
                return collected

            print(f"  - {browser_name}: 已读取 {len(collected)} 个 cookies，但缺少关键登录态")
        except Exception as e:
            print(f"  - {browser_name}: 读取失败 ({e})")

    return {}


# ============================================================================
# batchexecute 响应解析
# ============================================================================
def parse_batchexecute_response(resp_text):
    """解析 Google batchexecute 响应格式，返回 [(rpcid, data), ...]"""
    body = resp_text
    if body.startswith(")]}'"):
        body = body[body.index('\n') + 1:]
    body = body.lstrip('\n\r')

    items = []
    # 解析长度前缀的 JSON 块
    pos = 0
    while pos < len(body):
        while pos < len(body) and body[pos] in ' \t\r\n':
            pos += 1
        if pos >= len(body):
            break
        nl = body.find('\n', pos)
        if nl == -1:
            break
        try:
            length = int(body[pos:nl])
        except ValueError:
            break
        pos = nl + 1
        chunk = body[pos:pos + length]
        pos += length

        # chunk 可能包含多行 JSON
        for line_data in chunk.split('\n'):
            line_data = line_data.strip()
            if not line_data:
                continue
            try:
                parsed = json.loads(line_data)
                if isinstance(parsed, list):
                    for item in parsed:
                        if (isinstance(item, list) and len(item) >= 3
                                and item[0] == 'wrb.fr'
                                and isinstance(item[2], str)):
                            rpcid = item[1]
                            inner = json.loads(item[2])
                            items.append((rpcid, inner))
            except (json.JSONDecodeError, IndexError):
                pass

    return items


def discover_email_authuser_mapping_via_listaccounts(cookies):
    """
    使用 ListAccounts 接口获取邮箱与 authuser 映射。

    该接口当前依赖请求上下文，缺少 Origin 往往会返回 HTTP 400。
    """
    list_accounts_url = "https://accounts.google.com/ListAccounts"
    params = {
        "authuser": "0",
        "listPages": "1",
        "fwput": "10",
        "rdr": "2",
        "pid": "658",
        "gpsia": "1",
        "source": "ogb",
        "atic": "1",
        "mo": "1",
        "mn": "1",
        "hl": "zh-CN",
        "ts": "641",
    }
    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept-Language": BROWSER_ACCEPT_LANGUAGE,
        "Referer": f"{GEMINI_BASE}/app",
        "Origin": GEMINI_BASE,
    }

    with httpx.Client(cookies=cookies, follow_redirects=True, timeout=30.0, headers=headers) as client:
        resp = client.get(list_accounts_url, params=params)

    if resp.status_code != 200:
        raise RuntimeError(f"ListAccounts HTTP {resp.status_code}")

    m = re.search(r"postMessage\('(.+?)'\s*,\s*'[^']*'\)", resp.text, flags=re.S)
    if not m:
        raise RuntimeError("ListAccounts 响应缺少 postMessage payload")

    payload_raw = m.group(1).replace("\\/", "/")
    payload = codecs.decode(payload_raw, "unicode_escape")
    parsed = json.loads(payload)

    rows = parsed[1] if isinstance(parsed, list) and len(parsed) > 1 and isinstance(parsed[1], list) else []
    result = []
    seen_email = set()
    for row in rows:
        if not isinstance(row, list) or len(row) < 4:
            continue

        email = row[3] if isinstance(row[3], str) else ""
        if not email:
            continue
        email = email.strip().lower()
        if email in seen_email:
            continue
        seen_email.add(email)

        authuser_raw = row[7] if len(row) > 7 else None
        authuser = None
        if isinstance(authuser_raw, int):
            authuser = str(authuser_raw)
        elif isinstance(authuser_raw, str) and authuser_raw.isdigit():
            authuser = authuser_raw

        redirect_url = None
        if authuser is not None:
            redirect_url = f"{GEMINI_BASE}/app" if authuser == "0" else f"{GEMINI_BASE}/u/{authuser}/app"

        result.append({
            "email": email,
            "authuser": authuser,
            "redirect_url": redirect_url,
        })

    return result


def discover_email_authuser_mapping(cookies):
    """通过 ListAccounts 获取本地 cookies 下的账号映射。"""
    return discover_email_authuser_mapping_via_listaccounts(cookies)


# ============================================================================
# 主导出类
# ============================================================================
class GeminiExporter:
    CONVERSATION_STATUS_NORMAL = "normal"
    CONVERSATION_STATUS_LOST = "lost"
    CONVERSATION_STATUS_HIDDEN = "hidden"

    def __init__(self, cookies: dict, user=None, account_id=None, account_email=None, cookies_refresher=None):
        self.cookies = cookies
        self.user_spec = str(user).strip() if user is not None else None
        self.account_id_override = str(account_id).strip() if account_id else None
        if self.account_id_override == "":
            self.account_id_override = None
        self.account_email_override = str(account_email).strip().lower() if account_email else None
        if self.account_email_override == "":
            self.account_email_override = None
        self.authuser = None
        self._client_refresh_in_progress = False
        self._client_reinit_failure_mark = -1
        self._client_reinit_attempted_for_scope = False
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
        self._cookies_refresher = cookies_refresher

    @staticmethod
    def _collect_runtime_google_cookies(client):
        """
        从现有 client 的 CookieJar 提取可复用的 Google 会话 cookie。
        """
        if client is None:
            return {}
        jar = getattr(getattr(client, "cookies", None), "jar", None)
        if jar is None:
            return {}
        try:
            return _select_preferred_google_cookies(list(jar))
        except Exception:
            return {}

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

    def _reinit_http_client_and_auth(self, trigger_label, verbose=True):
        if self._client_refresh_in_progress:
            return False

        snapshot_failures = self._request_consecutive_failures
        snapshot_started = self._request_started
        snapshot_limit_probe_consumed = self._limit_probe_consumed
        old_client = self.client
        runtime_cookie_overrides = self._collect_runtime_google_cookies(old_client)
        candidate_cookies = dict(self.cookies or {})
        if runtime_cookie_overrides:
            candidate_cookies.update(runtime_cookie_overrides)

        fresh_cookies_for_commit = None
        if self._cookies_refresher is not None:
            try:
                fresh_cookies = self._cookies_refresher()
                if fresh_cookies:
                    candidate_cookies.update(fresh_cookies)
                    fresh_cookies_for_commit = dict(candidate_cookies)
            except Exception as _refresh_err:
                if verbose:
                    print(f"  [reinit] 浏览器 cookie 刷新失败，使用缓存 cookies: {_refresh_err}")

        new_client = self._create_http_client(cookies=candidate_cookies)
        self.client = new_client

        self._client_refresh_in_progress = True
        try:
            if verbose:
                print(
                    "  [reinit] 命中4s退避档，重建HTTP client并重新初始化认证:"
                    f" op={trigger_label}"
                )
            self.init_auth()
            if fresh_cookies_for_commit is not None:
                self.cookies = fresh_cookies_for_commit
            refreshed_cookie_overrides = self._collect_runtime_google_cookies(new_client)
            if refreshed_cookie_overrides:
                self.cookies.update(refreshed_cookie_overrides)
            if old_client is not None:
                try:
                    old_client.close()
                except Exception:
                    pass
            if verbose:
                print("  [reinit] 认证刷新完成，继续当前请求")
            return True
        except Exception as e:
            self.client = old_client
            try:
                new_client.close()
            except Exception:
                pass
            if verbose:
                print(f"  [reinit] 认证刷新失败，将继续尝试当前请求: {e}")
            return False
        finally:
            # reinit 不是业务请求，不应影响失败计数和重试控制状态。
            self._request_consecutive_failures = snapshot_failures
            self._request_started = snapshot_started
            self._limit_probe_consumed = snapshot_limit_probe_consumed
            self._sync_request_state_file()
            self._client_refresh_in_progress = False

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

    @staticmethod
    def _request_backoff_seconds(consecutive_failures):
        if consecutive_failures < 3:
            return 0.0
        if consecutive_failures < 6:
            return float(min(4, 2 ** (consecutive_failures - 3)))
        if consecutive_failures < 9:
            return float(min(32, 8 * (2 ** (consecutive_failures - 6))))
        return float(min(REQUEST_BACKOFF_MAX_SECONDS, 60 * (2 ** (consecutive_failures - 9))))

    def _request_backoff_ms(self):
        return int(round(self._request_backoff_seconds(self._request_consecutive_failures) * 1000))

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
        state = self._load_sync_state(account_dir)
        if not isinstance(state, dict):
            state = {}
        now_iso = datetime.datetime.now(datetime.UTC).isoformat()
        state["version"] = int(state.get("version") or 1)
        state["accountId"] = state.get("accountId") or account_dir.name
        state["updatedAt"] = now_iso
        state["requestState"] = self._current_request_state(now_iso)
        self._write_sync_state(account_dir, state)

    def _set_request_state_scope(self, account_dir):
        self._request_state_account_dir = Path(account_dir)
        # 新任务作用域开始时重置请求起始标记。
        self._request_started = False
        self._client_reinit_failure_mark = -1
        self._client_reinit_attempted_for_scope = False
        state = self._load_sync_state(self._request_state_account_dir)
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
        backoff_sec = self._request_backoff_seconds(self._request_consecutive_failures)
        if (
            not self._client_refresh_in_progress
            and not self._client_reinit_attempted_for_scope
            and abs(backoff_sec - REQUEST_REINIT_BACKOFF_SECONDS) < 1e-9
            and self._request_consecutive_failures != self._client_reinit_failure_mark
        ):
            self._client_reinit_attempted_for_scope = True
            self._client_reinit_failure_mark = self._request_consecutive_failures
            self._reinit_http_client_and_auth(label, verbose=verbose)
            backoff_sec = self._request_backoff_seconds(self._request_consecutive_failures)

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
        self._client_reinit_failure_mark = -1
        self._client_reinit_attempted_for_scope = False
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

    @staticmethod
    def email_to_account_id(email):
        """账号目录 ID：沿用原规则（邮箱小写后将非字母数字替换为下划线）。"""
        if not isinstance(email, str):
            email = str(email or "")
        normalized = email.strip().lower()
        return re.sub(r"[^a-z0-9]", "_", normalized)

    @staticmethod
    def _to_iso_utc(ts):
        if ts is None:
            return None
        try:
            return datetime.datetime.fromtimestamp(int(ts), datetime.UTC).isoformat()
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _coerce_epoch_seconds(value):
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            s = value.strip()
            if s.isdigit():
                return int(s)
        return None

    @staticmethod
    def _iso_to_epoch_seconds(iso_text):
        if not isinstance(iso_text, str):
            return None
        candidate = iso_text.strip()
        if not candidate:
            return None
        if candidate.endswith("Z"):
            candidate = f"{candidate[:-1]}+00:00"
        try:
            return int(datetime.datetime.fromisoformat(candidate).timestamp())
        except Exception:
            return None

    @staticmethod
    def _summary_to_epoch_seconds(summary):
        if not isinstance(summary, dict):
            return None
        remote_hash_ts = GeminiExporter._coerce_epoch_seconds(summary.get("remoteHash"))
        if remote_hash_ts is not None:
            return remote_hash_ts
        return GeminiExporter._iso_to_epoch_seconds(summary.get("updatedAt"))

    @staticmethod
    def _diagnose_auth_page(html, final_url):
        text = str(html or "").lower()
        url_text = str(final_url or "").lower()
        hints = []
        if "accounts.google.com" in url_text or "servicelogin" in text:
            hints.append("命中 Google 登录页")
        if "consent.google.com" in url_text:
            hints.append("命中 consent 页面")
        if "unusual traffic" in text or "/sorry/" in url_text:
            hints.append("可能触发异常流量风控")
        if "recaptcha" in text or "g-recaptcha" in text or "captcha" in text:
            hints.append("可能触发验证码挑战")
        if not hints:
            hints.append("页面结构变化或返回非 Gemini app 页面")
        return "；".join(hints)

    @staticmethod
    def normalize_chat_id(chat_id):
        """将外部传入的对话 ID 规范化为 c_xxx 形式。"""
        if not isinstance(chat_id, str):
            return chat_id
        cid = chat_id.strip()
        if not cid:
            return cid
        if cid.startswith("c_"):
            return cid
        return f"c_{cid}"

    @staticmethod
    def _extract_chat_latest_update(chat_item):
        """从聊天列表条目提取最新更新时间（秒级时间戳）"""
        if not isinstance(chat_item, list) or len(chat_item) <= 5:
            return None
        field = chat_item[5]
        if isinstance(field, list) and field and isinstance(field[0], int):
            return field[0]
        return None

    # ------------------------------------------------------------------
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
            diagnosis = self._diagnose_auth_page(html, final_url)
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
                latest_update_ts = self._extract_chat_latest_update(chat)
                items.append({
                    "id": conv_id,
                    "title": title,
                    "latest_update_ts": latest_update_ts,
                    "latest_update_iso": self._to_iso_utc(latest_update_ts),
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
                    return self._extract_chat_latest_update(chat)

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
            "previous_update_iso": self._to_iso_utc(previous_ts),
            "latest_update_ts": latest_ts,
            "latest_update_iso": self._to_iso_utc(latest_ts),
            "updated": updated,
            "found": latest_ts is not None,
        }

    # ------------------------------------------------------------------
    # 解析对话轮次
    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_http_url(value):
        return isinstance(value, str) and (
            value.startswith("https://") or value.startswith("http://")
        )

    @staticmethod
    def _is_media_descriptor(item):
        """
        判断一个 list 是否像 Gemini 媒体描述项（图片/视频）。
        """
        if not isinstance(item, list) or len(item) < 2:
            return False

        type_val = item[1]
        if type_val not in (1, 2):
            return False

        has_url = False
        if len(item) > 3 and GeminiExporter._looks_like_http_url(item[3]):
            has_url = True
        if not has_url and len(item) > 7 and isinstance(item[7], list):
            has_url = any(GeminiExporter._looks_like_http_url(u) for u in item[7])
        if not has_url:
            return False

        has_name = len(item) > 2 and isinstance(item[2], str) and "." in item[2]
        has_mime = len(item) > 11 and isinstance(item[11], str) and "/" in item[11]
        return has_name or has_mime

    @staticmethod
    def _collect_media_descriptors(node, out):
        if isinstance(node, list):
            if GeminiExporter._is_media_descriptor(node):
                out.append(node)
                return
            for child in node:
                GeminiExporter._collect_media_descriptors(child, out)

    @staticmethod
    def _media_descriptor_size_hint(item):
        if (
            isinstance(item, list)
            and len(item) > 15
            and isinstance(item[15], list)
            and len(item[15]) > 2
            and isinstance(item[15][2], int)
        ):
            return item[15][2]
        return 0

    @staticmethod
    def _pick_preferred_media_descriptor(items):
        valid = [it for it in items if GeminiExporter._is_media_descriptor(it)]
        if not valid:
            return None

        def _score(item):
            size_hint = GeminiExporter._media_descriptor_size_hint(item)
            mime = item[11] if len(item) > 11 and isinstance(item[11], str) else ""
            is_png = 1 if mime == "image/png" else 0
            return (size_hint, is_png)

        return max(valid, key=_score)

    @staticmethod
    def _collect_primary_media_descriptors(node, out):
        """
        处理 image_generation 的双格式结构（常见于同一图同时给 png/jpeg）。
        命中同层 3/6 槽位时只保留一份主资源，避免重复渲染。
        """
        if not isinstance(node, list):
            return

        if GeminiExporter._is_media_descriptor(node):
            out.append(node)
            return

        slot_candidates = []
        for idx in (3, 6):
            if len(node) > idx and isinstance(node[idx], list):
                item = node[idx]
                if GeminiExporter._is_media_descriptor(item):
                    slot_candidates.append(item)
        if slot_candidates:
            preferred = GeminiExporter._pick_preferred_media_descriptor(slot_candidates)
            if preferred is not None:
                out.append(preferred)
            return

        for child in node:
            GeminiExporter._collect_primary_media_descriptors(child, out)

    @staticmethod
    def _extract_ai_media_items(ai_data):
        """
        从 AI 候选结构中提取可下载的媒体描述项。
        """
        if not isinstance(ai_data, list):
            return []

        candidates = []
        if len(ai_data) > 12 and ai_data[12] is not None:
            GeminiExporter._collect_primary_media_descriptors(ai_data[12], candidates)
        if not candidates:
            GeminiExporter._collect_media_descriptors(ai_data, candidates)

        deduped = []
        seen = set()
        for item in candidates:
            parsed = GeminiExporter._parse_media_item(item, "assistant")
            url = parsed.get("url")
            if not url or _is_internal_placeholder_content_url(url):
                continue
            key = (url, parsed.get("filename"), parsed.get("mime"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        return deduped

    @staticmethod
    def parse_turn(turn):
        """解析单个对话轮次，返回结构化数据"""
        result = {
            "turn_id": None,
            "timestamp": None,
            "timestamp_iso": None,
            "user": {"text": "", "files": []},
            "assistant": {"text": "", "thinking": "", "model": "", "files": []},
        }

        try:
            # Turn IDs
            ids = turn[0]
            result["turn_id"] = ids[1] if len(ids) > 1 else ids[0]

            # Turn 时间（秒级时间戳）
            if len(turn) > 4 and isinstance(turn[4], list) and turn[4]:
                if isinstance(turn[4][0], int):
                    result["timestamp"] = turn[4][0]
                    result["timestamp_iso"] = GeminiExporter._to_iso_utc(turn[4][0])

            # === 用户消息 ===
            content = turn[2]
            msg = content[0]
            # 用户文本
            if isinstance(msg[0], str):
                result["user"]["text"] = msg[0]

            # 用户上传的文件 (content[0][4][0][3])
            if (len(msg) > 4 and msg[4] is not None
                    and isinstance(msg[4], list) and len(msg[4]) > 0
                    and isinstance(msg[4][0], list) and len(msg[4][0]) > 3
                    and msg[4][0][3] is not None):
                user_files = msg[4][0][3]
                for f in user_files:
                    if isinstance(f, list):
                        result["user"]["files"].append(
                            GeminiExporter._parse_media_item(f, "user")
                        )

            # === AI 回复 ===
            detail = turn[3]

            # 模型名称
            if len(detail) > 21 and isinstance(detail[21], str):
                result["assistant"]["model"] = detail[21]

            # AI 回复核心数据：优先使用当前选中候选（detail[3]），回退第一个候选
            ai_data = None
            selected_candidate_id = detail[3] if len(detail) > 3 and isinstance(detail[3], str) else None
            if isinstance(detail[0], list) and len(detail[0]) > 0:
                candidates = [c for c in detail[0] if isinstance(c, list)]
                if selected_candidate_id:
                    for c in candidates:
                        if len(c) > 0 and c[0] == selected_candidate_id:
                            ai_data = c
                            break
                if ai_data is None and candidates:
                    ai_data = candidates[0]

            user_media_keys = {
                (
                    f.get("url") or "",
                    f.get("filename") or "",
                    f.get("mime") or "",
                    f.get("type") or "",
                )
                for f in result["user"].get("files", [])
                if isinstance(f, dict)
            }

            ai_media_items = []
            if isinstance(ai_data, list):

                # AI 文本: ai_data[1][0]
                if (len(ai_data) > 1 and isinstance(ai_data[1], list)
                        and len(ai_data[1]) > 0 and isinstance(ai_data[1][0], str)):
                    result["assistant"]["text"] = ai_data[1][0]

                # AI 思考: ai_data[37][0][0]
                if (len(ai_data) > 37 and ai_data[37] is not None
                        and isinstance(ai_data[37], list) and len(ai_data[37]) > 0):
                    thinking = ai_data[37]
                    if isinstance(thinking[0], list) and len(thinking[0]) > 0:
                        if isinstance(thinking[0][0], str):
                            result["assistant"]["thinking"] = thinking[0][0]
                    elif isinstance(thinking[0], str):
                        result["assistant"]["thinking"] = thinking[0]
                # 优先从 AI 候选结构中提取生成媒体（含 image_generation_content 实际图）
                ai_media_items = GeminiExporter._extract_ai_media_items(ai_data)

            seen_ai = set()
            for f in ai_media_items:
                parsed = GeminiExporter._parse_media_item(f, "assistant")
                url = parsed.get("url")
                key = (
                    url or "",
                    parsed.get("filename") or "",
                    parsed.get("mime") or "",
                    parsed.get("type") or "",
                )
                if key in user_media_keys:
                    continue
                if key in seen_ai:
                    continue
                seen_ai.add(key)
                result["assistant"]["files"].append(parsed)

            asst_text = result["assistant"].get("text")
            result["assistant"]["text"] = sanitize_generation_placeholder_text(
                asst_text,
                has_attachments=bool(result["assistant"]["files"]),
            )

        except (IndexError, TypeError):
            pass

        return result

    @staticmethod
    def _parse_media_item(item, role):
        """解析单个媒体项目"""
        media = {
            "role": role,
            "type": "unknown",
            "filename": None,
            "mime": None,
            "url": None,
            "thumbnail_url": None,
        }

        try:
            # type: 1=image, 2=video
            type_val = item[1] if len(item) > 1 else None
            media["filename"] = item[2] if len(item) > 2 and isinstance(item[2], str) else None
            media["mime"] = item[11] if len(item) > 11 and isinstance(item[11], str) else None

            if type_val == 1:
                media["type"] = "image"
                # 图片直链在 item[3]
                if len(item) > 3 and isinstance(item[3], str):
                    media["url"] = item[3]
            elif type_val == 2:
                media["type"] = "video"
                # 视频 URL 在 item[7]
                if len(item) > 7 and isinstance(item[7], list):
                    urls = item[7]
                    # urls[0] = lh3 缩略图/预览
                    # urls[1] = contribution 下载链接 (用户上传)
                    # urls[2] = lh3 另一个链接
                    if len(urls) > 1 and isinstance(urls[1], str):
                        media["url"] = urls[1]  # 优先用 contribution URL
                    elif len(urls) > 0 and isinstance(urls[0], str):
                        media["url"] = urls[0]
                    if len(urls) > 0 and isinstance(urls[0], str):
                        media["thumbnail_url"] = urls[0]
                # 有些 AI 生成视频的 URL 在 item[3]
                if not media["url"] and len(item) > 3 and isinstance(item[3], str):
                    media["url"] = item[3]
            else:
                # 未知类型，尝试提取 URL
                if len(item) > 3 and isinstance(item[3], str):
                    media["url"] = item[3]

        except (IndexError, TypeError):
            pass

        return media

    @staticmethod
    def _media_identity_key(file_item):
        """构建媒体身份键，用于去重/去堆叠。"""
        if not isinstance(file_item, dict):
            return None

        media_id = file_item.get("media_id")
        if media_id:
            return ("media_id", str(media_id))

        url = file_item.get("url")
        if url:
            return ("url", str(url))

        return (
            "fallback",
            file_item.get("type"),
            file_item.get("filename"),
            file_item.get("mime"),
            file_item.get("thumbnail_url"),
        )

    @staticmethod
    def normalize_turn_media_first_seen(parsed_turns):
        """
        处理 Gemini 媒体"堆叠回放"结构：
        - 按时间正序识别媒体首次出现位置
        - 仅在首次出现 turn 保留该媒体
        - 后续 turn 的重复媒体移除
        """
        if not isinstance(parsed_turns, list) or not parsed_turns:
            return parsed_turns

        seen = {"user": set(), "assistant": set()}

        # get_chat_detail 返回通常是逆序（新 -> 旧），这里反向遍历做"首见"判定
        for turn in reversed(parsed_turns):
            if not isinstance(turn, dict):
                continue

            for role in ("user", "assistant"):
                role_obj = turn.get(role)
                if not isinstance(role_obj, dict):
                    continue

                files = role_obj.get("files")
                if not isinstance(files, list) or not files:
                    continue

                deduped_in_turn = []
                turn_seen = set()

                for f in files:
                    key = GeminiExporter._media_identity_key(f)
                    if key in turn_seen:
                        continue
                    turn_seen.add(key)

                    if key in seen[role]:
                        continue

                    seen[role].add(key)
                    deduped_in_turn.append(f)

                role_obj["files"] = deduped_in_turn

        return parsed_turns

    @staticmethod
    def _video_preview_name(media_id):
        stem = Path(media_id).stem
        return f"{stem}_preview.jpg"

    def _generate_video_preview(self, video_path, preview_path):
        """
        从视频首帧生成固定尺寸预览图（匹配前端预览卡片 160x110）。
        """
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return False

        preview_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            "scale=160:110:force_original_aspect_ratio=increase,crop=160:110",
            "-q:v",
            "4",
            str(preview_path),
        ]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return proc.returncode == 0 and preview_path.exists() and preview_path.stat().st_size > 0
        except Exception:
            return False

    def _ensure_video_previews_from_turns(self, parsed_turns, media_dir):
        """
        遍历 turn 中的视频附件，确保存在对应首帧预览图。
        """
        media_dir = Path(media_dir)
        seen = set()
        stats = {"preview_generated": 0, "preview_failed": 0}

        for parsed in parsed_turns or []:
            if not isinstance(parsed, dict):
                continue
            for role in ("user", "assistant"):
                role_obj = parsed.get(role)
                if not isinstance(role_obj, dict):
                    continue
                for f in role_obj.get("files", []) or []:
                    if not isinstance(f, dict) or f.get("type") != "video":
                        continue
                    media_id = f.get("media_id")
                    if not media_id:
                        continue

                    preview_id = f.get("preview_media_id") or self._video_preview_name(media_id)
                    f["preview_media_id"] = preview_id

                    key = (media_id, preview_id)
                    if key in seen:
                        continue
                    seen.add(key)

                    video_path = media_dir / media_id
                    preview_path = media_dir / preview_id

                    if preview_path.exists() and preview_path.stat().st_size > 0:
                        continue
                    if not video_path.exists():
                        stats["preview_failed"] += 1
                        continue

                    if self._generate_video_preview(video_path, preview_path):
                        stats["preview_generated"] += 1
                    else:
                        stats["preview_failed"] += 1

        return stats

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

    @staticmethod
    def _infer_media_type(media_hint):
        if not isinstance(media_hint, str) or not media_hint:
            return "file"
        ext = Path(media_hint).suffix.lower().lstrip(".")
        if ext in {"jpg", "jpeg", "png", "webp", "gif", "bmp", "avif", "heic", "heif", "svg"}:
            return "image"
        if ext in {"mp4", "mov", "webm", "mkv", "m4v", "avi", "3gp"}:
            return "video"
        return "file"

    @staticmethod
    def _media_log_fields(url_text, media_type=None, media_hint=None):
        host = "-"
        if isinstance(url_text, str) and url_text:
            try:
                host = (urlparse(url_text).hostname or "-").lower()
            except Exception:
                host = "-"

        kind = media_type if media_type in {"image", "video", "file"} else None
        if kind is None:
            kind = GeminiExporter._infer_media_type(media_hint)
        return {"media": kind, "domain": host}

    @staticmethod
    def _append_authuser(url, authuser):
        if authuser is None:
            return url
        parsed = urlparse(url)
        q = dict(parse_qsl(parsed.query, keep_blank_values=True))
        q["authuser"] = str(authuser)
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(q),
            parsed.fragment,
        ))

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
                media_fields = self._media_log_fields(current_url, media_type=media_type, media_hint=media_hint)
                print(
                    f"  [media-fail] httpx 下载异常: {e}"
                    f" | media={media_fields['media']} domain={media_fields['domain']}"
                )
                return None
            media_fields = self._media_log_fields(current_url, media_type=media_type, media_hint=media_hint)

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
        media_fields = self._media_log_fields(url, media_type=media_type, media_hint=media_hint)
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
            candidates = [self._append_authuser(url, authuser)] if authuser is not None else [url]

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
                    media_fields = self._media_log_fields(candidate_url, media_type=media_type, media_hint=media_hint)
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
                media_fields = self._media_log_fields(url, media_type=media_type, media_hint=media_hint)
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

    @staticmethod
    def _read_jsonl_rows(jsonl_file):
        rows = []
        if not jsonl_file.exists():
            return rows
        with open(jsonl_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    @staticmethod
    def _dedupe_raw_turns_by_id(raw_turns):
        if not isinstance(raw_turns, list) or not raw_turns:
            return raw_turns, 0

        deduped = []
        seen = set()
        removed = 0
        for turn in raw_turns:
            tid = GeminiExporter._turn_id_from_raw(turn)
            if isinstance(tid, str) and tid:
                if tid in seen:
                    removed += 1
                    continue
                seen.add(tid)
            deduped.append(turn)
        return deduped, removed

    @staticmethod
    def _dedupe_message_rows_by_id(message_rows):
        if not isinstance(message_rows, list) or not message_rows:
            return message_rows, 0

        deduped = []
        seen = set()
        removed = 0
        for row in message_rows:
            if not isinstance(row, dict):
                deduped.append(row)
                continue
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id:
                deduped.append(row)
                continue
            if row_id in seen:
                removed += 1
                continue
            seen.add(row_id)
            deduped.append(row)
        return deduped, removed

    @staticmethod
    def _message_row_sort_num(row):
        if not isinstance(row, dict):
            return float("-inf")
        ts = row.get("timestamp")
        if not isinstance(ts, str) or not ts.strip():
            return float("-inf")
        parsed = GeminiExporter._iso_to_epoch_seconds(ts)
        if parsed is None:
            return float("-inf")
        return parsed

    @staticmethod
    def _is_message_rows_sorted_by_timestamp(message_rows):
        if not isinstance(message_rows, list) or len(message_rows) <= 1:
            return True
        prev = float("-inf")
        for row in message_rows:
            cur = GeminiExporter._message_row_sort_num(row)
            if cur < prev:
                return False
            prev = cur
        return True

    @staticmethod
    def _merge_message_rows_for_write(new_msg_rows, existing_msg_rows):
        """
        合并新增与已有 message 行：
        - 以新增行为优先去重（同 id 取 new）
        - 线性归并（两段输入必须已按 timestamp 升序）
        """
        new_deduped, removed_new_dup = GeminiExporter._dedupe_message_rows_by_id(new_msg_rows)
        new_ids = {
            row.get("id")
            for row in new_deduped
            if isinstance(row, dict) and isinstance(row.get("id"), str) and row.get("id")
        }

        existing_without_new = []
        removed_existing_by_new = 0
        for row in existing_msg_rows:
            if (
                isinstance(row, dict)
                and isinstance(row.get("id"), str)
                and row.get("id")
                and row.get("id") in new_ids
            ):
                removed_existing_by_new += 1
                continue
            existing_without_new.append(row)

        existing_deduped, removed_existing_dup = GeminiExporter._dedupe_message_rows_by_id(existing_without_new)
        removed_total = removed_new_dup + removed_existing_by_new + removed_existing_dup

        if not GeminiExporter._is_message_rows_sorted_by_timestamp(new_deduped):
            raise RuntimeError("new_msg_rows 必须按 timestamp 升序")
        if not GeminiExporter._is_message_rows_sorted_by_timestamp(existing_deduped):
            raise RuntimeError("existing_msg_rows 必须按 timestamp 升序")

        merged = []
        i = 0
        j = 0
        while i < len(new_deduped) and j < len(existing_deduped):
            if GeminiExporter._message_row_sort_num(new_deduped[i]) <= GeminiExporter._message_row_sort_num(existing_deduped[j]):
                merged.append(new_deduped[i])
                i += 1
            else:
                merged.append(existing_deduped[j])
                j += 1
        if i < len(new_deduped):
            merged.extend(new_deduped[i:])
        if j < len(existing_deduped):
            merged.extend(existing_deduped[j:])
        return merged, removed_total

    @staticmethod
    def _write_jsonl_rows(jsonl_file, rows):
        with open(jsonl_file, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    @staticmethod
    def _is_media_file_ready(media_dir, media_id):
        if not isinstance(media_id, str) or not media_id:
            return False
        try:
            p = Path(media_dir) / media_id
            return p.exists() and p.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _build_media_id_to_url_map(account_dir):
        url_to_name = GeminiExporter._load_media_manifest_new(account_dir)
        media_to_url = {}
        if not isinstance(url_to_name, dict):
            return media_to_url
        for url, media_name in url_to_name.items():
            if not isinstance(url, str) or not isinstance(media_name, str):
                continue
            if media_name not in media_to_url:
                media_to_url[media_name] = url
        return media_to_url

    @classmethod
    def _scan_failed_media_from_rows(cls, rows, media_dir, media_id_to_url):
        pending = []
        recovered = set()
        seen_pending = set()

        for row in rows:
            if not isinstance(row, dict) or row.get("type") != "message":
                continue
            attachments = row.get("attachments")
            if not isinstance(attachments, list):
                continue

            for att in attachments:
                if not isinstance(att, dict):
                    continue
                media_id = att.get("mediaId")
                if not isinstance(media_id, str) or not media_id:
                    continue

                file_ready = cls._is_media_file_ready(media_dir, media_id)
                marked_failed = bool(att.get("downloadFailed"))

                if file_ready:
                    if marked_failed:
                        recovered.add(media_id)
                    continue

                if media_id in seen_pending:
                    continue
                seen_pending.add(media_id)

                pending.append({
                    "media_id": media_id,
                    "url": media_id_to_url.get(media_id),
                    "error": att.get("downloadError") if isinstance(att.get("downloadError"), str) else "download_failed",
                })

        return pending, recovered

    @classmethod
    def _update_jsonl_media_failure_flags(cls, jsonl_file, failed_error_map, recovered_ids):
        rows = cls._read_jsonl_rows(jsonl_file)
        if not rows:
            return {"marked": 0, "cleared": 0}

        marked = 0
        cleared = 0
        changed = False
        recovered_ids = set(recovered_ids or set())
        failed_error_map = dict(failed_error_map or {})

        for row in rows:
            if not isinstance(row, dict) or row.get("type") != "message":
                continue
            attachments = row.get("attachments")
            if not isinstance(attachments, list):
                continue

            for att in attachments:
                if not isinstance(att, dict):
                    continue
                media_id = att.get("mediaId")
                if not isinstance(media_id, str) or not media_id:
                    continue

                if media_id in recovered_ids:
                    had_failed = "downloadFailed" in att
                    had_error = "downloadError" in att
                    if had_failed:
                        att.pop("downloadFailed", None)
                    if had_error:
                        att.pop("downloadError", None)
                    if had_failed or had_error:
                        changed = True
                        cleared += 1
                    continue

                if media_id in failed_error_map:
                    error_text = failed_error_map.get(media_id) or "download_failed"
                    if att.get("downloadFailed") is not True or att.get("downloadError") != error_text:
                        att["downloadFailed"] = True
                        att["downloadError"] = error_text
                        changed = True
                    marked += 1

        if changed:
            cls._write_jsonl_rows(jsonl_file, rows)
        return {"marked": marked, "cleared": cleared}

    def _retry_failed_media_for_conversation(self, jsonl_file, account_dir, media_dir, stats):
        if not Path(jsonl_file).exists():
            return {"attempted": 0, "recovered": 0, "failed": 0, "missingUrl": 0}

        rows = self._read_jsonl_rows(jsonl_file)
        if not rows:
            return {"attempted": 0, "recovered": 0, "failed": 0, "missingUrl": 0}

        media_id_to_url = self._build_media_id_to_url_map(account_dir)
        pending, recovered_existing = self._scan_failed_media_from_rows(rows, media_dir, media_id_to_url)
        if not pending and not recovered_existing:
            return {"attempted": 0, "recovered": 0, "failed": 0, "missingUrl": 0}

        downloadable = [p for p in pending if isinstance(p.get("url"), str) and p["url"]]
        missing_url = [p for p in pending if not p.get("url")]

        retry_batch = [
            {
                "url": item["url"],
                "filepath": Path(media_dir) / item["media_id"],
                "media_id": item["media_id"],
                "media_type": self._infer_media_type(item["media_id"]),
            }
            for item in downloadable
        ]
        failed_items = self.download_media_batch(retry_batch, media_dir, stats) if retry_batch else []

        failed_map = {item["media_id"]: (item.get("error") or "download_failed") for item in failed_items}
        for item in missing_url:
            failed_map[item["media_id"]] = "missing_manifest_url"

        attempted_ids = {item["media_id"] for item in downloadable}
        recovered_ids = set(recovered_existing) | (attempted_ids - set(failed_map.keys()))

        flag_stats = self._update_jsonl_media_failure_flags(jsonl_file, failed_map, recovered_ids)

        return {
            "attempted": len(attempted_ids),
            "recovered": len(recovered_ids),
            "failed": len(failed_map),
            "missingUrl": len(missing_url),
            "flagMarked": flag_stats.get("marked", 0),
            "flagCleared": flag_stats.get("cleared", 0),
        }

    @staticmethod
    def _turn_id_from_raw(raw_turn):
        try:
            ids = raw_turn[0]
            return ids[1] if len(ids) > 1 else ids[0]
        except (IndexError, TypeError):
            return None

    @staticmethod
    def _build_existing_turn_id_set(existing_rows):
        out = set()
        for row in existing_rows:
            tid = row.get("turn_id") if isinstance(row, dict) else None
            if isinstance(tid, str) and tid:
                out.add(tid)
        return out

    @staticmethod
    def _latest_ts_from_rows(rows):
        latest = None
        for row in rows:
            ts = row.get("timestamp") if isinstance(row, dict) else None
            if isinstance(ts, int) and (latest is None or ts > latest):
                latest = ts
        return latest

    @staticmethod
    def _load_media_manifest(out_dir):
        manifest_file = out_dir / "media_manifest.json"
        if not manifest_file.exists():
            return {}
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            url_map = data.get("url_to_name", {}) if isinstance(data, dict) else {}
            return url_map if isinstance(url_map, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def _save_media_manifest(out_dir, url_to_name):
        manifest_file = out_dir / "media_manifest.json"
        manifest_file.write_text(
            json.dumps({"url_to_name": url_to_name}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # 新格式输出辅助方法（accounts/{id}/ 目录结构）
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
            safe_id = self.email_to_account_id(email)
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

    @staticmethod
    def _write_accounts_json(base_dir, account_info):
        """更新根目录 accounts.json"""
        accounts_file = Path(base_dir) / "accounts.json"
        now_iso = datetime.datetime.now(datetime.UTC).isoformat()

        existing = {}
        if accounts_file.exists():
            try:
                data = json.loads(accounts_file.read_text(encoding="utf-8"))
                for a in data.get("accounts", []):
                    existing[a["id"]] = a
            except Exception:
                pass

        account_id = account_info["id"]
        existing_account = existing.get(account_id, {})
        authuser = account_info.get("authuser")
        if authuser is None:
            authuser = existing_account.get("authuser")
        existing[account_id] = {
            "id": account_id,
            "email": account_info.get("email", ""),
            "addedAt": existing_account.get("addedAt", now_iso),
            "dataDir": f"accounts/{account_id}",
            "authuser": authuser,
        }

        data = {
            "version": 1,
            "updatedAt": now_iso,
            "accounts": list(existing.values()),
        }
        accounts_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _write_account_meta(account_dir, account_info):
        """写入 accounts/{id}/meta.json"""
        meta = {
            "version": 1,
            "id": account_info["id"],
            "name": account_info.get("name", ""),
            "email": account_info.get("email", ""),
            "avatarText": account_info.get("avatarText", "?"),
            "avatarColor": account_info.get("avatarColor", "#667eea"),
            "conversationCount": account_info.get("conversationCount", 0),
            "remoteConversationCount": account_info.get("remoteConversationCount"),
            "lastSyncAt": account_info.get("lastSyncAt"),
            "lastSyncResult": account_info.get("lastSyncResult"),
            "authuser": account_info.get("authuser"),
        }
        (Path(account_dir) / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _write_conversations_index(account_dir, account_id, updated_at, summaries):
        """写入 accounts/{id}/conversations.json"""
        data = {
            "version": 1,
            "accountId": account_id,
            "updatedAt": updated_at,
            "totalCount": len(summaries),
            "items": summaries,
        }
        (Path(account_dir) / "conversations.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _write_sync_state(account_dir, state):
        """写入 accounts/{id}/sync_state.json"""
        (Path(account_dir) / "sync_state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _load_sync_state(account_dir):
        """读取 accounts/{id}/sync_state.json，失败时返回空 dict。"""
        sync_file = Path(account_dir) / "sync_state.json"
        if not sync_file.exists():
            return {}
        try:
            data = json.loads(sync_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _load_conversations_index(account_dir):
        """
        读取 accounts/{id}/conversations.json。
        返回: (ordered_ids, index_map)
        """
        conv_file = Path(account_dir) / "conversations.json"
        if not conv_file.exists():
            return [], {}
        try:
            data = json.loads(conv_file.read_text(encoding="utf-8"))
        except Exception:
            return [], {}

        items = data.get("items", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            return [], {}

        ordered_ids = []
        index_map = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            if not isinstance(cid, str) or not cid:
                continue
            ordered_ids.append(cid)
            index_map[cid] = item
        return ordered_ids, index_map

    @classmethod
    def _normalize_conversation_status(cls, value, default=None):
        fallback = default or cls.CONVERSATION_STATUS_NORMAL
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
        return fallback

    @classmethod
    def _status_for_remote_summary(cls, existing=None):
        existing = existing if isinstance(existing, dict) else {}
        current_status = cls._normalize_conversation_status(existing.get("status"))
        if current_status == cls.CONVERSATION_STATUS_HIDDEN:
            return cls.CONVERSATION_STATUS_HIDDEN
        return cls.CONVERSATION_STATUS_NORMAL

    @classmethod
    def _build_lost_summary(cls, bare_id, existing=None):
        existing = existing if isinstance(existing, dict) else {}
        last_message = existing.get("lastMessage")
        if not isinstance(last_message, str):
            last_message = ""

        message_count = existing.get("messageCount")
        if not isinstance(message_count, int) or message_count < 0:
            message_count = 0

        image_count = existing.get("imageCount")
        if not isinstance(image_count, int) or image_count < 0:
            image_count = 0
        video_count = existing.get("videoCount")
        if not isinstance(video_count, int) or video_count < 0:
            video_count = 0

        return {
            "id": bare_id,
            "title": existing.get("title") or bare_id,
            "lastMessage": last_message,
            "messageCount": message_count,
            "hasMedia": bool(existing.get("hasMedia", False)),
            "hasFailedData": bool(existing.get("hasFailedData", False)),
            "imageCount": image_count,
            "videoCount": video_count,
            "updatedAt": existing.get("updatedAt"),
            "remoteHash": existing.get("remoteHash"),
            "status": cls.CONVERSATION_STATUS_LOST,
        }

    @staticmethod
    def _build_summary_from_chat_listing(chat, existing=None):
        """将列表页 chat 条目转换为 conversations.json 的 summary 条目。"""
        existing = existing if isinstance(existing, dict) else {}
        status = GeminiExporter._status_for_remote_summary(existing)
        bare_id = str(chat.get("id", "")).replace("c_", "")
        title = chat.get("title")
        if not isinstance(title, str):
            title = existing.get("title", "")
        remote_ts = GeminiExporter._coerce_epoch_seconds(chat.get("latest_update_ts"))
        if remote_ts is not None:
            updated_at = GeminiExporter._to_iso_utc(remote_ts)
            remote_hash = str(remote_ts)
        else:
            updated_at = existing.get("updatedAt")
            remote_hash = existing.get("remoteHash")

        msg_count = existing.get("messageCount", 0)
        if not isinstance(msg_count, int):
            msg_count = 0
        image_count = existing.get("imageCount", 0)
        if not isinstance(image_count, int) or image_count < 0:
            image_count = 0
        video_count = existing.get("videoCount", 0)
        if not isinstance(video_count, int) or video_count < 0:
            video_count = 0

        return {
            "id": bare_id,
            "title": title or "",
            "lastMessage": existing.get("lastMessage", ""),
            "messageCount": msg_count,
            "hasMedia": bool(existing.get("hasMedia", False)),
            "hasFailedData": bool(existing.get("hasFailedData", False)),
            "imageCount": image_count,
            "videoCount": video_count,
            "updatedAt": updated_at,
            "remoteHash": remote_hash,
            "status": status,
        }

    @staticmethod
    def _load_media_manifest_new(account_dir):
        """从账号目录读取媒体清单"""
        manifest_file = Path(account_dir) / "media_manifest.json"
        if not manifest_file.exists():
            return {}
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            url_map = data.get("url_to_name", {}) if isinstance(data, dict) else {}
            return url_map if isinstance(url_map, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _save_media_manifest_new(account_dir, url_to_name):
        """保存媒体清单到账号目录"""
        manifest_file = Path(account_dir) / "media_manifest.json"
        manifest_file.write_text(
            json.dumps({"url_to_name": url_to_name}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _sort_parsed_turns_by_timestamp(parsed_turns):
        if not isinstance(parsed_turns, list) or not parsed_turns:
            return []
        indexed = list(enumerate(parsed_turns))

        def _sort_key(item):
            idx, turn = item
            ts = turn.get("timestamp") if isinstance(turn, dict) else None
            sort_ts = ts if isinstance(ts, int) else 2**63 - 1
            return (sort_ts, idx)

        return [turn for _, turn in sorted(indexed, key=_sort_key)]

    @staticmethod
    def _turns_to_jsonl_rows(parsed_turns, conv_id, account_id, title, chat_info):
        """将 parsed_turns 转为新 JSONL 格式行列表（meta 首行 + message 行）"""
        now_iso = datetime.datetime.now(datetime.UTC).isoformat()
        bare_id = conv_id.replace("c_", "")
        ordered_turns = GeminiExporter._sort_parsed_turns_by_timestamp(parsed_turns)

        ts_list = [t["timestamp"] for t in ordered_turns if isinstance(t.get("timestamp"), int)]
        created_at = GeminiExporter._to_iso_utc(min(ts_list)) if ts_list else None

        remote_ts = GeminiExporter._coerce_epoch_seconds(chat_info.get("latest_update_ts"))
        if remote_ts is None and ts_list:
            remote_ts = max(ts_list)

        updated_at = GeminiExporter._to_iso_utc(remote_ts)
        if not updated_at:
            chat_iso = chat_info.get("latest_update_iso")
            if isinstance(chat_iso, str) and chat_iso.strip():
                updated_at = chat_iso.strip()
        if not created_at:
            created_at = updated_at or now_iso

        remote_hash = str(remote_ts) if remote_ts is not None else None

        rows = [{
            "type": "meta",
            "id": bare_id,
            "accountId": account_id,
            "title": title,
            "createdAt": created_at,
            "updatedAt": updated_at,
            "remoteHash": remote_hash,
        }]

        for turn in ordered_turns:
            turn_id = turn.get("turn_id") or uuid.uuid4().hex
            ts = GeminiExporter._to_iso_utc(turn.get("timestamp")) or now_iso

            user = turn.get("user", {})
            user_attachments = []
            for f in user.get("files", []):
                media_id = f.get("media_id")
                if not media_id:
                    continue
                item = {"mediaId": media_id, "mimeType": f.get("mime") or ""}
                preview_id = f.get("preview_media_id")
                if preview_id:
                    item["previewMediaId"] = preview_id
                user_attachments.append(item)
            rows.append({
                "type": "message",
                "id": f"{turn_id}_u",
                "role": "user",
                "text": user.get("text", ""),
                "attachments": user_attachments,
                "timestamp": ts,
            })

            asst = turn.get("assistant", {})
            asst_attachments = []
            for f in asst.get("files", []):
                media_id = f.get("media_id")
                if not media_id:
                    continue
                item = {"mediaId": media_id, "mimeType": f.get("mime") or ""}
                preview_id = f.get("preview_media_id")
                if preview_id:
                    item["previewMediaId"] = preview_id
                asst_attachments.append(item)
            model_row = {
                "type": "message",
                "id": f"{turn_id}_m",
                "role": "model",
                "text": asst.get("text", ""),
                "attachments": asst_attachments,
                "timestamp": ts,
                "model": asst.get("model", ""),
            }
            thinking = asst.get("thinking", "")
            if thinking:
                model_row["thinking"] = thinking
            rows.append(model_row)

        return rows

    @staticmethod
    def _build_existing_turn_id_set_new(jsonl_file):
        """从新格式 JSONL 中提取已有 turn_id 集合（跳过 meta 行）"""
        ids = set()
        if not Path(jsonl_file).exists():
            return ids
        with open(jsonl_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if row.get("type") != "message":
                        continue
                    msg_id = row.get("id", "")
                    if msg_id.endswith("_u") or msg_id.endswith("_m"):
                        ids.add(msg_id[:-2])
                except json.JSONDecodeError:
                    continue
        return ids

    @staticmethod
    def _count_message_rows_new(jsonl_file):
        """统计新格式 JSONL 中 message 行数量。"""
        count = 0
        if not Path(jsonl_file).exists():
            return 0
        with open(jsonl_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") == "message":
                    count += 1
        return count

    @staticmethod
    def _count_media_types_from_rows(rows):
        image_count = 0
        video_count = 0
        for row in rows:
            if not isinstance(row, dict) or row.get("type") != "message":
                continue
            attachments = row.get("attachments")
            if not isinstance(attachments, list):
                continue
            for att in attachments:
                if not isinstance(att, dict):
                    continue
                mime_type = att.get("mimeType")
                if not isinstance(mime_type, str):
                    continue
                mime_lower = mime_type.lower()
                if mime_lower.startswith("image/"):
                    image_count += 1
                elif mime_lower.startswith("video/"):
                    video_count += 1
        return image_count, video_count

    @staticmethod
    def _rows_has_failed_data(rows):
        for row in rows:
            if not isinstance(row, dict) or row.get("type") != "message":
                continue
            attachments = row.get("attachments")
            if not isinstance(attachments, list):
                continue
            for att in attachments:
                if isinstance(att, dict) and att.get("downloadFailed") is True:
                    return True
        return False

    @staticmethod
    def _remote_hash_from_jsonl(jsonl_file):
        """从新格式 JSONL meta 行读取 remoteHash"""
        if not Path(jsonl_file).exists():
            return None
        with open(jsonl_file, "r", encoding="utf-8") as fh:
            line = fh.readline().strip()
            if not line:
                return None
            try:
                row = json.loads(line)
                if row.get("type") == "meta":
                    return row.get("remoteHash")
            except json.JSONDecodeError:
                pass
        return None

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
                        ext = "mp4" if f.get("type") == "video" else "jpg"
                        raw_name = f.get("filename") or ""
                        raw_suffix = Path(raw_name).suffix.lower()
                        if raw_suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".mp4", ".mov", ".webm", ".mkv"}:
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
                        f["preview_media_id"] = self._video_preview_name(media_id)
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
                tid = self._turn_id_from_raw(turn)
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

        print(f"[*] 账号: {account_info['email'] or account_id}")
        print(f"[*] 仅同步列表到: {account_dir.absolute()}")

        existing_order, existing_index = self._load_conversations_index(account_dir)
        sync_state = self._load_sync_state(account_dir)
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
                        summaries.append(self._build_lost_summary(cid, existing_index.get(cid)))
                        lost_count += 1
                listing_cursor = None
                listing_fetched_ids = []
            else:
                summaries = _build_partial_summaries()
                listing_cursor = cursor
                listing_fetched_ids = list(fetched_order)

            current_state = self._load_sync_state(account_dir)
            pending_conversations = (
                current_state.get("pendingConversations")
                if isinstance(current_state, dict) else []
            )
            if not isinstance(pending_conversations, list):
                pending_conversations = []

            self._write_conversations_index(account_dir, account_id, now_iso, summaries)
            self._write_sync_state(account_dir, {
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
            self._write_accounts_json(base_dir, account_info)
            self._write_account_meta(account_dir, account_info)
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
                conv_index[bare_id] = self._build_summary_from_chat_listing(chat, existing)
                if bare_id not in fetched_seen:
                    fetched_seen.add(bare_id)
                    fetched_order.append(bare_id)

                remote_ts = chat.get("latest_update_ts")
                local_ts = self._summary_to_epoch_seconds(existing_index.get(bare_id))
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

        conv_id = self.normalize_chat_id(conversation_id)
        bare_id = conv_id.replace("c_", "")
        jsonl_file = conv_dir / f"{bare_id}.jsonl"
        tmp_turns_file = conv_dir / f".tmp_{bare_id}.turns.json"
        local_jsonl_exists = jsonl_file.exists()
        detail_mode = "incremental" if local_jsonl_exists else "full"
        pre_sync_media_stats = {
            "media_downloaded": 0,
            "media_failed": 0,
        }

        print(f"[*] 账号: {account_info['email'] or account_id}")
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

        _, existing_index = self._load_conversations_index(account_dir)
        existing_summary = existing_index.get(bare_id, {}) if isinstance(existing_index, dict) else {}
        existing_status = self._normalize_conversation_status(
            existing_summary.get("status"),
            self.CONVERSATION_STATUS_NORMAL,
        )

        latest_update_ts = self._coerce_epoch_seconds(existing_summary.get("remoteHash"))

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
            state = self._load_sync_state(account_dir)
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
            self._write_sync_state(account_dir, state)

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
        existing_turn_ids = self._build_existing_turn_id_set_new(jsonl_file) if local_jsonl_exists else set()

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
            base_message_count = self._count_message_rows_new(jsonl_file)

        progress_index = dict(existing_index)

        def _persist_progress_summary(parsed_turn_count):
            now_iso_local = datetime.datetime.now(datetime.UTC).isoformat()
            incremental_base = base_message_count if detail_mode == "incremental" and local_jsonl_exists else 0
            progress_count = max(0, incremental_base + parsed_turn_count * 2)
            progress_remote_ts = self._coerce_epoch_seconds(chat_info.get("latest_update_ts"))
            progress_remote_iso = self._to_iso_utc(progress_remote_ts) if progress_remote_ts is not None else None

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
            progress_summary["status"] = self._normalize_conversation_status(
                progress_summary.get("status"),
                existing_status,
            )

            progress_index[bare_id] = progress_summary
            progress_summaries = list(progress_index.values())
            progress_summaries.sort(key=lambda s: _updated_sort_num(s.get("updatedAt")), reverse=True)
            self._write_conversations_index(account_dir, account_id, now_iso_local, progress_summaries)

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
                        tid = self._turn_id_from_raw(turn)
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

        raw_turns, removed_turns = self._dedupe_raw_turns_by_id(raw_turns)
        if removed_turns > 0:
            print(f"  [dedupe] 当前会话分页结果去重: {removed_turns} 个重复 turn")

        fetch_elapsed = time.perf_counter() - fetch_start
        media_start = time.perf_counter()

        global_seen_urls = self._load_media_manifest_new(account_dir)
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
                parsed_new_turns = [self.parse_turn(turn) for turn in raw_turns]
                parsed_new_turns = self.normalize_turn_media_first_seen(parsed_new_turns)

                batch_list = self._assign_media_ids_and_collect_downloads(
                    parsed_new_turns, media_dir, global_seen_urls, global_used_names,
                )
                new_rows_full = self._turns_to_jsonl_rows(parsed_new_turns, conv_id, account_id, title, chat_info)
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

                merged_msg_rows, removed_msg_rows = self._merge_message_rows_for_write(
                    new_msg_rows, existing_msg_rows
                )
                if removed_msg_rows > 0:
                    print(f"  [dedupe] 合并写盘去重: {removed_msg_rows} 行")
                all_rows = [new_meta] + merged_msg_rows
                self._write_jsonl_rows(jsonl_file, all_rows)

                failed_items = []
                if batch_list:
                    print(f"  媒体文件: {len(batch_list)} 个（去重后）")
                    failed_items = self.download_media_batch(batch_list, media_dir, media_stats)
                    self._save_media_manifest_new(account_dir, global_seen_urls)
                preview_stats = self._ensure_video_previews_from_turns(parsed_new_turns, media_dir)
                media_stats["preview_generated"] += preview_stats["preview_generated"]
                media_stats["preview_failed"] += preview_stats["preview_failed"]

                batch_media_ids = {item.get("media_id") or item["filepath"].name for item in batch_list}
                failed_map = {item["media_id"]: (item.get("error") or "download_failed") for item in failed_items}
                recovered_ids = batch_media_ids - set(failed_map.keys())
                flag_stats = self._update_jsonl_media_failure_flags(jsonl_file, failed_map, recovered_ids)
                if flag_stats["marked"] > 0 or flag_stats["cleared"] > 0:
                    print(
                        "  [media-flag] 已更新附件下载标记:"
                        f" marked={flag_stats['marked']},"
                        f" cleared={flag_stats['cleared']}"
                    )

                rows_after = self._read_jsonl_rows(jsonl_file)
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
                has_failed_data = self._rows_has_failed_data(all_msg_rows)
                image_count, video_count = self._count_media_types_from_rows(all_msg_rows)
                last_text = ""
                for r in reversed(all_msg_rows):
                    if r.get("text"):
                        last_text = r["text"][:80]
                        break

                summary = {
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
                    "status": existing_status,
                }
            else:
                remote_ts = self._coerce_epoch_seconds(chat_info.get("latest_update_ts"))
                remote_iso = self._to_iso_utc(remote_ts) if remote_ts is not None else None
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
            parsed_turns = [self.parse_turn(turn) for turn in raw_turns]
            parsed_turns = self.normalize_turn_media_first_seen(parsed_turns)

            batch_list = self._assign_media_ids_and_collect_downloads(
                parsed_turns, media_dir, global_seen_urls, global_used_names,
            )
            rows = self._turns_to_jsonl_rows(parsed_turns, conv_id, account_id, title, chat_info)
            self._write_jsonl_rows(jsonl_file, rows)

            failed_items = []
            if batch_list:
                print(f"  媒体文件: {len(batch_list)} 个（去重后）")
                failed_items = self.download_media_batch(batch_list, media_dir, media_stats)
                self._save_media_manifest_new(account_dir, global_seen_urls)
            preview_stats = self._ensure_video_previews_from_turns(parsed_turns, media_dir)
            media_stats["preview_generated"] += preview_stats["preview_generated"]
            media_stats["preview_failed"] += preview_stats["preview_failed"]

            batch_media_ids = {item.get("media_id") or item["filepath"].name for item in batch_list}
            failed_map = {item["media_id"]: (item.get("error") or "download_failed") for item in failed_items}
            recovered_ids = batch_media_ids - set(failed_map.keys())
            flag_stats = self._update_jsonl_media_failure_flags(jsonl_file, failed_map, recovered_ids)
            if flag_stats["marked"] > 0 or flag_stats["cleared"] > 0:
                print(
                    "  [media-flag] 已更新附件下载标记:"
                    f" marked={flag_stats['marked']},"
                    f" cleared={flag_stats['cleared']}"
                )

            rows_after = self._read_jsonl_rows(jsonl_file)
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
            has_media = any(r.get("attachments") for r in msg_rows)
            has_failed_data = self._rows_has_failed_data(msg_rows)
            image_count, video_count = self._count_media_types_from_rows(msg_rows)
            last_text = ""
            for r in reversed(msg_rows):
                if r.get("text"):
                    last_text = r["text"][:80]
                    break

            summary = {
                "id": bare_id,
                "title": title,
                "lastMessage": last_text,
                "messageCount": len(msg_rows),
                "hasMedia": has_media,
                "hasFailedData": has_failed_data,
                "imageCount": image_count,
                "videoCount": video_count,
                "updatedAt": meta_row.get("updatedAt") or chat_info.get("latest_update_iso"),
                "remoteHash": meta_row.get("remoteHash"),
                "status": existing_status,
            }

        summary["status"] = self._normalize_conversation_status(
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

        self._write_accounts_json(base_dir, account_info)
        self._write_account_meta(account_dir, account_info)
        self._write_conversations_index(account_dir, account_id, now_iso, summaries)
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

    def export_all(self, output_dir=None, chat_ids=None):
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
        account_info = self._resolve_account_info()
        account_id = account_info["id"]
        account_dir = base_dir / "accounts" / account_id
        conv_dir = account_dir / "conversations"
        media_dir = account_dir / "media"

        account_dir.mkdir(parents=True, exist_ok=True)
        conv_dir.mkdir(parents=True, exist_ok=True)
        media_dir.mkdir(parents=True, exist_ok=True)
        self._set_request_state_scope(account_dir)
        existing_order, existing_index = self._load_conversations_index(account_dir)

        print(f"[*] 账号: {account_info['email'] or account_id}")
        print(f"[*] 输出目录: {account_dir.absolute()}")

        # 3. 获取聊天列表
        if chat_ids:
            chats = [{
                "id": self.normalize_chat_id(cid),
                "title": "",
                "latest_update_ts": None,
                "latest_update_iso": None,
            } for cid in chat_ids]
            print(f"[*] 指定导出 {len(chats)} 个对话")
        else:
            chats = self.get_all_chats()

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

        global_seen_urls = self._load_media_manifest_new(account_dir)
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
            remote_status = self._status_for_remote_summary(existing_summary)
            print(f"\n[{idx + 1}/{total}] {title} ({conv_id})")

            try:
                raw_turns = self.get_chat_detail(conv_id)
                raw_turns, removed_turns = self._dedupe_raw_turns_by_id(raw_turns)
                print(f"  轮次: {len(raw_turns)}")
                if removed_turns > 0:
                    print(f"  [dedupe] 分页结果去重: {removed_turns} 个重复 turn")

                parsed_turns = [self.parse_turn(turn) for turn in raw_turns]
                parsed_turns = self.normalize_turn_media_first_seen(parsed_turns)

                batch_list = self._assign_media_ids_and_collect_downloads(
                    parsed_turns, media_dir, global_seen_urls, global_used_names,
                )

                # 转换为新 JSONL 格式并写入
                rows = self._turns_to_jsonl_rows(parsed_turns, conv_id, account_id, title, chat)
                jsonl_file = conv_dir / f"{bare_id}.jsonl"
                self._write_jsonl_rows(jsonl_file, rows)

                # 下载媒体
                failed_items = []
                if batch_list:
                    print(f"  媒体文件: {len(batch_list)} 个（去重后）")
                    failed_items = self.download_media_batch(batch_list, media_dir, stats)
                    self._save_media_manifest_new(account_dir, global_seen_urls)
                preview_stats = self._ensure_video_previews_from_turns(parsed_turns, media_dir)
                stats["preview_generated"] += preview_stats["preview_generated"]
                stats["preview_failed"] += preview_stats["preview_failed"]

                batch_media_ids = {item.get("media_id") or item["filepath"].name for item in batch_list}
                failed_map = {item["media_id"]: (item.get("error") or "download_failed") for item in failed_items}
                recovered_ids = batch_media_ids - set(failed_map.keys())
                flag_stats = self._update_jsonl_media_failure_flags(jsonl_file, failed_map, recovered_ids)
                if flag_stats["marked"] > 0 or flag_stats["cleared"] > 0:
                    print(
                        "  [media-flag] 已更新附件下载标记:"
                        f" marked={flag_stats['marked']},"
                        f" cleared={flag_stats['cleared']}"
                    )

                # 构建 summary
                rows_after = self._read_jsonl_rows(jsonl_file)
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
                has_failed_data = self._rows_has_failed_data(msg_rows)
                image_count, video_count = self._count_media_types_from_rows(msg_rows)
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
                chat_remote_ts = self._coerce_epoch_seconds(chat.get("latest_update_ts"))
                conv_summaries.append({
                    "id": bare_id,
                    "title": title,
                    "lastMessage": "",
                    "messageCount": 0,
                    "hasMedia": False,
                    "hasFailedData": False,
                    "imageCount": 0,
                    "videoCount": 0,
                    "updatedAt": self._to_iso_utc(chat_remote_ts) or chat.get("latest_update_iso"),
                    "remoteHash": str(chat_remote_ts) if chat_remote_ts is not None else None,
                    "status": remote_status,
                })

        remote_ids = {row.get("id") for row in conv_summaries if isinstance(row, dict)}
        lost_ids = [cid for cid in existing_order if cid not in remote_ids]
        for cid in lost_ids:
            conv_summaries.append(self._build_lost_summary(cid, existing_index.get(cid)))
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

        self._write_accounts_json(base_dir, account_info)
        self._write_account_meta(account_dir, account_info)
        self._write_conversations_index(account_dir, account_id, now_iso, conv_summaries)
        self._write_sync_state(account_dir, {
            "version": 1,
            "accountId": account_id,
            "updatedAt": now_iso,
            "requestState": self._current_request_state(now_iso),
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
        print(f"  账号: {account_info['email'] or account_id}")
        print(f"  成功: {stats['success']}/{total}")
        print(f"  失败: {stats['failed']}/{total}")
        print(f"  媒体下载: {stats['media_downloaded']}")
        print(f"  媒体失败: {stats['media_failed']}")
        print(f"  视频预览生成: {stats['preview_generated']}")
        print(f"  视频预览失败: {stats['preview_failed']}")
        print(f"  输出目录: {account_dir.absolute()}")

    def export_incremental(self, output_dir=None):
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
        account_info = self._resolve_account_info()
        account_id = account_info["id"]
        account_dir = base_dir / "accounts" / account_id
        conv_dir = account_dir / "conversations"
        media_dir = account_dir / "media"

        account_dir.mkdir(parents=True, exist_ok=True)
        conv_dir.mkdir(parents=True, exist_ok=True)
        media_dir.mkdir(parents=True, exist_ok=True)
        self._set_request_state_scope(account_dir)

        print(f"[*] 账号: {account_info['email'] or account_id}")

        global_seen_urls = self._load_media_manifest_new(account_dir)
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
        existing_order, existing_index = self._load_conversations_index(account_dir)
        conv_index = dict(existing_index)

        chats_to_update = []
        scanned_order = []
        scanned_seen = set()
        listing_cursor = None
        listing_exhausted = False

        while True:
            items, next_cursor = self.get_chats_page(listing_cursor)
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

                conv_index[bare_id] = self._build_summary_from_chat_listing(
                    chat,
                    conv_index.get(bare_id),
                )

                local_summary = existing_index.get(bare_id)
                local_updated_ts = self._summary_to_epoch_seconds(local_summary)
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
            existing_ids = self._build_existing_turn_id_set_new(jsonl_file)
            raw_new_turns = self.get_chat_detail_incremental(conv_id, existing_ids)
            raw_new_turns, removed_turns = self._dedupe_raw_turns_by_id(raw_new_turns)

            if not raw_new_turns:
                print("  无新增 turn")
                continue
            if removed_turns > 0:
                print(f"  [dedupe] 增量抓取结果去重: {removed_turns} 个重复 turn")

            parsed_new_turns = [self.parse_turn(turn) for turn in raw_new_turns]
            batch_list = self._assign_media_ids_and_collect_downloads(
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
            new_rows_full = self._turns_to_jsonl_rows(parsed_new_turns, conv_id, account_id, title, chat)
            new_meta = new_rows_full[0]
            new_msg_rows = new_rows_full[1:]

            # 合并：新消息（正序）在前，旧消息跟随
            merged_msg_rows, removed_msg_rows = self._merge_message_rows_for_write(
                new_msg_rows, existing_msg_rows
            )
            if removed_msg_rows > 0:
                print(f"  [dedupe] 增量合并写盘去重: {removed_msg_rows} 行")
            all_rows = [new_meta] + merged_msg_rows
            self._write_jsonl_rows(jsonl_file, all_rows)

            failed_items = []
            if batch_list:
                failed_items = self.download_media_batch(batch_list, media_dir, stats)
                self._save_media_manifest_new(account_dir, global_seen_urls)
            preview_stats = self._ensure_video_previews_from_turns(parsed_new_turns, media_dir)
            stats["preview_generated"] += preview_stats["preview_generated"]
            stats["preview_failed"] += preview_stats["preview_failed"]

            batch_media_ids = {item.get("media_id") or item["filepath"].name for item in batch_list}
            failed_map = {item["media_id"]: (item.get("error") or "download_failed") for item in failed_items}
            recovered_ids = batch_media_ids - set(failed_map.keys())
            flag_stats = self._update_jsonl_media_failure_flags(jsonl_file, failed_map, recovered_ids)
            if flag_stats["marked"] > 0 or flag_stats["cleared"] > 0:
                print(
                    "  [media-flag] 已更新附件下载标记:"
                    f" marked={flag_stats['marked']},"
                    f" cleared={flag_stats['cleared']}"
                )

            # 更新 conv_index
            rows_after = self._read_jsonl_rows(jsonl_file)
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
            has_failed_data = self._rows_has_failed_data(all_msg_rows)
            image_count, video_count = self._count_media_types_from_rows(all_msg_rows)
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
                "status": self._status_for_remote_summary(conv_index.get(bare_id)),
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
                summaries.append(self._build_lost_summary(cid, existing_index.get(cid)))
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
        self._write_accounts_json(base_dir, account_info)
        self._write_account_meta(account_dir, account_info)
        self._write_conversations_index(account_dir, account_id, now_iso, summaries)
        self._write_sync_state(account_dir, {
            "version": 1,
            "accountId": account_id,
            "updatedAt": now_iso,
            "requestState": self._current_request_state(now_iso),
            "concurrency": 3,
            "fullSync": None,
            "pendingConversations": [],
        })

        print(f"\n{'=' * 50}")
        print("增量导出完成")
        print(f"  账号: {account_info['email'] or account_id}")
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
            exporter.normalize_chat_id(args.check_chat_id),
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

        def _persist_account(account_info):
            account_id = account_info["id"]
            account_dir = base_dir / "accounts" / account_id
            account_dir.mkdir(parents=True, exist_ok=True)
            (account_dir / "conversations").mkdir(exist_ok=True)
            (account_dir / "media").mkdir(exist_ok=True)
            GeminiExporter._write_accounts_json(base_dir, account_info)
            GeminiExporter._write_account_meta(account_dir, account_info)
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
        exporter.export_incremental(output_dir=args.output)
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

    exporter.export_all(output_dir=args.output, chat_ids=args.chat_ids)


if __name__ == "__main__":
    main()
