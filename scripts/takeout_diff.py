#!/usr/bin/env python3
"""
takeout_diff.py  —  Takeout 活动记录 vs 本地 app 对话的全量交叉对比

匹配策略：时间戳（±1s）为主键，用户文本前缀（前30字）做二次确认。
未匹配条目输出 Markdown 表格，附"疑似所属对话"（±120s 内有数据的对话 ID+title）。
分两个文件输出：有疑似ID / 无疑似ID。

用法：
  python3 scripts/takeout_diff.py --account cynaustraline_gmail_com
  python3 scripts/takeout_diff.py --account cynaustraline_gmail_com --out /tmp/diff.md
"""

import argparse
import bisect
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── 常量 ──────────────────────────────────────────────────────────────────────

APP_DATA_DEFAULT = (
    Path.home() / "Library" / "Application Support" / "com.gemini-collector.app"
)
TAKEOUT_DEFAULT = (
    Path.home()
    / "Downloads"
    / "Takeout 2"
    / "我的活动"
    / "Gemini Apps"
    / "我的活动记录_with_id.json"
)

PROMPTED_PREFIX = "Prompted "
TEXT_PREFIX_LEN = 30   # 用户文本前缀比对长度
PREVIEW_LEN     = 80   # 报告中预览截断长度
TS_TOLERANCE    = 1    # 时间戳容差（秒）
TS_NEARBY       = 120  # 疑似对话搜索窗口（秒）

# 模型拒绝/负向关键词，命中则过滤该条目
REFUSAL_KEYWORDS = [
    # 中文
    "我是一个人工智能", "我是人工智能", "作为人工智能", "作为一个AI", "作为AI",
    "无法继续", "无法提供帮助", "无法满足", "无法协助", "无法完成",
    "不能提供帮助", "不能满足", "不能协助",
    "安全准则", "使用准则", "内容政策", "违反政策", "违反准则",
    "负责任", "道德准则", "不道德", "有害内容",
    # 踩刹车类
    "踩刹车", "踩一脚刹车", "必须停止", "需要停止", "暂停一下",
    "在这里停下", "我要停", "停下来",
    # 道歉/遗憾类
    "抱歉", "非常抱歉", "很抱歉", "深感抱歉", "对不起",
    "遗憾", "很遗憾", "非常遗憾",
    # 帮助/服务类
    "提供帮助", "为您提供帮助", "无法为您", "无法帮助", "无法帮您",
    "帮助您完成", "乐意帮助", "竭诚为您", "很乐意为您",
    # 程序/设计局限类
    "没法在这方面帮", "无法在这方面帮", "我的设计用途只是处理和生成文本",
    "程序代码的局限", "无法提供这方面的帮助",
    # 英文
    "i'm an ai", "i am an ai", "as an ai", "as a language model",
    "i cannot", "i can't", "i'm unable", "i am unable", "i'm not able",
    "cannot fulfill", "cannot assist", "cannot provide",
    "safety guidelines", "usage policies", "content policy",
    "against my guidelines", "against my values", "i apologize",
    "harmful", "inappropriate", "violates",
]

SHORT_ENGLISH_MAX = 80   # 纯英文且不超过此字数时过滤


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def ts_epoch(ts: str) -> float:
    return parse_ts(ts).timestamp()


def strip_html(h: str) -> str:
    """去掉 HTML 标签，还原实体，折叠空白。"""
    text = re.sub(r"<[^>]+>", " ", h)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def user_prefix(text: str) -> str:
    return text[:TEXT_PREFIX_LEN]


def preview(text: str) -> str:
    t = text.replace("\n", " ").strip()
    return t[:PREVIEW_LEN] + ("…" if len(t) > PREVIEW_LEN else "")


def is_refusal(model_plain: str) -> bool:
    """模型回复纯文本是否包含拒绝/负向关键词。"""
    lower = model_plain.lower()
    return any(kw.lower() in lower for kw in REFUSAL_KEYWORDS)


def is_short_english(model_plain: str) -> bool:
    """纯英文（无中日韩字符）且长度不超过阈值。"""
    if len(model_plain) > SHORT_ENGLISH_MAX:
        return False
    return not re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", model_plain)


# ── 索引构建 ──────────────────────────────────────────────────────────────────

def build_app_index(account_dir: Path) -> tuple[dict, dict, dict]:
    """
    返回：
      index        {epoch_int: [(epoch_float, user_text, conv_id), ...]}  ±1s 匹配用
      title_map    {conv_id: title}
      conv_ts_map  {conv_id: sorted list of epoch_float}  用于判断前后数据
    """
    index: dict[int, list] = {}
    conv_ts_map: dict[str, list] = {}
    conv_dir = account_dir / "conversations"

    # 读取对话标题
    title_map: dict[str, str] = {}
    index_file = account_dir / "conversations.json"
    if index_file.exists():
        try:
            data = json.loads(index_file.read_text(encoding="utf-8"))
            for item in data.get("items", []):
                title_map[item["id"]] = item.get("title", "")
        except Exception:
            pass

    for jsonl in conv_dir.glob("*.jsonl"):
        conv_id = jsonl.stem
        try:
            lines = jsonl.read_text(encoding="utf-8").strip().split("\n")
        except Exception:
            continue
        for line in lines[1:]:  # 跳过 meta
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            ts = msg.get("timestamp", "")
            if not ts:
                continue
            try:
                ep = ts_epoch(ts)
            except Exception:
                continue
            # 所有 role 都计入 conv_ts_map（判断前后数据用）
            conv_ts_map.setdefault(conv_id, []).append(ep)
            # 仅 user turn 计入匹配索引
            if msg.get("role") != "user":
                continue
            key = int(ep)
            index.setdefault(key, []).append((ep, msg.get("text", ""), conv_id))

    # 对每条对话的时间列表排序
    for cid in conv_ts_map:
        conv_ts_map[cid].sort()

    return index, title_map, conv_ts_map


