"""
Gemini 对话轮次解析：turn/media 描述项提取、占位 URL 清理。
"""

import re
import uuid
from urllib.parse import urlparse

from gemini_protocol import _to_iso_utc

INTERNAL_PLACEHOLDER_PATH_RE = re.compile(r"(?:^|/)[a-z0-9_]+_content(?:/|$)")


# ============================================================================
# 占位 URL 清理
# ============================================================================
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


# ============================================================================
# 媒体描述项工具
# ============================================================================
def _looks_like_http_url(value):
    return isinstance(value, str) and (
        value.startswith("https://") or value.startswith("http://")
    )


def _is_media_descriptor(item):
    """判断一个 list 是否像 Gemini 媒体描述项（图片/视频）。"""
    if not isinstance(item, list) or len(item) < 2:
        return False

    type_val = item[1]
    if type_val not in (1, 2, 4):
        return False

    has_url = False
    if len(item) > 3 and _looks_like_http_url(item[3]):
        has_url = True
    if not has_url and len(item) > 7 and isinstance(item[7], list):
        has_url = any(_looks_like_http_url(u) for u in item[7])
    if not has_url:
        return False

    has_name = len(item) > 2 and isinstance(item[2], str) and "." in item[2]
    has_mime = len(item) > 11 and isinstance(item[11], str) and "/" in item[11]
    return has_name or has_mime


def _extract_generated_media(ai_data):
    """从候选数据的 [12] 提取 AI 生成的音乐/视频媒体。
    返回 (files, music_meta, gen_meta)。
    """
    import json as _json
    files = []
    music_meta = None
    gen_meta = None

    if not isinstance(ai_data, list):
        return files, music_meta, gen_meta

    try:
        block12 = ai_data[12]
        if not isinstance(block12, list) or not block12:
            return files, music_meta, gen_meta

        last_idx = -1
        for idx in range(len(block12) - 1, -1, -1):
            if block12[idx] is not None:
                last_idx = idx
                break
        if last_idx < 0:
            return files, music_meta, gen_meta

        block = block12[last_idx]
        if not isinstance(block, list) or not block:
            return files, music_meta, gen_meta

        # 检测音乐块: block[6] 存在且含 "music_gen"
        is_music = (
            len(block) > 6
            and isinstance(block[6], list)
            and "music_gen" in _json.dumps(block[6:])
        )

        if is_music:
            for slot_idx in (0, 1):
                if len(block) > slot_idx and isinstance(block[slot_idx], list):
                    slot = block[slot_idx]
                    media_item = slot[1] if len(slot) > 1 and isinstance(slot[1], list) else None
                    if media_item:
                        files.append(_parse_media_item(media_item, "assistant"))

            if len(block) > 2 and isinstance(block[2], list):
                meta = block[2]
                music_meta = {
                    "title": meta[0] if len(meta) > 0 and isinstance(meta[0], str) else None,
                    "album": meta[2] if len(meta) > 2 and isinstance(meta[2], str) else None,
                    "genre": meta[4] if len(meta) > 4 and isinstance(meta[4], str) else None,
                    "moods": meta[5] if len(meta) > 5 and isinstance(meta[5], list) else [],
                }

            if len(block) > 3 and isinstance(block[3], list) and len(block[3]) > 3:
                caption = block[3][3]
                if isinstance(caption, str):
                    if music_meta is None:
                        music_meta = {}
                    music_meta["caption"] = caption

            return files, music_meta, gen_meta

        # 检测视频生成块
        try:
            inner = block[0]
            if isinstance(inner, list) and inner:
                group = inner[0]
                if isinstance(group, list) and len(group) >= 2:
                    media_items = group[0]
                    gen_info = group[1] if len(group) > 1 else None

                    if isinstance(media_items, list):
                        for m in media_items:
                            if isinstance(m, list) and len(m) > 1:
                                files.append(_parse_media_item(m, "assistant"))

                    if isinstance(gen_info, list) and gen_info:
                        prompt = gen_info[0] if isinstance(gen_info[0], str) else None
                        model = None
                        if len(gen_info) > 2 and isinstance(gen_info[2], list) and len(gen_info[2]) > 2:
                            model = gen_info[2][2] if isinstance(gen_info[2][2], str) else None
                        gen_meta = {"model": model, "prompt": prompt}
        except (IndexError, TypeError):
            pass

    except (IndexError, TypeError):
        pass

    return files, music_meta, gen_meta


