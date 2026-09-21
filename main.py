"""astrbot_plugin_cyber_tombstone - 赛博墓碑

自动检测群内长期潜水的群友，生成庄重又搞笑的"赛博墓碑"图片与悼词。
指令统一入口 /tomb：help|scan|bury|list|forget|forget_all|revive|config|debug
"""

import asyncio
import base64
import re
import time
import traceback
from typing import List, Optional

import astrbot

try:  # 插件元信息（老版本无此模块，忽略即可）
    from astrbot.api import logger, AstrMessageEvent
    from astrbot.api.star import Context, Star, register
except ImportError:  # pragma: no cover
    from astrbot.api.event import AstrMessageEvent
    from astrbot.core import logger
    from astrbot.core.star import Context, Star, register

try:
    from astrbot.api import MessageChain
except ImportError:
    try:
        from astrbot.core.message.message_event_result import MessageChain
    except ImportError:
        MessageChain = None

try:
    from astrbot.api.event import filter
except ImportError:
    from astrbot.api import filter

try:
    EventMessageType = filter.EventMessageType
except AttributeError:
    try:
        from astrbot.api.event.filter import EventMessageType
    except ImportError:
        from astrbot.core.star.register import EventMessageType

try:
    from astrbot.api.message_components import At, Plain, Image
except ImportError:
    from astrbot.core.message.components import At, Plain, Image

from .database import TombDatabase
from .fetcher import fmt_days, fmt_ts, generate_epitaph, get_profile
from .renderer import fallback_text, render_tombstone

PLUGIN_NAME = "astrbot_plugin_cyber_tombstone"
PLUGIN_VERSION = "v1.0.1"

FLUSH_INTERVAL = 5          # 内存缓冲 flush 周期（秒）
FLUSH_BATCH = 100           # 缓冲达到该条数立即 flush
SCAN_POLL_INTERVAL = 30     # 定时扫描轮询周期（秒）
LLM_TIMEOUT = 30            # LLM 悼词超时（秒）
AUTO_BURY_LIMIT = 10        # 自动扫描单群最多立碑人数
AUTO_BURY_FLOOD = 20        # 潜水人数超过该值时只取最早发言的 10 人

# 中文子指令别名 → 内部英文（英文原名保持兼容）
SUB_ALIASES = {
    "帮助": "help", "说明": "help",
    "配置": "config",
    "诊断": "debug",
    "潜水名单": "scan", "扫描": "scan", "潜水": "scan",
    "墓碑列表": "list", "碑录": "list", "列表": "list",
    "立碑": "bury", "安葬": "bury", "埋": "bury",
    "遗忘": "forget", "抹去": "forget",
    "全部遗忘": "forget_all", "清空": "forget_all",
    "复活": "revive",
}