def find_nearby_conv(ep: float, index: dict, title_map: dict) -> tuple[str, str]:
    """在 ±TS_NEARBY 窗口内找最近的 app turn，返回 (conv_id, title)，无则返回空字符串。"""
    key = int(ep)
    best = None
    best_dist = float("inf")
    for k in range(key - TS_NEARBY, key + TS_NEARBY + 1):
        for (c_ep, c_text, c_conv) in index.get(k, []):
            dist = abs(c_ep - ep)
            if dist <= TS_NEARBY and dist < best_dist:
                best_dist = dist
                best = c_conv
    if best:
        return best, title_map.get(best, "")
    return "", ""


def has_data_around(conv_id: str, ep: float, conv_ts_map: dict) -> bool:
    """疑似对话中，ep 前后各有至少一条数据（不限时间范围）。"""
    ts_list = conv_ts_map.get(conv_id, [])
    if not ts_list:
        return False
    pos = bisect.bisect_left(ts_list, ep)
    has_before = pos > 0
    has_after = pos < len(ts_list) and ts_list[pos] > ep
    return has_before and has_after


# ── 匹配 ──────────────────────────────────────────────────────────────────────

def match_entry(entry: dict, index: dict) -> tuple[bool, str]:
    """
    返回 (matched, reason)
    reason: "exact" / "ts_only" / "none"
    """
    try:
        ep = ts_epoch(entry["time"])
    except Exception:
        return False, "ts_parse_error"

    key = int(ep)
    candidates = []
    for k in (key - 1, key, key + 1):
        candidates.extend(index.get(k, []))

    if not candidates:
        return False, "none"

    user_text = entry.get("title", "")
    if user_text.startswith(PROMPTED_PREFIX):
        user_text = user_text[len(PROMPTED_PREFIX):]
    pfx = user_prefix(user_text)

    # 时间窗口内的候选，按时间距离排序
    candidates.sort(key=lambda c: abs(c[0] - ep))

    for (c_ep, c_text, c_conv) in candidates:
        if abs(c_ep - ep) > TS_TOLERANCE:
            continue
        if pfx and user_prefix(c_text) == pfx:
            return True, "exact"

    # 时间命中但文本不符（可能截断或微小差异），作为宽松匹配
    for (c_ep, c_text, c_conv) in candidates:
        if abs(c_ep - ep) <= TS_TOLERANCE:
            return True, "ts_only"

    return False, "none"


# ── 主逻辑 ────────────────────────────────────────────────────────────────────

