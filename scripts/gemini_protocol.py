"""
Gemini 协议常量、异常类、batchexecute 响应解析，及通用工具函数。
"""

import datetime
import json
import re
import time

# ============================================================================
# 配置常量
# ============================================================================
GEMINI_BASE = "https://gemini.google.com"
BATCH_SIZE = 20          # MaZiqc 每页数量
DETAIL_PAGE_SIZE = 10    # hNvQHb 每页数量

REQUEST_DELAY = 0.30
REQUEST_JITTER_MIN = 0.00
REQUEST_JITTER_MAX = 0.30
REQUEST_JITTER_MODE = 0.14
REQUEST_BACKOFF_MAX_SECONDS = 120.0
REQUEST_BACKOFF_LIMIT_FAILURES = 10

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
BROWSER_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"


# ============================================================================
# 异常类
# ============================================================================
class RequestBackoffLimitReachedError(RuntimeError):
    """请求连续失败触发最终退避兜底时抛出。"""
    pass


class SessionExpiredError(RuntimeError):
    """HTTP 200 但响应数据为空（服务端 session/cookie 已过期）时抛出。"""
    pass


# ============================================================================
# 通用工具
# ============================================================================
def timing_log(action: str, start_perf: float, **fields) -> None:
    elapsed_ms = (time.perf_counter() - start_perf) * 1000.0
    detail = " ".join(f"{k}={v}" for k, v in fields.items())
    if detail:
        print(f"  [timing] {action} {detail} elapsed={elapsed_ms:.1f}ms")
    else:
        print(f"  [timing] {action} elapsed={elapsed_ms:.1f}ms")


def _to_iso_utc(ts):
    if ts is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(int(ts), datetime.UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _coerce_epoch_seconds(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return int(s)
    return None


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


def _summary_to_epoch_seconds(summary):
    if not isinstance(summary, dict):
        return None
    remote_hash_ts = _coerce_epoch_seconds(summary.get("remoteHash"))
    if remote_hash_ts is not None:
        return remote_hash_ts
    return _iso_to_epoch_seconds(summary.get("updatedAt"))


def email_to_account_id(email):
    """账号目录 ID：邮箱小写后将非字母数字替换为下划线。"""
    if not isinstance(email, str):
        email = str(email or "")
    normalized = email.strip().lower()
    return re.sub(r"[^a-z0-9]", "_", normalized)


def mask_email(email):
    """返回脱敏后的邮箱，仅保留本地部分前3位，其余替换为 ***。"""
    if not isinstance(email, str) or not email:
        return email or ""
    at_pos = email.find("@")
    if at_pos <= 0:
        return email[:3] + "***" if len(email) > 3 else email
    local = email[:at_pos]
    domain = email[at_pos:]
    visible = local[:3]
    return visible + "***" + domain


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


def _extract_chat_latest_update(chat_item):
    """从聊天列表条目提取最新更新时间（秒级时间戳）"""
    if not isinstance(chat_item, list) or len(chat_item) <= 5:
        return None
    field = chat_item[5]
    if isinstance(field, list) and field and isinstance(field[0], int):
        return field[0]
    return None


def _request_backoff_seconds(consecutive_failures):
    if consecutive_failures < 3:
        return 0.0
    if consecutive_failures < 6:
        return float(min(4, 2 ** (consecutive_failures - 3)))
    if consecutive_failures < 9:
        return float(min(32, 8 * (2 ** (consecutive_failures - 6))))
    return float(min(REQUEST_BACKOFF_MAX_SECONDS, 60 * (2 ** (consecutive_failures - 9))))


# ============================================================================
# batchexecute 响应解析
# ============================================================================
def _iter_batchexecute_wrb_items(resp_text):
    """逐条产出 batchexecute 响应中的 wrb.fr 条目 (rpcid, raw_data)。
    raw_data 为字符串（有数据）或 None（服务端返回空/错误）。
    """
    body = resp_text
    if body.startswith(")]}'"):
        body = body[body.index('\n') + 1:]
    body = body.lstrip('\n\r')

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

        for line_data in chunk.split('\n'):
            line_data = line_data.strip()
            if not line_data:
                continue
            try:
                parsed = json.loads(line_data)
                if isinstance(parsed, list):
                    for item in parsed:
                        if (isinstance(item, list) and len(item) >= 2
                                and item[0] == 'wrb.fr'):
                            yield item[1], (item[2] if len(item) > 2 else None)
            except (json.JSONDecodeError, IndexError):
                pass


def parse_batchexecute_response(resp_text):
    """解析 Google batchexecute 响应格式，返回 [(rpcid, data), ...]（仅含有效数据条目）"""
    items = []
    for rpcid, raw in _iter_batchexecute_wrb_items(resp_text):
        if isinstance(raw, str):
            try:
                items.append((rpcid, json.loads(raw)))
            except (json.JSONDecodeError, IndexError):
                pass
    return items


def has_batchexecute_session_error(resp_text, rpcid):
    """检测响应中指定 rpcid 是否存在服务端会话错误（wrb.fr 条目存在但数据为 null）"""
    for rid, raw in _iter_batchexecute_wrb_items(resp_text):
        if rid == rpcid and raw is None:
            return True
    return False