@register(
    "astrbot_plugin_cyber_tombstone",
    "Zxin_Pro",
    "赛博墓碑：检测群内长期潜水的群友，生成庄重又搞笑的墓碑图与悼词",
    "1.0.0",
)
class CyberTombstone(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.db: Optional[TombDatabase] = None
        self._buffer: List[tuple] = []          # 消息内存缓冲
        self._flush_task = None
        self._scan_task = None
        self._scan_date = ""                    # 自动扫描日期防重
        self._last_cleanup_date = ""            # messages 清理日期防重

    # ---------------- 配置 ----------------

    def _cfg(self, key: str, default=None):
        if key in self.config:
            return self.config[key]
        try:
            global_cfg = self.context.get_config()
            if hasattr(global_cfg, "get"):
                val = global_cfg.get(key, default)
                if val is not None:
                    return val
        except Exception:
            pass
        return default

    @property
    def inactive_days(self) -> int:
        try:
            return max(int(self._cfg("inactive_days", 30)), 1)
        except Exception:
            return 30

    @property
    def max_scan_results(self) -> int:
        try:
            return max(int(self._cfg("max_scan_results", 10)), 1)
        except Exception:
            return 10

    @property
    def enable_image_render(self) -> bool:
        return bool(self._cfg("enable_image_render", True))

    # ---------------- 生命周期 ----------------

    async def initialize(self):
        import os
        data_dir = os.path.join("data", "plugin_data", PLUGIN_NAME)
        os.makedirs(data_dir, exist_ok=True)
        self.db = TombDatabase(os.path.join(data_dir, "tombstone.db"))
        await self.db.init()
        self._flush_task = asyncio.create_task(self._flush_loop())
        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info(f"[cyber_tombstone] loaded {PLUGIN_VERSION}")

    async def terminate(self):
        for task in (self._flush_task, self._scan_task):
            if task:
                task.cancel()
        try:
            await self._flush_buffer()
        except Exception as e:
            logger.error(f"[cyber_tombstone] terminate flush 失败: {e}")
        if self.db:
            await self.db.close()
        logger.info("[cyber_tombstone] terminated")

    # ---------------- 消息监听 ----------------

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """记录群消息（仅群聊），先入内存缓冲再批量落库。"""
        try:
            if not bool(self._cfg("enable_record", True)):
                return
            group_id = str(event.get_group_id() or "").strip()
            if not group_id or group_id == "None":
                return
            user_id = str(event.get_sender_id() or "").strip()
            if not user_id:
                return
            user_name = ""
            try:
                user_name = (event.get_sender_name() or "").strip()
            except Exception:
                pass
            content = (event.message_str or "").strip()
            if content == "/":
                content = ""
            self._buffer.append(
                (group_id, user_id, user_name or user_id, content[:1500], int(time.time()))
            )
            if len(self._buffer) >= FLUSH_BATCH:
                await self._flush_buffer()
        except Exception as e:
            logger.error(f"[cyber_tombstone] 记录消息失败: {e}")

    async def _flush_buffer(self):
        if not self._buffer or self.db is None:
            return
        rows, self._buffer = self._buffer, []
        agg = {}
        for group_id, user_id, user_name, content, ts in rows:
            key = (group_id, user_id)
            item = agg.setdefault(
                key,
                {"user_name": user_name, "first_seen": ts, "last_seen": ts,
                 "count": 0, "last_message": content},
            )
            item["first_seen"] = min(item["first_seen"], ts)
            item["last_seen"] = max(item["last_seen"], ts)
            item["count"] += 1
            if ts >= item["last_seen"]:
                item["last_message"] = content
                if user_name:
                    item["user_name"] = user_name
        await self.db.insert_messages(rows)
        await self.db.upsert_users(agg)

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            try:
                await self._flush_buffer()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[cyber_tombstone] flush 失败: {e}\n{traceback.format_exc()}")

    # ---------------- 指令入口 ----------------

    @filter.command("tomb", alias=["墓碑", "赛博墓碑"])
    async def tomb_command(self, event: AstrMessageEvent):
        tokens = [t for t in (event.message_str or "").strip().split() if t]
        if tokens and tokens[0].lstrip("/").lower() in ("tomb", "墓碑", "赛博墓碑"):
            tokens = tokens[1:]
        sub = tokens[0].lstrip("/").lower() if tokens else ""
        sub = SUB_ALIASES.get(sub, sub)

        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            yield event.plain_result("🪦 该指令仅支持在群聊中使用。")
            return

        try:
            if sub in ("", "help", "帮助"):
                # 裸 /tomb @某人 → 预览墓碑（不立碑）
                target = self._extract_target(event, tokens) if sub == "" else None
                if target:
                    async for r in self._make_tombstone(event, group_id, target,
                                                        bury=False):
                        yield r
                else:
                    yield event.plain_result(self._help_text())
            elif sub == "config":
                yield event.plain_result(self._config_text())
            elif sub == "debug":
                yield event.plain_result(await self._debug_text())
            elif sub == "scan":
                async for r in self._cmd_scan(event, group_id):
                    yield r
            elif sub == "list":
                async for r in self._cmd_list(event, group_id):
                    yield r
            elif sub == "bury":
                async for r in self._cmd_bury(event, group_id):
                    yield r
            elif sub in ("forget", "forget_all", "revive"):
                async for r in self._cmd_privacy(event, group_id, sub):
                    yield r
            else:
                # 裸 /tomb @某人 → 预览墓碑（不标记）
                target = self._extract_target(event, tokens)
                if target:
                    async for r in self._make_tombstone(event, group_id, target,
                                                        bury=False):
                        yield r
                else:
                    yield event.plain_result(self._help_text())
        except Exception as e:
            logger.error(f"[cyber_tombstone] 指令异常: {e}\n{traceback.format_exc()}")
            yield event.plain_result(f"🪦 墓园闹鬼了（{type(e).__name__}），稍后再试。")

    # ---------------- 子指令实现 ----------------

    def _help_text(self) -> str:
        return (
            "🪦 赛博墓园 · 指令一览\n"
            "────────────────\n"
            "/墓碑 @某人 —— 预览 TA 的墓碑（不立碑）\n"
            "/墓碑 立碑 @某人 —— 正式立碑（记录在案）\n"
            "/墓碑 潜水名单 —— 扫描本群潜水名单\n"
            "/墓碑 墓碑列表 —— 查看本群所有墓碑\n"
            "/墓碑 复活 @某人 —— 复活（移出墓碑名单）\n"
            "/墓碑 遗忘 @某人 —— 删除 TA 的全部记录（隐私）\n"
            "/墓碑 清空 —— 清空本群全部记录（仅管理员）\n"
            "/墓碑 配置 —— 查看当前配置\n"
            "/墓碑 诊断 —— 墓园运行诊断\n"
            "────────────────\n"
            "愿天堂没有已读不回。"
        )

    def _config_text(self) -> str:
        lines = ["🪦 赛博墓园 · 当前配置"]
        for key in ("inactive_days", "auto_scan_enabled", "auto_scan_time",
                    "push_target", "max_scan_results", "enable_image_render",
                    "enable_record", "message_retention_days"):
            lines.append(f"· {key}: {self._cfg(key, '(默认)')}")
        return "\n".join(lines)

    async def _debug_text(self) -> str:
        stats = await self.db.stats() if self.db else {}
        buf_n = len(self._buffer)
        prov = self._get_provider()
        return (
            "🪦 赛博墓园 · 诊断\n"
            f"· 版本: {PLUGIN_VERSION}\n"
            f"· 数据库: {'OK' if self.db and self.db.db else '未连接'} "
            f"(messages={stats.get('messages')}, users={stats.get('users')}, "
            f"tombs={stats.get('tombs')})\n"
            f"· 内存缓冲: {buf_n} 条\n"
            f"· LLM Provider: {'可用' if prov else '不可用（悼词走本地模板）'}\n"
            f"· 图片渲染: {'开' if self.enable_image_render else '关'}\n"
            f"· 潜水阈值: {self.inactive_days} 天"
        )

    async def _cmd_scan(self, event: AstrMessageEvent, group_id: str):
        cutoff = int(time.time()) - self.inactive_days * 86400
        total = await self.db.count_inactive(group_id, cutoff)
        if total == 0:
            yield event.plain_result(
                f"🪦 本群暂无潜水超过 {self.inactive_days} 天的群友，人人健在（至少手是）。"
            )
            return
        # 刷屏保护：超过 20 人只取最后发言最早的 10 人
        if total > AUTO_BURY_FLOOD:
            rows = await self.db.get_inactive(group_id, cutoff, AUTO_BURY_LIMIT)
            head = f"共发现 {total} 位潜水群友，仅列出沉默最久的 {len(rows)} 位："
        else:
            rows = await self.db.get_inactive(group_id, cutoff, self.max_scan_results)
            head = f"共发现 {total} 位潜水群友（阈值 {self.inactive_days} 天）："
        lines = [f"🪦 潜水名单 · {head}", "────────────────"]
        import time as _t
        for i, u in enumerate(rows, 1):
            silence = _t.time() - (u.get("last_seen") or 0)
            lines.append(
                f"{i}. {u.get('user_name') or u['user_id']} · "
                f"沉默 {fmt_days(int(silence))} · "
                f"最后发言 {fmt_ts(u.get('last_seen'))}"
            )
        lines.append("────────────────")
        lines.append("用 /墓碑 立碑 @某人 为其正式立碑。")
        yield event.plain_result("\n".join(lines))

    async def _cmd_list(self, event: AstrMessageEvent, group_id: str):
        tombs = await self.db.list_tombs(group_id)
        if not tombs:
            yield event.plain_result("🪦 本群还没有立过墓碑。用 /墓碑 立碑 @某人 送 TA 一程。")
            return
        lines = [f"🪦 本群墓碑共 {len(tombs)} 座：", "────────────────"]
        for t in tombs[:20]:
            lines.append(
                f"第 {t.get('tomb_no')} 号 · {t.get('user_name') or t['user_id']} · "
                f"立碑于 {fmt_ts(t.get('bury_time'))}"
            )
        if len(tombs) > 20:
            lines.append(f"…（其余 {len(tombs) - 20} 座略）")
        yield event.plain_result("\n".join(lines))

    async def _cmd_bury(self, event: AstrMessageEvent, group_id: str):
        target = self._extract_target(event, [])
        if not target:
            yield event.plain_result("用法：/墓碑 立碑 @某人")
            return
        async for r in self._make_tombstone(event, group_id, target, bury=True):
            yield r

    async def _cmd_privacy(self, event: AstrMessageEvent, group_id: str, sub: str):
        if sub == "forget_all":
            if not self._is_admin(event):
                yield event.plain_result("⛔ 该指令仅管理员可用。")
                return
            n = await self.db.forget_all(group_id)
            yield event.plain_result(
                f"🪦 已将本群 {n} 条记录迁入无名冢（全部清除）。生者当自省。"
            )
            return
        target = self._extract_target(event, [])
        if not target:
            yield event.plain_result(f"用法：/墓碑 {'遗忘' if sub == 'forget' else '复活'} @某人")
            return
        user_id, user_name = target
        if sub == "forget":
            n = await self.db.forget(group_id, user_id)
            if n:
                yield event.plain_result(
                    f"🪦 已抹去 {user_name} 在本群的一切痕迹（{n} 条记录）。"
                    "TA 获得了原谅，也获得了遗忘。"
                )
            else:
                yield event.plain_result(f"🪦 查无 {user_name} 的记录。")
        else:  # revive
            ok = await self.db.remove_tomb(group_id, user_id)
            if ok:
                yield event.plain_result(
                    f"✨ {user_name} 从墓碑名单中复活了！快出来冒个泡证明你还活着。"
                )
            else:
                yield event.plain_result(f"🪦 {user_name} 不在墓碑名单中，无需复活。")

    # ---------------- 立碑核心流程 ----------------

    async def _make_tombstone(self, event: AstrMessageEvent, group_id: str,
                              target: tuple, bury: bool):
        user_id, at_name = target
        profile = await get_profile(self.db, group_id, user_id)
        if not profile:
            name = at_name or user_id
            yield event.plain_result(
                f"🪦 查无「{name}」的发言记录：TA 要么从未开口，要么已被遗忘清除。"
            )
            return

        user_name = profile.get("user_name") or at_name or user_id
        profile["user_name"] = user_name

        if bury and await self.db.has_tomb(group_id, user_id):
            yield event.plain_result(
                f"🪦 {user_name} 已有墓碑在册，不可重复安葬（可用 /墓碑 复活 移出名单）。"
            )
            return

        provider = self._get_provider()
        epitaph, source = await generate_epitaph(provider, profile, LLM_TIMEOUT)

        if bury:
            tomb_no = await self.db.add_tomb(group_id, user_id, profile, epitaph)
        else:
            tomb_no = await self.db.next_tomb_no(group_id)

        data = {
            "tomb_no": tomb_no,
            "user_name": user_name,
            "days_text": profile.get("days_text", "未知"),
            "last_seen_str": profile.get("last_seen_str", "未知"),
            "last_message": profile.get("last_message", "……"),
            "epitaph": epitaph,
        }

        if self.enable_image_render:
            try:
                png = await asyncio.to_thread(render_tombstone, data)
                b64 = base64.b64encode(png).decode()
                note = f"（悼词来源：{'LLM' if source == 'llm' else '本地模板'}）"
                yield event.chain_result([
                    Image.fromBase64(b64),
                    Plain(f"🪦 第 {tomb_no} 号墓碑已立。{note}"),
                ])
                return
            except Exception as e:
                logger.error(f"[cyber_tombstone] 渲染失败: {e}\n{traceback.format_exc()}")
        yield event.plain_result(fallback_text(data))

    # ---------------- 自动扫描 ----------------

    async def _scan_loop(self):
        await asyncio.sleep(SCAN_POLL_INTERVAL)
        while True:
            try:
                if bool(self._cfg("auto_scan_enabled", True)):
                    scan_time = str(self._cfg("auto_scan_time", "20:00")).strip()
                    now = time.localtime()
                    now_hm = time.strftime("%H:%M", now)
                    today = time.strftime("%Y-%m-%d", now)
                    if now_hm == scan_time and self._scan_date != today:
                        self._scan_date = today
                        await self._auto_scan_all()
                    # 每日一次旧消息清理
                    if self._last_cleanup_date != today:
                        self._last_cleanup_date = today
                        retention = self._cfg("message_retention_days", 90)
                        try:
                            n = await self.db.cleanup_messages(int(retention))
                            if n:
                                logger.info(f"[cyber_tombstone] 清理 {n} 条过期消息")
                        except Exception as e:
                            logger.error(f"[cyber_tombstone] 消息清理失败: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[cyber_tombstone] 扫描循环异常: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(SCAN_POLL_INTERVAL)

    async def _auto_scan_all(self):
        """扫描所有已记录群，为潜水用户立碑并推送。"""
        logger.info("[cyber_tombstone] 自动扫描开始")
        groups = await self.db.distinct_groups()
        cutoff = int(time.time()) - self.inactive_days * 86400
        for group_id in groups:
            try:
                total = await self.db.count_inactive(group_id, cutoff)
                if total == 0:
                    continue
                limit = AUTO_BURY_LIMIT if total > AUTO_BURY_FLOOD else self.max_scan_results
                rows = await self.db.get_inactive(group_id, cutoff, limit)
                buried = 0
                for u in rows:
                    user_id = u["user_id"]
                    if await self.db.has_tomb(group_id, user_id):
                        continue
                    profile = await get_profile(self.db, group_id, user_id)
                    if not profile:
                        continue
                    epitaph, _ = await generate_epitaph(
                        self._get_provider(), profile, LLM_TIMEOUT
                    )
                    tomb_no = await self.db.add_tomb(group_id, user_id, profile, epitaph)
                    buried += 1
                    data = {
                        "tomb_no": tomb_no,
                        "user_name": profile.get("user_name") or user_id,
                        "days_text": profile.get("days_text", "未知"),
                        "last_seen_str": profile.get("last_seen_str", "未知"),
                        "last_message": profile.get("last_message", "……"),
                        "epitaph": epitaph,
                    }
                    await self._push_tombstone(group_id, data)
                    await asyncio.sleep(1)  # 温和限速
                logger.info(
                    f"[cyber_tombstone] 群 {group_id}: 潜水 {total} 人，本次立碑 {buried} 座"
                )
            except Exception as e:
                logger.error(
                    f"[cyber_tombstone] 群 {group_id} 自动扫描失败: {e}\n{traceback.format_exc()}"
                )

    async def _push_tombstone(self, group_id: str, data: dict):
        raw_targets = str(self._cfg("push_target", "") or "").strip()
        if raw_targets:
            targets = [t.strip() for t in raw_targets.split(",") if t.strip()]
        else:
            targets = [group_id]
        if self.enable_image_render:
            try:
                png = await asyncio.to_thread(render_tombstone, data)
                b64 = base64.b64encode(png).decode()
                chain = [Image.fromBase64(b64)]
            except Exception:
                chain = [Plain(fallback_text(data))]
        else:
            chain = [Plain(fallback_text(data))]
        for target in targets:
            umo = self._build_group_umo(target)
            if not umo:
                continue
            try:
                await self.context.send_message(umo, MessageChain(chain=chain))
            except Exception as e:
                logger.error(f"[cyber_tombstone] 推送到 {umo} 失败: {e}")

    def _build_group_umo(self, group_id: str) -> str:
        try:
            insts = self.context.platform_manager.get_insts()
        except Exception:
            insts = []
        for inst in insts:
            try:
                if "aiocqhttp" in str(getattr(inst, "type", "")):
                    return f"{inst.name}:GroupMessage:{group_id}"
            except Exception:
                continue
        if insts:
            return f"{insts[0].name}:GroupMessage:{group_id}"
        return ""

    # ---------------- 工具 ----------------

    def _get_provider(self):
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin_id():
                return True
        except Exception:
            pass
        try:
            admins = self.context.get_config().get("admins_id", []) or []
            return str(event.get_sender_id()) in [str(a) for a in admins]
        except Exception:
            return False

    @staticmethod
    def _extract_target(event: AstrMessageEvent, tokens: List[str]) -> Optional[tuple]:
        """从消息中提取目标：优先 At 组件，兜底解析裸 QQ 号。"""
        # 1) At 组件
        try:
            comps = event.message_obj.message
            for c in comps:
                if isinstance(c, At):
                    qq = str(getattr(c, "qq", "") or "")
                    if qq and qq != "all":
                        name = str(getattr(c, "name", "") or "").strip()
                        return qq, name
        except Exception:
            pass
        # 2) 裸 QQ 号
        for tok in tokens:
            m = re.fullmatch(r"[@＠]?\s*(\d{5,12})", tok)
            if m:
                return m.group(1), m.group(1)
        return None

