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
import re
import sys
import time
import datetime
import codecs
import uuid
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
REQUEST_DELAY = 0.5      # 请求间隔(秒)

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
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
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
    def __init__(self, cookies: dict, user=None):
        self.cookies = cookies
        self.user_spec = str(user).strip() if user is not None else None
        self.authuser = None
        self.client = httpx.Client(
            cookies=cookies,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            },
            follow_redirects=True,
            timeout=60.0,
        )
        self.at = None   # CSRF token
        self.bl = None   # 服务器版本
        self.fsid = None # session ID
        self.reqid = 100000

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
                resp = self.client.get(f"{GEMINI_BASE}/app", params={"authuser": str(idx)})
                if email in resp.text.lower():
                    self.authuser = str(idx)
                    print(f"  authuser: {self.authuser} (邮箱匹配)")
                    return
            except Exception:
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

    def _client_get_with_retry(self, url, params=None, attempts=6):
        last_err = None
        for i in range(attempts):
            try:
                return self.client.get(url, params=params)
            except Exception as e:
                last_err = e
                time.sleep(0.4 * (i + 1))
        if last_err:
            raise last_err
        raise RuntimeError("GET 请求失败")

    @staticmethod
    def _to_iso_utc(ts):
        if ts is None:
            return None
        try:
            return datetime.datetime.fromtimestamp(int(ts), datetime.UTC).isoformat()
        except (TypeError, ValueError, OSError):
            return None

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
        resp = self._client_get_with_retry(f"{GEMINI_BASE}/app", params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"获取 Gemini 页面失败: HTTP {resp.status_code}")

        html = resp.text

        # 提取 SNlM0e (at token)
        at_match = re.search(r'"SNlM0e":"([^"]+)"', html)
        if not at_match:
            raise RuntimeError("无法提取 CSRF token (SNlM0e)，可能未登录")
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

        if resp.status_code != 200:
            print(f"  [debug] 响应内容: {resp.text[:500]}")
            raise RuntimeError(f"batchexecute 失败: HTTP {resp.status_code}")

        results = parse_batchexecute_response(resp.text)
        for rid, data_inner in results:
            if rid == rpcid:
                return data_inner

        print(f"  [debug] 响应中未找到 {rpcid}，已解析 {len(results)} 项: "
              f"{[r[0] for r in results]}")
        print(f"  [debug] 原始响应 (前500字符): {resp.text[:500]}")
        raise RuntimeError(f"响应中未找到 {rpcid} 数据")

    # ------------------------------------------------------------------
    # 获取聊天列表
    # ------------------------------------------------------------------
    def get_all_chats(self):
        """获取所有聊天列表（含分页）"""
        print("[*] 获取聊天列表...")
        all_chats = []
        page = 0

        # 第一页
        payload = json.dumps([BATCH_SIZE, None, [0, None, 1]])
        result = self._batchexecute("MaZiqc", payload, source_path="/app")

        while True:
            page += 1

            if not result or not isinstance(result, list):
                print(f"  [debug] 异常响应: type={type(result)}, value={str(result)[:200]}")
                break

            if len(result) < 3 or not result[2]:
                print(f"  [debug] 结果为空或结构异常: len={len(result)}, "
                      f"preview={json.dumps(result, ensure_ascii=False)[:300]}")
                break

            chats = result[2]
            for chat in chats:
                if isinstance(chat, list) and len(chat) > 1:
                    conv_id = chat[0]  # e.g. "c_5c762430d8391f18"
                    title = chat[1] if len(chat) > 1 else ""
                    latest_update_ts = self._extract_chat_latest_update(chat)
                    all_chats.append({
                        "id": conv_id,
                        "title": title,
                        "latest_update_ts": latest_update_ts,
                        "latest_update_iso": self._to_iso_utc(latest_update_ts),
                    })

            print(f"  第 {page} 页: {len(chats)} 个对话 (累计 {len(all_chats)})")

            # 检查分页 token
            next_token = result[1] if len(result) > 1 else None
            if not next_token or not isinstance(next_token, str):
                break

            time.sleep(REQUEST_DELAY)
            payload = json.dumps([BATCH_SIZE, next_token])
            result = self._batchexecute("MaZiqc", payload, source_path="/app")

        print(f"  共 {len(all_chats)} 个对话")
        return all_chats

    # ------------------------------------------------------------------
    # 获取对话详情
    # ------------------------------------------------------------------
    def get_chat_detail(self, conv_id):
        """获取单个对话的完整内容（含分页）"""
        all_turns = []
        page = 0

        # 第一页
        payload = json.dumps([conv_id, DETAIL_PAGE_SIZE, None, 1, [1], [4], None, 1])
        source_path = f"/app/{conv_id.replace('c_', '')}"

        result = self._batchexecute("hNvQHb", payload, source_path=source_path)

        while True:
            page += 1

            if not result or not result[0]:
                break

            turns = result[0]
            all_turns.extend(turns)

            # 检查分页 token
            next_token = result[1] if len(result) > 1 and isinstance(result[1], str) else None
            if not next_token:
                break

            time.sleep(REQUEST_DELAY)
            payload = json.dumps([conv_id, DETAIL_PAGE_SIZE, next_token, 1, [1], [4], None, 1])
            result = self._batchexecute("hNvQHb", payload, source_path=source_path)

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

            time.sleep(REQUEST_DELAY)
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

            # AI 生成/关联的媒体 (content[0][4][0][4])
            if (len(msg) > 4 and msg[4] is not None
                    and isinstance(msg[4], list) and len(msg[4]) > 0
                    and isinstance(msg[4][0], list) and len(msg[4][0]) > 4
                    and msg[4][0][4] is not None):
                ai_media = msg[4][0][4]
                for f in ai_media:
                    if isinstance(f, list):
                        result["assistant"]["files"].append(
                            GeminiExporter._parse_media_item(f, "assistant")
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

    # ------------------------------------------------------------------
    # 批量下载媒体文件（无 CDP）
    # ------------------------------------------------------------------
    def download_media_batch(self, media_list, media_dir, stats):
        """
        批量下载媒体文件（按用户上下文顺序下载）
        media_list: [{"url": ..., "filepath": Path}, ...]
        """
        self._download_media_batch_no_cdp(media_list, stats)

    def _build_media_cookie_header(self):
        return "; ".join(
            f"{k}={self.cookies[k]}" for k in GOOGLE_MEDIA_COOKIE_NAMES if k in self.cookies
        )

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

    def _download_one_media_no_cdp(self, url, cookie_header, referer):
        try:
            import curl_cffi.requests as curl_requests
        except ImportError:
            os.system(f"{sys.executable} -m pip install curl_cffi")
            import curl_cffi.requests as curl_requests

        base_headers = {
            "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "referer": referer,
            "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "image",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "cross-site",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/144.0.0.0 Safari/537.36"
            ),
        }

        current_url = url
        for _ in range(8):
            headers = dict(base_headers)
            host = (urlparse(current_url).hostname or "").lower()
            if host in PROTECTED_MEDIA_HOSTS:
                headers["cookie"] = cookie_header

            resp = curl_requests.get(
                current_url,
                headers=headers,
                allow_redirects=False,
                timeout=45,
            )

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    return None
                current_url = urljoin(current_url, location)
                continue

            if resp.status_code == 200:
                return resp.content

            return None

        return None

    def _download_media_batch_no_cdp(self, media_list, stats):
        if not media_list:
            return

        # 对齐账号上下文（与 --user 一致）
        authuser = self._authuser_params().get("authuser")
        try:
            self.client.get(f"{GEMINI_BASE}/app", params=self._authuser_params())
        except Exception:
            pass

        cookie_header = self._build_media_cookie_header()
        referer = f"{GEMINI_BASE}/u/{authuser}/app" if authuser is not None else f"{GEMINI_BASE}/app"

        for item in media_list:
            filepath = item["filepath"]
            url = item["url"]

            if filepath.exists():
                stats["media_downloaded"] += 1
                continue

            candidates = [url, self._append_authuser(url, authuser)] if authuser is not None else [url]

            content = None
            for candidate_url in candidates:
                content = self._download_one_media_no_cdp(candidate_url, cookie_header, referer)
                if content:
                    break

            if content:
                filepath.parent.mkdir(parents=True, exist_ok=True)
                filepath.write_bytes(content)
                stats["media_downloaded"] += 1
            else:
                stats["media_failed"] += 1

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
    def _write_jsonl_rows(jsonl_file, rows):
        with open(jsonl_file, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

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
            safe_id = re.sub(r"[^a-z0-9]", "_", email.lower())
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
    def _turns_to_jsonl_rows(parsed_turns, conv_id, account_id, title, chat_info):
        """将 parsed_turns 转为新 JSONL 格式行列表（meta 首行 + message 行）"""
        now_iso = datetime.datetime.now(datetime.UTC).isoformat()
        bare_id = conv_id.replace("c_", "")

        ts_list = [t["timestamp"] for t in parsed_turns if isinstance(t.get("timestamp"), int)]
        created_at = GeminiExporter._to_iso_utc(min(ts_list)) if ts_list else now_iso
        updated_at = GeminiExporter._to_iso_utc(max(ts_list)) if ts_list else now_iso
        if not updated_at:
            updated_at = chat_info.get("latest_update_iso") or now_iso
        if not created_at:
            created_at = updated_at

        remote_hash = (
            str(chat_info["latest_update_ts"]) if chat_info.get("latest_update_ts") else None
        )

        rows = [{
            "type": "meta",
            "id": bare_id,
            "accountId": account_id,
            "title": title,
            "createdAt": created_at,
            "updatedAt": updated_at,
            "syncedAt": now_iso,
            "remoteHash": remote_hash,
        }]

        # parsed_turns 逆序（新→旧），反转为正序（旧→新）输出
        for turn in reversed(parsed_turns):
            turn_id = turn.get("turn_id") or uuid.uuid4().hex
            ts = GeminiExporter._to_iso_utc(turn.get("timestamp")) or now_iso

            user = turn.get("user", {})
            user_attachments = [
                {"mediaId": f["media_id"], "mimeType": f.get("mime") or ""}
                for f in user.get("files", []) if f.get("media_id")
            ]
            rows.append({
                "type": "message",
                "id": f"{turn_id}_u",
                "role": "user",
                "text": user.get("text", ""),
                "attachments": user_attachments,
                "timestamp": ts,
            })

            asst = turn.get("assistant", {})
            asst_attachments = [
                {"mediaId": f["media_id"], "mimeType": f.get("mime") or ""}
                for f in asst.get("files", []) if f.get("media_id")
            ]
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
                    target = global_media_dir / fname
                    if not target.exists() and not any(item["filepath"] == target for item in batch_list):
                        batch_list.append({"url": url, "filepath": target})

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

            time.sleep(REQUEST_DELAY)
            payload = json.dumps([conv_id, DETAIL_PAGE_SIZE, next_token, 1, [1], [4], None, 1])
            result = self._batchexecute("hNvQHb", payload, source_path=source_path)

        return all_new_turns

    # ------------------------------------------------------------------
    # 主导出流程
    # ------------------------------------------------------------------
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
        """
        now_iso = datetime.datetime.now(datetime.UTC).isoformat()
        base_dir = Path(output_dir) if output_dir else OUTPUT_DIR
        base_dir.mkdir(parents=True, exist_ok=True)

        # 1. 初始化认证
        self.init_auth()

        # 2. 解析账号信息
        account_info = self._resolve_account_info()
        account_id = account_info["id"]
        account_dir = base_dir / "accounts" / account_id
        conv_dir = account_dir / "conversations"
        media_dir = account_dir / "media"

        account_dir.mkdir(parents=True, exist_ok=True)
        conv_dir.mkdir(parents=True, exist_ok=True)
        media_dir.mkdir(parents=True, exist_ok=True)

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
        stats = {"success": 0, "failed": 0, "media_downloaded": 0, "media_failed": 0}

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
            print(f"\n[{idx + 1}/{total}] {title} ({conv_id})")

            try:
                raw_turns = self.get_chat_detail(conv_id)
                print(f"  轮次: {len(raw_turns)}")

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
                if batch_list:
                    print(f"  媒体文件: {len(batch_list)} 个（去重后）")
                    self.download_media_batch(batch_list, media_dir, stats)
                    self._save_media_manifest_new(account_dir, global_seen_urls)

                # 构建 summary
                meta_row = rows[0]
                msg_rows = [r for r in rows if r.get("type") == "message"]
                has_media = any(r.get("attachments") for r in msg_rows)
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
                    "updatedAt": meta_row.get("updatedAt"),
                    "syncedAt": meta_row.get("syncedAt"),
                    "remoteHash": meta_row.get("remoteHash"),
                })
                stats["success"] += 1

            except Exception as e:
                print(f"  [!] 导出失败: {e}")
                import traceback; traceback.print_exc()
                stats["failed"] += 1
                failed_ids.append(bare_id)
                conv_summaries.append({
                    "id": bare_id,
                    "title": title,
                    "lastMessage": "",
                    "messageCount": 0,
                    "hasMedia": False,
                    "updatedAt": chat.get("latest_update_iso"),
                    "syncedAt": None,
                    "remoteHash": None,
                })

            time.sleep(REQUEST_DELAY)

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
        print(f"  输出目录: {account_dir.absolute()}")

    def export_incremental(self, output_dir=None):
        """
        增量导出：
        - 按聊天列表新到旧扫描
        - 命中第一个未更新会话后停止继续下探
        - 对更新会话仅抓取新增 turn（遇到本地已存在 turn_id 即停止）
        """
        now_iso = datetime.datetime.now(datetime.UTC).isoformat()
        base_dir = Path(output_dir) if output_dir else OUTPUT_DIR
        base_dir.mkdir(parents=True, exist_ok=True)

        self.init_auth()

        # 解析账号信息
        account_info = self._resolve_account_info()
        account_id = account_info["id"]
        account_dir = base_dir / "accounts" / account_id
        conv_dir = account_dir / "conversations"
        media_dir = account_dir / "media"

        account_dir.mkdir(parents=True, exist_ok=True)
        conv_dir.mkdir(parents=True, exist_ok=True)
        media_dir.mkdir(parents=True, exist_ok=True)

        print(f"[*] 账号: {account_info['email'] or account_id}")

        chats = self.get_all_chats()
        if not chats:
            print("[!] 未找到任何对话")
            return

        account_info["remoteConversationCount"] = len(chats)

        global_seen_urls = self._load_media_manifest_new(account_dir)
        global_used_names = set(global_seen_urls.values())
        for f in media_dir.iterdir():
            if f.is_file():
                global_used_names.add(f.name)

        stats = {"updated": 0, "checked": 0, "media_downloaded": 0, "media_failed": 0}
        stop_chat = None

        # 读取现有 conversations.json 构建索引
        conv_index = {}
        conv_index_file = account_dir / "conversations.json"
        if conv_index_file.exists():
            try:
                data = json.loads(conv_index_file.read_text(encoding="utf-8"))
                for item in data.get("items", []):
                    conv_index[item["id"]] = item
            except Exception:
                pass

        for chat in chats:
            stats["checked"] += 1
            conv_id = chat["id"]
            bare_id = conv_id.replace("c_", "")
            title = chat.get("title", "")
            jsonl_file = conv_dir / f"{bare_id}.jsonl"

            local_hash = self._remote_hash_from_jsonl(jsonl_file)
            remote_latest_ts = chat.get("latest_update_ts")

            # 命中首个未更新会话，停止继续下探
            if local_hash is not None and remote_latest_ts is not None:
                try:
                    if int(remote_latest_ts) <= int(local_hash):
                        stop_chat = conv_id
                        print(f"[*] 命中未更新会话，停止: {conv_id}")
                        break
                except (TypeError, ValueError):
                    pass

            print(f"\n[*] 增量检查: {title} ({conv_id})")
            existing_ids = self._build_existing_turn_id_set_new(jsonl_file)
            raw_new_turns = self.get_chat_detail_incremental(conv_id, existing_ids)

            if not raw_new_turns:
                print("  无新增 turn")
                time.sleep(REQUEST_DELAY)
                continue

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
            all_rows = [new_meta] + new_msg_rows + existing_msg_rows
            self._write_jsonl_rows(jsonl_file, all_rows)

            if batch_list:
                self.download_media_batch(batch_list, media_dir, stats)
                self._save_media_manifest_new(account_dir, global_seen_urls)

            # 更新 conv_index
            all_msg_rows = new_msg_rows + existing_msg_rows
            has_media = any(r.get("attachments") for r in all_msg_rows)
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
                "updatedAt": new_meta.get("updatedAt"),
                "syncedAt": new_meta.get("syncedAt"),
                "remoteHash": new_meta.get("remoteHash"),
            }

            print(f"  新增 turn: {len(parsed_new_turns)}")
            stats["updated"] += 1
            time.sleep(REQUEST_DELAY)

        # 为 chat_list 中出现但本地无记录的对话补充占位 summary
        for chat in chats:
            bare_id = chat["id"].replace("c_", "")
            if bare_id not in conv_index:
                conv_index[bare_id] = {
                    "id": bare_id,
                    "title": chat.get("title", ""),
                    "lastMessage": "",
                    "messageCount": 0,
                    "hasMedia": False,
                    "updatedAt": chat.get("latest_update_iso"),
                    "syncedAt": None,
                    "remoteHash": None,
                }

        # 按 chats 原顺序排列 summaries
        summaries = []
        seen_ids = set()
        for chat in chats:
            bare_id = chat["id"].replace("c_", "")
            if bare_id not in seen_ids:
                seen_ids.add(bare_id)
                if bare_id in conv_index:
                    summaries.append(conv_index[bare_id])

        # 写入账号结构文件
        account_info["conversationCount"] = len([s for s in summaries if s.get("syncedAt")])
        account_info["lastSyncAt"] = now_iso
        account_info["lastSyncResult"] = "success"
        self._write_accounts_json(base_dir, account_info)
        self._write_account_meta(account_dir, account_info)
        self._write_conversations_index(account_dir, account_id, now_iso, summaries)
        self._write_sync_state(account_dir, {
            "version": 1,
            "accountId": account_id,
            "updatedAt": now_iso,
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
        print(f"  输出目录: {account_dir.absolute()}")


# ============================================================================
# 迁移工具
# ============================================================================
def migrate_old_to_new(old_dir, new_base_dir, account_id, email="", name=""):
    """
    将旧格式目录（{bare_id}.jsonl + chat_list*.json + media/）迁移到新格式。

    old_dir      : 旧格式数据目录
    new_base_dir : 新格式根目录（accounts.json 在此级别）
    account_id   : 账号 ID（如 sanitized email）
    """
    old_dir = Path(old_dir)
    new_base_dir = Path(new_base_dir)
    now_iso = datetime.datetime.now(datetime.UTC).isoformat()

    account_dir = new_base_dir / "accounts" / account_id
    conv_dir = account_dir / "conversations"
    media_dir = account_dir / "media"

    account_dir.mkdir(parents=True, exist_ok=True)
    conv_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)

    # 读取聊天列表
    chats = []
    for fname in ["chat_list_union.json", "chat_list.json", "chat_list_latest.json"]:
        candidate = old_dir / fname
        if candidate.exists():
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    chats = raw
                elif isinstance(raw, dict) and "conversations" in raw:
                    chats = raw["conversations"]
                if chats:
                    print(f"[migrate] 聊天列表: {fname} ({len(chats)} 个)")
                    break
            except Exception:
                pass

    chat_info_map = {}
    for chat in chats:
        cid = chat.get("id", "")
        bare_id = cid.replace("c_", "")
        chat_info_map[bare_id] = chat

    # 迁移媒体文件（创建软链接）
    old_media_dir = old_dir / "media"
    if old_media_dir.exists():
        for f in old_media_dir.iterdir():
            if f.is_file():
                target = media_dir / f.name
                if not target.exists():
                    try:
                        target.symlink_to(f.resolve())
                    except Exception:
                        pass  # fallback: skip if symlink fails
        print(f"[migrate] 媒体文件软链接完成: {media_dir}")

    # 迁移媒体清单
    old_manifest = old_dir / "media_manifest.json"
    if old_manifest.exists():
        try:
            manifest_data = json.loads(old_manifest.read_text(encoding="utf-8"))
            GeminiExporter._save_media_manifest_new(account_dir, manifest_data.get("url_to_name", {}))
        except Exception:
            pass

    # 迁移每个对话 JSONL
    jsonl_files = sorted(old_dir.glob("*.jsonl"))
    print(f"[migrate] 发现 {len(jsonl_files)} 个 JSONL 文件")

    conv_summaries = []

    for jsonl_file in jsonl_files:
        bare_id = jsonl_file.stem
        chat = chat_info_map.get(bare_id, {
            "id": f"c_{bare_id}",
            "title": bare_id,
            "latest_update_ts": None,
            "latest_update_iso": None,
        })
        conv_id = chat.get("id", f"c_{bare_id}")
        title = chat.get("title", bare_id)

        old_turns = GeminiExporter._read_jsonl_rows(jsonl_file)
        rows = GeminiExporter._turns_to_jsonl_rows(old_turns, conv_id, account_id, title, chat)

        new_jsonl = conv_dir / f"{bare_id}.jsonl"
        GeminiExporter._write_jsonl_rows(new_jsonl, rows)

        meta_row = rows[0]
        msg_rows = [r for r in rows if r.get("type") == "message"]
        has_media = any(r.get("attachments") for r in msg_rows)
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
            "updatedAt": meta_row.get("updatedAt"),
            "syncedAt": meta_row.get("syncedAt"),
            "remoteHash": meta_row.get("remoteHash"),
        })

    # 写入账号结构文件
    avatar_text = (email.split("@")[0][0].upper() if email else account_id[0].upper())
    account_info = {
        "id": account_id,
        "email": email,
        "name": name or (email.split("@")[0] if email else account_id),
        "avatarText": avatar_text,
        "avatarColor": "#667eea",
        "conversationCount": len(conv_summaries),
        "remoteConversationCount": len(chats) or None,
        "lastSyncAt": now_iso,
        "lastSyncResult": "success",
        "authuser": None,
    }
    GeminiExporter._write_accounts_json(new_base_dir, account_info)
    GeminiExporter._write_account_meta(account_dir, account_info)
    GeminiExporter._write_conversations_index(account_dir, account_id, now_iso, conv_summaries)
    GeminiExporter._write_sync_state(account_dir, {
        "version": 1,
        "accountId": account_id,
        "updatedAt": now_iso,
        "concurrency": 3,
        "fullSync": {
            "phase": "done",
            "startedAt": now_iso,
            "listingCursor": None,
            "listingTotal": len(chats),
            "listingFetched": len(chats),
            "conversationsToFetch": [],
            "conversationsFetched": len(conv_summaries),
            "conversationsFailed": [],
            "completedAt": now_iso,
            "errorMessage": None,
        },
        "pendingConversations": [],
    })

    print(f"[migrate] 完成: {len(conv_summaries)} 个对话已迁移")
    print(f"[migrate] 新格式目录: {account_dir.absolute()}")
    return account_dir


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
    parser.add_argument("--check-chat-id", help="检查指定对话是否更新（需配合 --last-update-ts）")
    parser.add_argument("--last-update-ts", type=int, help="上次记录的对话更新时间（秒级时间戳）")
    parser.add_argument("--incremental", action="store_true", help="增量更新导出（命中首个未更新会话后停止）")
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
    exporter = GeminiExporter(cookies, user=args.user)
    if args.list_users:
        rows = exporter.list_user_options()
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    # 3. 鉴权校验
    auth_ready = False
    try:
        exporter.init_auth()
        auth_ready = True
    except Exception as e:
        print(f"[!] cookies 鉴权失败: {e}")
        print("    请确认浏览器已登录 Gemini，或使用 --cookies-file 提供可用 cookie")
        sys.exit(1)

    # 4. 导出 / 仅列表测试
    if args.check_chat_id:
        if args.last_update_ts is None:
            print("[!] 使用 --check-chat-id 时必须提供 --last-update-ts")
            sys.exit(1)
        if not auth_ready:
            exporter.init_auth()
        check_result = exporter.is_chat_updated(
            exporter.normalize_chat_id(args.check_chat_id),
            args.last_update_ts,
        )
        print(json.dumps(check_result, ensure_ascii=False, indent=2))
        return

    if args.list_only:
        if not auth_ready:
            exporter.init_auth()
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
            return {
                "id": re.sub(r"[^a-z0-9]", "_", email),
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

    if args.incremental:
        exporter.export_incremental(output_dir=args.output)
        return

    exporter.export_all(output_dir=args.output, chat_ids=args.chat_ids)


if __name__ == "__main__":
    main()
