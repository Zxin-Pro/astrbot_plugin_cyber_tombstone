"""astrbot_plugin_cyber_tombstone - 数据聚合 + 悼词生成（LLM / 本地模板降级）"""

import asyncio
import re
import time
from typing import Optional, Tuple

EPITAPH_PROMPT = """你是一位庄重又幽默的追悼会主持人。请为以下群友写一段 100 字以内的悼词：
- 昵称：{user_name}
- 群龄：{days_in_group} 天
- 最后发言时间：{last_seen_str}
- 最后一条消息：「{last_message}」
- 历史发言数：{message_count}

要求：
1. 语气庄重，但要有幽默感，让人会心一笑
2. 引用最后一条消息作为"遗言"
3. 结尾用"愿天堂没有已读不回"或类似金句
4. 不要出现"死亡""去世"等直白词汇，用"沉默""隐退""归于潜水"替代
只输出悼词正文，不要任何前后缀说明。"""


class FetchError(Exception):
    pass


# ---------------- 历史消息解析（初始化回溯用） ----------------

CQ_RE = re.compile(r"\[CQ:([a-zA-Z]+)[^\]]*\]")
CQ_PLACEHOLDERS = {
    "image": "[图片]", "record": "[语音]", "video": "[视频]",
    "face": "[表情]", "forward": "[合并转发]", "json": "[卡片]", "xml": "[卡片]",
}


def strip_cq(text: str) -> str:
    """把 CQ 码替换为可读占位符（at 丢弃，其余映射，未知丢弃）。"""
    return CQ_RE.sub(lambda m: CQ_PLACEHOLDERS.get(m.group(1), ""), text or "")


def _text_from_segments(message) -> str:
    """message 段数组兜底提取纯文本。"""
    if not isinstance(message, list):
        return ""
    parts = []
    for seg in message:
        if isinstance(seg, dict) and seg.get("type") == "text":
            parts.append((seg.get("data") or {}).get("text") or "")
    return "".join(parts)


def parse_history_msg(m: dict) -> Optional[Tuple[str, str, str, int]]:
    """解析 get_group_msg_history 的单条消息 → (user_id, name, content, ts)。

    无效消息返回 None。
    """
    sender = m.get("sender") or {}
    uid = str(sender.get("user_id") or m.get("user_id") or "").strip()
    if not uid:
        return None
    name = str(sender.get("card") or sender.get("nickname") or uid).strip()
    content = str(m.get("raw_message") or "") or _text_from_segments(m.get("message"))
    content = strip_cq(content).strip()[:1500]
    try:
        ts = int(m.get("time") or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return uid, name or uid, content, ts


# ---------------- 时间 / 天数格式化 ----------------


def fmt_ts(ts: Optional[int]) -> str:
    """统一时间格式 %Y-%m-%d %H:%M"""
    if not ts:
        return "未知"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return "未知"


def fmt_days(seconds: int) -> str:
    """秒 → 'X 年 X 天' / 'X 天'"""
    days = max(int(seconds // 86400), 0)
    if days <= 0:
        return "不足 1 天"
    years, days = divmod(days, 365)
    if years > 0:
        return f"{years} 年 {days} 天"
    return f"{days} 天"


async def get_profile(db, group_id: str, user_id: str) -> Optional[dict]:
    """聚合单个用户画像。查无记录返回 None。"""
    if db is None:
        return None
    row = await db.get_profile(group_id, user_id)
    if not row:
        return None
    now = int(time.time())
    profile = dict(row)
    profile["days_in_group"] = max((now - (row.get("first_seen") or now)) // 86400, 0)
    profile["days_text"] = fmt_days(now - (row.get("first_seen") or now))
    profile["inactive_text"] = fmt_days(now - (row.get("last_seen") or now))
    profile["last_seen_str"] = fmt_ts(row.get("last_seen"))
    if not (row.get("last_message") or "").strip():
        profile["last_message"] = "（最后的言语已随风而逝）"
    return profile


# ---------------- 悼词生成 ----------------


def fallback_epitaph(profile: dict) -> str:
    """LLM 失败时的本地模板。"""
    return (
        f"{profile.get('user_name', '佚名')} 同志，于 {profile.get('last_seen_str', '未知')} "
        f"停止发言，享年 {profile.get('days_text', '未知')}群龄，"
        f"生前最后一条消息是「{profile.get('last_message', '……')}」。\n"
        f"愿天堂没有已读不回。"
    )


def _extract_llm_text(resp) -> str:
    """兼容多版本 text_chat 返回：LLMResponse / 元组 / 字符串。"""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    text = getattr(resp, "completion_text", None)
    if text:
        return text
    # 老版本返回 (message_chain, ...) 或 (text, ...)
    if isinstance(resp, (tuple, list)):
        for item in resp:
            if isinstance(item, str) and item.strip():
                return item
            t = getattr(item, "completion_text", None)
            if t:
                return t
            t = getattr(item, "plain", None)
            if t:
                return t
    return str(resp) if not isinstance(resp, (dict,)) else ""


def _clean_epitaph(text: str) -> str:
    text = (text or "").strip()
    # 去掉包裹引号与常见前缀
    for prefix in ("悼词：", "悼词:", "好的，以下是悼词：", "以下是悼词："):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if len(text) >= 2 and text[0] in "\"“「『" and text[-1] in "\"”」』":
        text = text[1:-1].strip()
    return text[:300]


async def generate_epitaph(provider, profile: dict, timeout: int = 30) -> Tuple[str, str]:
    """生成悼词，返回 (悼词, 来源)。来源：llm / local。

    provider 为 None 或调用超时/失败时降级本地模板。
    """
    if provider is None:
        return fallback_epitaph(profile), "local"
    prompt = EPITAPH_PROMPT.format(
        user_name=profile.get("user_name", "佚名"),
        days_in_group=profile.get("days_in_group", 0),
        last_seen_str=profile.get("last_seen_str", "未知"),
        last_message=profile.get("last_message", "……"),
        message_count=profile.get("message_count", 0),
    )
    try:
        resp = await asyncio.wait_for(
            provider.text_chat(prompt=prompt, session_id=None), timeout=timeout
        )
        text = _clean_epitaph(_extract_llm_text(resp))
        if text:
            return text, "llm"
    except asyncio.TimeoutError:
        pass
    except Exception:
        pass
    return fallback_epitaph(profile), "local"
