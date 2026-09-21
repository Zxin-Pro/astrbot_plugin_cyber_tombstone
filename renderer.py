"""astrbot_plugin_cyber_tombstone - Pillow 墓碑图片渲染

深色墓园风，画布宽 1200px：
  顶部 R.I.P 金色衬线大字 + 「安息」
  中部拱形墓碑：群昵称 / 群龄·最后发言 / 遗言
  底部悼词全文（自动换行，超 200 字截断）
  角落 "AstrBot 赛博墓园 · 第 N 号"
渲染失败由上层降级为 Markdown 文本。
"""

import glob
import os
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# 配色
BG_COLOR = (10, 10, 14)
GOLD = (212, 175, 55)
GOLD_DIM = (160, 130, 45)
STONE_FILL = (46, 46, 52)
STONE_EDGE = (110, 110, 120)
STONE_INNER = (66, 66, 74)
WHITE = (240, 240, 240)
GREY = (184, 184, 196)
QUOTE_COLOR = (232, 228, 200)
EPITAPH_COLOR = (216, 216, 216)
DIM = (120, 120, 132)

WIDTH = 1200
MAX_EPITAPH_LEN = 200

_font_cache: dict = {}


def _search_font_paths() -> List[str]:
    paths = []
    # 1) 插件自带 fonts/
    plugin_fonts = os.path.join(PLUGIN_DIR, "fonts")
    if os.path.isdir(plugin_fonts):
        for pat in ("*.ttf", "*.otf", "*.ttc"):
            paths.extend(sorted(glob.glob(os.path.join(plugin_fonts, pat))))
    # 2) 系统常见目录 CJK 关键字
    for root in ("/usr/share/fonts", "/usr/local/share/fonts", "/system/fonts"):
        for pat in (
            f"{root}/**/*CJK*.tt*", f"{root}/**/*cjk*.tt*",
            f"{root}/**/*wqy*.tt*", f"{root}/**/*noto*sans*sc*.tt*",
            f"{root}/**/*msyh*",
        ):
            paths.extend(sorted(glob.glob(pat, recursive=True)))
    # 3) DejaVu 兜底
    for pat in ("/usr/share/fonts/**/DejaVuSans*.ttf",):
        paths.extend(sorted(glob.glob(pat, recursive=True)))
    seen, out = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _load_font(size: int, serif: bool = False):
    key = ("serif" if serif else "cjk", size)
    if key in _font_cache:
        return _font_cache[key]
    font = None
    candidates = []
    if serif:
        # 衬线：优先 Noto Serif / DejaVu Serif（R.I.P 用）
        for root in ("/usr/share/fonts",):
            candidates.extend(glob.glob(f"{root}/**/*Serif*.tt*", recursive=True))
            candidates.extend(glob.glob(f"{root}/**/DejaVuSerif*.ttf", recursive=True))
        candidates = [p for p in candidates if "CJK" in p or "Serif" in p]
    else:
        candidates = _search_font_paths()
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size)
        except Exception:
            font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _text_len(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        return int(draw.textlength(text, font=font))
    except Exception:
        return len(text) * (font.size or 20)


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> List[str]:
    """逐字符换行（CJK 友好），兼容英文单词整体换行。"""
    lines: List[str] = []
    for para in (text or "").split("\n"):
        if not para:
            lines.append("")
            continue
        buf = ""
        for ch in para:
            trial = buf + ch
            if _text_len(draw, trial, font) > max_width and buf:
                lines.append(buf)
                buf = ch
            else:
                buf = trial
        if buf:
            lines.append(buf)
    return lines


def _truncate(text: str, limit: int = MAX_EPITAPH_LEN) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        return text[:limit] + "……"
    return text


def _draw_center(draw, y, text, font, fill):
    w = _text_len(draw, text, font)
    draw.text(((WIDTH - w) // 2, y), text, font=font, fill=fill)
    return y + (font.size or 20)


def render_tombstone(data: dict) -> bytes:
    """渲染墓碑图，返回 PNG bytes。

    data: {tomb_no, user_name, days_text, last_seen_str, last_message,
           epitaph, inactive_text}
    """
    user_name = (data.get("user_name") or "佚名")[:20]
    days_text = data.get("days_text") or "未知"
    last_seen_str = data.get("last_seen_str") or "未知"
    last_message = (data.get("last_message") or "……").strip()[:80]
    epitaph = _truncate(data.get("epitaph") or "")
    tomb_no = data.get("tomb_no", "?")

    probe = Image.new("RGB", (WIDTH, 100))
    pd = ImageDraw.Draw(probe)

    f_rip = _load_font(132, serif=True)
    f_rest = _load_font(46)
    f_name = _load_font(68)
    f_meta = _load_font(38)
    f_label = _load_font(32)
    f_quote = _load_font(40)
    f_epi = _load_font(38)
    f_foot = _load_font(28)

    # ---- 预排版计算高度 ----
    name_w = _text_len(pd, user_name, f_name)
    stone_w = max(min(max(name_w + 160, 760), 900), 0)
    stone_x0 = (WIDTH - stone_w) // 2
    stone_inner_w = stone_w - 120

    meta_text = f"群龄 {days_text} · 最后发言 {last_seen_str}"
    quote_lines = wrap_text(pd, f"「{last_message}」", f_quote, stone_inner_w)
    epitaph_lines = wrap_text(pd, epitaph, f_epi, WIDTH - 200)

    # 石碑内部布局
    y = 120  # 墓碑顶部相对偏移
    name_y = y + 60
    meta_y = name_y + 100
    quote_label_y = meta_y + 70
    quote_y = quote_label_y + 50
    quote_end = quote_y + len(quote_lines) * 58
    stone_h = (quote_end + 70) - y

    total_h = (
        90            # 顶部留白
        + 150         # R.I.P
        + 70          # 安息
        + 60          # 间隔
        + stone_h     # 墓碑
        + 70          # 间隔
        + 40          # 悼词标签
        + len(epitaph_lines) * 54 + 30
        + 90          # 底部落款
    )

    img = Image.new("RGB", (WIDTH, total_h), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # ---- 顶部 R.I.P ----
    cy = _draw_center(draw, 60, "R.I.P", f_rip, GOLD)
    cy = _draw_center(draw, cy + 14, "— 安 息 —", f_rest, GREY)

    # ---- 墓碑（拱形圆角矩形）----
    stone_y0 = cy + 50
    stone_y1 = stone_y0 + stone_h
    # 底座
    draw.rounded_rectangle(
        (stone_x0 - 30, stone_y1 - 20, stone_x0 + stone_w + 30, stone_y1 + 26),
        radius=14, fill=(30, 30, 36), outline=STONE_EDGE, width=3,
    )
    # 碑体：上圆下方（radius 大值近似拱形）
    draw.rounded_rectangle(
        (stone_x0, stone_y0, stone_x0 + stone_w, stone_y1),
        radius=stone_w // 3, fill=STONE_FILL, outline=STONE_EDGE, width=5,
    )
    draw.rounded_rectangle(
        (stone_x0 + 18, stone_y0 + 18, stone_x0 + stone_w - 18, stone_y1 - 30),
        radius=stone_w // 3 - 12, outline=STONE_INNER, width=2,
    )
    # 遮住下半部过圆的弧线（把碑体下部补成直角）
    draw.rectangle((stone_x0 + 1, stone_y0 + stone_w // 3,
                    stone_x0 + stone_w - 1, stone_y1 - 1), fill=STONE_FILL)
    draw.line((stone_x0, stone_y0 + stone_w // 3, stone_x0, stone_y1), fill=STONE_EDGE, width=5)
    draw.line((stone_x0 + stone_w, stone_y0 + stone_w // 3, stone_x0 + stone_w, stone_y1),
              fill=STONE_EDGE, width=5)
    draw.line((stone_x0 + 19, stone_y0 + stone_w // 3, stone_x0 + 19, stone_y1 - 31),
              fill=STONE_INNER, width=2)
    draw.line((stone_x0 + stone_w - 19, stone_y0 + stone_w // 3,
               stone_x0 + stone_w - 19, stone_y1 - 31), fill=STONE_INNER, width=2)

    # 碑文
    sy = stone_y0 + name_y - y  # 换算到绝对坐标
    sy = _draw_center(draw, sy, user_name, f_name, WHITE)
    sy = _draw_center(draw, sy + 26, meta_text, f_meta, GREY)
    sy = _draw_center(draw, sy + 18, "· 遗 言 ·", f_label, GOLD_DIM)
    for line in quote_lines:
        sy = _draw_center(draw, sy + 8, line, f_quote, QUOTE_COLOR)

    # ---- 悼词 ----
    e_y = stone_y1 + 80
    e_y = _draw_center(draw, e_y, "—— 悼 词 ——", f_label, GOLD_DIM)
    for line in epitaph_lines:
        e_y = _draw_center(draw, e_y + 16, line, f_epi, EPITAPH_COLOR)

    # ---- 底部落款 ----
    foot = f"AstrBot 赛博墓园 · 第 {tomb_no} 号"
    fw = _text_len(draw, foot, f_foot)
    draw.text((WIDTH - fw - 40, total_h - 70), foot, font=f_foot, fill=DIM)

    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def fallback_text(data: dict) -> str:
    """渲染失败 / 关闭图片时的 Markdown 降级文本。"""
    epitaph = _truncate(data.get("epitaph") or "")
    return (
        f"🪦 赛博墓碑 · 第 {data.get('tomb_no', '?')} 号\n\n"
        f"「{data.get('user_name', '佚名')}」\n"
        f"群龄 {data.get('days_text', '未知')} · "
        f"最后发言 {data.get('last_seen_str', '未知')}\n"
        f"遗言：「{(data.get('last_message') or '……').strip()[:80]}」\n\n"
        f"悼词：\n{epitaph}"
    )