def _collect_media_descriptors(node, out):
    if isinstance(node, list):
        if _is_media_descriptor(node):
            out.append(node)
            return
        for child in node:
            _collect_media_descriptors(child, out)


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


def _pick_preferred_media_descriptor(items):
    valid = [it for it in items if _is_media_descriptor(it)]
    if not valid:
        return None

    def _score(item):
        size_hint = _media_descriptor_size_hint(item)
        mime = item[11] if len(item) > 11 and isinstance(item[11], str) else ""
        is_png = 1 if mime == "image/png" else 0
        return (size_hint, is_png)

    return max(valid, key=_score)


def _collect_primary_media_descriptors(node, out):
    """
    处理 image_generation 的双格式结构（常见于同一图同时给 png/jpeg）。
    命中同层 3/6 槽位时只保留一份主资源，避免重复渲染。
    """
    if not isinstance(node, list):
        return

    if _is_media_descriptor(node):
        out.append(node)
        return

    slot_candidates = []
    for idx in (3, 6):
        if len(node) > idx and isinstance(node[idx], list):
            item = node[idx]
            if _is_media_descriptor(item):
                slot_candidates.append(item)
    if slot_candidates:
        preferred = _pick_preferred_media_descriptor(slot_candidates)
        if preferred is not None:
            out.append(preferred)
        return

    for child in node:
        _collect_primary_media_descriptors(child, out)