def run_diff(account_id: str, takeout_path: Path, app_data: Path, out_path: Path):
    account_dir = app_data / "accounts" / account_id

    print(f"加载本地对话索引：{account_dir / 'conversations'}")
    index, title_map, conv_ts_map = build_app_index(account_dir)
    total_app_turns = sum(len(v) for v in index.values())
    print(f"  user turn 总数：{total_app_turns}，唯一秒级时间戳：{len(index)}")

    print(f"加载 Takeout：{takeout_path}")
    takeout = json.loads(takeout_path.read_text(encoding="utf-8"))
    print(f"  条目总数：{len(takeout)}")

    matched_exact = 0
    matched_ts    = 0
    unmatched     = []

    for entry in takeout:
        ok, reason = match_entry(entry, index)
        if ok:
            if reason == "exact":
                matched_exact += 1
            else:
                matched_ts += 1
        else:
            unmatched.append(entry)

    # 过滤无效回复
    cnt_no_reply = cnt_refusal = cnt_short_en = 0
    filtered_unmatched = []
    for entry in unmatched:
        safe = entry.get("safeHtmlItem")
        if not safe:
            cnt_no_reply += 1
            continue
        try:
            model_plain = strip_html(safe[0]["html"])
        except Exception:
            model_plain = ""
        if not model_plain:
            cnt_no_reply += 1
        elif is_refusal(model_plain):
            cnt_refusal += 1
        elif is_short_english(model_plain):
            cnt_short_en += 1
        else:
            filtered_unmatched.append(entry)
    unmatched = filtered_unmatched
    print(f"  过滤无模型回复：{cnt_no_reply} 条  拒绝回复：{cnt_refusal} 条  纯英文短回复：{cnt_short_en} 条")

    # 为每条未匹配条目查找疑似所属对话 + 判断前后数据
    print(f"查找疑似对话（±{TS_NEARBY}s 窗口）...")
    nearby_info: list[tuple[str, str, str]] = []  # (conv_id, title, around)
    for entry in unmatched:
        try:
            ep = ts_epoch(entry["time"])
            conv_id, title = find_nearby_conv(ep, index, title_map)
            if conv_id:
                around = "有" if has_data_around(conv_id, ep, conv_ts_map) else "无"
            else:
                around = ""
        except Exception:
            conv_id, title, around = "", "", ""
        nearby_info.append((conv_id, title, around))

    print()
    print(f"匹配结果：")
    print(f"  精确匹配（时间+文本前缀）：{matched_exact}")
    print(f"  宽松匹配（仅时间戳）      ：{matched_ts}")
    print(f"  未匹配（缺失）            ：{len(unmatched)}")
    print()

    # ── 输出 Markdown 报告（分两个文件）────────────────────────────────────────
    def md_cell(s: str) -> str:
        return s.replace("|", "\\|").replace("\n", " ")

    base = out_path.with_suffix("")
    path_with    = Path(str(base) + "_with_conv.md")
    path_without = Path(str(base) + "_no_conv.md")

    header_with    = "| # | takeout_id | 时间 | 用户文本 | 模型回复 | 疑似ID | 疑似title | 前后数据 |"
    sep_with       = "| --- | --- | --- | --- | --- | --- | --- | --- |"
    header_without = "| # | takeout_id | 时间 | 用户文本 | 模型回复 |"
    sep_without    = "| --- | --- | --- | --- | --- |"

    rows_with: list[str] = []
    rows_without: list[str] = []

    for entry, (nearby_id, nearby_title, around) in zip(unmatched, nearby_info):
        tid    = entry.get("id", "")
        time_s = entry.get("time", "")[:19].replace("T", " ")

        user_raw = entry.get("title", "")
        if user_raw.startswith(PROMPTED_PREFIX):
            user_raw = user_raw[len(PROMPTED_PREFIX):]
        user_p = md_cell(preview(user_raw))

        safe = entry.get("safeHtmlItem")
        if safe:
            try:
                model_p = md_cell(preview(strip_html(safe[0]["html"])))
            except Exception:
                model_p = "(解析失败)"
        else:
            model_p = "(无模型回复)"

        if nearby_id:
            rows_with.append(
                f"| {len(rows_with)+1} | {tid} | {time_s} | {user_p} | {model_p} "
                f"| {nearby_id} | {md_cell(nearby_title[:40])} | {around} |"
            )
        else:
            rows_without.append(
                f"| {len(rows_without)+1} | {tid} | {time_s} | {user_p} | {model_p} |"
            )

    path_with.write_text(
        "\n".join([header_with, sep_with] + rows_with) + "\n", encoding="utf-8"
    )
    path_without.write_text(
        "\n".join([header_without, sep_without] + rows_without) + "\n", encoding="utf-8"
    )

    print(f"报告（有疑似ID）已写入：{path_with}  共 {len(rows_with)} 条")
    print(f"报告（无疑似ID）已写入：{path_without}  共 {len(rows_without)} 条")

    # 控制台打印前10条有疑似ID的预览
    if rows_with:
        print()
        print(f"{'#':<4} {'时间':<19} {'用户文本预览':<35} {'疑似ID':<18} {'前后'}")
        print("─" * 90)
        shown = 0
        for i, (entry, (nid, ntitle, around)) in enumerate(
            zip(unmatched, nearby_info), 1
        ):
            if not nid:
                continue
            user_raw = entry.get("title", "")
            if user_raw.startswith(PROMPTED_PREFIX):
                user_raw = user_raw[len(PROMPTED_PREFIX):]
            t = entry.get("time", "")[:19].replace("T", " ")
            print(f"{i:<4} {t:<19} {user_raw[:35]:<35} {nid:<18} {around}")
            shown += 1
            if shown >= 10:
                break


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Takeout vs 本地对话全量交叉对比")
    parser.add_argument("--account", required=True, help="账号 ID")
    parser.add_argument(
        "--takeout-file",
        default=str(TAKEOUT_DEFAULT),
        help="Takeout JSON 文件路径",
    )
    parser.add_argument(
        "--app-data",
        default=str(APP_DATA_DEFAULT),
        help="app 数据根目录",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="报告输出路径基础名（默认：scripts/takeout_diff_report.md）",
    )
    args = parser.parse_args()

    takeout_path = Path(args.takeout_file).expanduser()
    app_data     = Path(args.app_data).expanduser()
    out_path     = Path(args.out) if args.out else (
        Path(__file__).parent / "takeout_diff_report.md"
    )

    if not takeout_path.exists():
        print(f"错误：Takeout 文件不存在：{takeout_path}", file=sys.stderr)
        sys.exit(1)
    if not (app_data / "accounts" / args.account).exists():
        print(f"错误：账号目录不存在", file=sys.stderr)
        sys.exit(1)

    run_diff(args.account, takeout_path, app_data, out_path)


if __name__ == "__main__":
    main()