def _extract_ai_media_items(ai_data):
    """从 AI 候选结构中提取可下载的媒体描述项。"""
    if not isinstance(ai_data, list):
        return []

    candidates = []
    if len(ai_data) > 12 and ai_data[12] is not None:
        _collect_primary_media_descriptors(ai_data[12], candidates)
    if not candidates:
        _collect_media_descriptors(ai_data, candidates)

    deduped = []
    seen = set()
    for item in candidates:
        parsed = _parse_media_item(item, "assistant")
        url = parsed.get("url")
        if not url or _is_internal_placeholder_content_url(url):
            continue
        key = (url, parsed.get("filename"), parsed.get("mime"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped


# ============================================================================
# Turn / media 解析
# ============================================================================
def parse_turn(turn):
    """解析单个对话轮次，返回结构化数据"""
    result = {
        "turn_id": None,
        "timestamp": None,
        "timestamp_iso": None,
        "user": {"text": "", "files": []},
        "assistant": {"text": "", "thinking": "", "model": "", "files": [], "music_meta": None, "gen_meta": None},
    }

    try:
        ids = turn[0]
        result["turn_id"] = ids[1] if len(ids) > 1 else ids[0]

        if len(turn) > 4 and isinstance(turn[4], list) and turn[4]:
            if isinstance(turn[4][0], int):
                result["timestamp"] = turn[4][0]
                result["timestamp_iso"] = _to_iso_utc(turn[4][0])

        content = turn[2]
        msg = content[0]
        if isinstance(msg[0], str):
            result["user"]["text"] = msg[0]

        if (len(msg) > 4 and msg[4] is not None
                and isinstance(msg[4], list) and len(msg[4]) > 0
                and isinstance(msg[4][0], list) and len(msg[4][0]) > 3
                and msg[4][0][3] is not None):
            user_files = msg[4][0][3]
            for f in user_files:
                if isinstance(f, list):
                    result["user"]["files"].append(_parse_media_item(f, "user"))

        detail = turn[3]

        if len(detail) > 21 and isinstance(detail[21], str):
            result["assistant"]["model"] = detail[21]

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

            if (len(ai_data) > 1 and isinstance(ai_data[1], list)
                    and len(ai_data[1]) > 0 and isinstance(ai_data[1][0], str)):
                result["assistant"]["text"] = ai_data[1][0]

            if (len(ai_data) > 37 and ai_data[37] is not None
                    and isinstance(ai_data[37], list) and len(ai_data[37]) > 0):
                thinking = ai_data[37]
                if isinstance(thinking[0], list) and len(thinking[0]) > 0:
                    if isinstance(thinking[0][0], str):
                        result["assistant"]["thinking"] = thinking[0][0]
                elif isinstance(thinking[0], str):
                    result["assistant"]["thinking"] = thinking[0]
            ai_media_items = _extract_ai_media_items(ai_data)

        seen_ai = set()
        for f in ai_media_items:
            parsed = _parse_media_item(f, "assistant")
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

        # AI 生成的音乐/视频（from ai_data[12] 深层结构）
        if isinstance(ai_data, list):
            gen_files, music_meta, gen_meta = _extract_generated_media(ai_data)
            if gen_files:
                # 去重后合并
                existing_urls = {f.get("url") for f in result["assistant"]["files"]}
                for gf in gen_files:
                    if gf.get("url") not in existing_urls:
                        result["assistant"]["files"].append(gf)
                        existing_urls.add(gf.get("url"))
            if music_meta:
                result["assistant"]["music_meta"] = music_meta
            if gen_meta:
                result["assistant"]["gen_meta"] = gen_meta

    except (IndexError, TypeError):
        pass

    return result


def _parse_media_item(item, role):
    """解析单个媒体项目"""
    media = {
        "role": role,
        "type": "unknown",
        "filename": None,
        "mime": None,
        "url": None,
        "thumbnail_url": None,
        "duration": None,
        "resolution": None,
    }

    try:
        type_val = item[1] if len(item) > 1 else None
        media["filename"] = item[2] if len(item) > 2 and isinstance(item[2], str) else None
        media["mime"] = item[11] if len(item) > 11 and isinstance(item[11], str) else None

        if type_val == 1:
            media["type"] = "image"
            if len(item) > 3 and isinstance(item[3], str):
                media["url"] = item[3]
        elif type_val == 2:
            media["type"] = "video"
            if len(item) > 7 and isinstance(item[7], list):
                urls = item[7]
                if len(urls) > 1 and isinstance(urls[1], str):
                    media["url"] = urls[1]
                elif len(urls) > 0 and isinstance(urls[0], str):
                    media["url"] = urls[0]
                if len(urls) > 0 and isinstance(urls[0], str):
                    media["thumbnail_url"] = urls[0]
            if not media["url"] and len(item) > 3 and isinstance(item[3], str):
                media["url"] = item[3]
        elif type_val == 4:
            media["type"] = "audio"
            if len(item) > 7 and isinstance(item[7], list):
                urls = item[7]
                if len(urls) > 1 and isinstance(urls[1], str):
                    media["url"] = urls[1]
                elif urls and isinstance(urls[0], str):
                    media["url"] = urls[0]
                if urls and isinstance(urls[0], str):
                    media["thumbnail_url"] = urls[0]
            if not media["url"] and len(item) > 3 and isinstance(item[3], str):
                media["url"] = item[3]
        else:
            if len(item) > 3 and isinstance(item[3], str):
                media["url"] = item[3]

        # 时长: item[14] 如 [[30, 772244000]] → 30.77 秒
        if len(item) > 14 and isinstance(item[14], list) and item[14]:
            dur = item[14][0] if isinstance(item[14][0], list) else item[14]
            if isinstance(dur, list) and len(dur) >= 1 and isinstance(dur[0], int):
                secs = dur[0]
                nanos = dur[1] if len(dur) > 1 and isinstance(dur[1], int) else 0
                media["duration"] = round(secs + nanos / 1e9, 2)

        # 分辨率: item[17] 如 [[8], 1280, 720]
        if len(item) > 17 and isinstance(item[17], list) and len(item[17]) >= 3:
            w, h = item[17][1], item[17][2]
            if isinstance(w, int) and isinstance(h, int):
                media["resolution"] = {"width": w, "height": h}

    except (IndexError, TypeError):
        pass

    return media


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
                key = _media_identity_key(f)
                if key in turn_seen:
                    continue
                turn_seen.add(key)

                if key in seen[role]:
                    continue

                seen[role].add(key)
                deduped_in_turn.append(f)

            role_obj["files"] = deduped_in_turn

    return parsed_turns
