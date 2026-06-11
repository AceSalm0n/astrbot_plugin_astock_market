"""
AstrBot A 股大盘数据插件
定时获取 A 股主要指数数据，支持 LLM 智能分析解读

支持指数：上证指数、深证成指、创业板指、科创50、沪深300
"""

import asyncio
import json
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .market_api import (
    MarketOverview,
    fetch_market_overview,
    fetch_single_index,
    format_market_overview_text,
    close_session,
)

# ---------------------------------------------------------------------------
# LLM 分析提示词
# ---------------------------------------------------------------------------

MARKET_ANALYSIS_PROMPT = """你是一位专业的A股市场分析师。请基于以下大盘数据，输出一份简洁的市场分析。

当前时间：{current_time}

{market_data}

请按以下格式输出分析：

📊 **市场概况**
[一句话总结今日市场整体表现]

📈 **板块轮动观察**
[基于各指数表现，分析资金可能的流向和板块轮动特征]

💡 **市场情绪判断**
[判断当前市场情绪：狂热/乐观/中性/谨慎/恐慌]

⚠️ **风险提示**
[列出需要注意的风险因素]

📝 **综合观点**
[一句话总结对当前市场的看法]

注意：请保持分析的专业性和客观性，不构成投资建议。总字数控制在300字以内。"""


# ===========================================================================
# 插件主类
# ===========================================================================

@register(
    "astrbot_plugin_astock_market",
    "AstrBot",
    "A股大盘数据定时推送插件，支持 LLM 智能分析",
    "1.0.0",
    "",
)
class AStockMarketPlugin(Star):
    """A股大盘数据插件"""

    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config: AstrBotConfig = config if config is not None else {}

        # 定时推送任务句柄
        self._push_task: Optional[asyncio.Task] = None
        self._startup_task: Optional[asyncio.Task] = None

        # 群 unified_msg_origin 映射（动态学习）
        self._group_umo_map: dict[str, str] = {}
        self._load_group_mapping()

        # 延迟启动定时推送
        if self.config.get("enable_scheduled_push", False):
            self._startup_task = asyncio.create_task(self._delayed_start_scheduler())

        logger.info("A股大盘插件已加载")

    # ------------------------------------------------------------------
    # 数据持久化
    # ------------------------------------------------------------------

    def _mapping_path(self) -> Path:
        return StarTools.get_data_dir("astrbot_plugin_astock_market") / "group_mapping.json"

    def _load_group_mapping(self):
        path = self._mapping_path()
        if path.exists():
            try:
                self._group_umo_map = json.loads(path.read_text(encoding="utf-8"))
                logger.info(f"已加载 {len(self._group_umo_map)} 个群组映射")
            except Exception as e:
                logger.warning(f"加载群组映射失败: {e}")

    def _save_group_mapping(self):
        try:
            path = self._mapping_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._group_umo_map, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存群组映射失败: {e}")

    def _extract_group_id(self, raw: str) -> str:
        raw = str(raw).strip()
        if raw.isdigit():
            return raw
        if ":" in raw:
            parts = raw.split(":")
            last = parts[-1]
            if "_" in last:
                return last.split("_")[-1]
            return last
        return raw

    def _learn_group_mapping(self, event: AstrMessageEvent):
        """从消息事件中学习群 unified_msg_origin 映射"""
        umo = event.unified_msg_origin
        group_id = self._extract_group_id(umo)
        if group_id and group_id not in self._group_umo_map:
            self._group_umo_map[group_id] = umo
            self._save_group_mapping()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def terminate(self):
        for t in (self._startup_task, self._push_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        await close_session()
        logger.info("A股大盘插件已卸载")

    # ------------------------------------------------------------------
    # 定时推送调度
    # ------------------------------------------------------------------

    def _parse_push_times(self) -> list[time]:
        """Parse scheduled_push_times config into a list of time objects."""
        push_times_str = self.config.get("scheduled_push_times", "09:30,11:30,15:00")
        times: list[time] = []
        for t in push_times_str.split(","):
            t = t.strip()
            try:
                h, m = map(int, t.split(":"))
                times.append(time(h, m))
            except (ValueError, AttributeError):
                logger.warning(f"跳过无效推送时间: {t}")
        return times

    async def _delayed_start_scheduler(self):
        await asyncio.sleep(15)
        if self._push_task and not self._push_task.done():
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
        self._push_task = asyncio.create_task(self._scheduled_push_loop())
        logger.info("A股大盘定时推送任务已启动")

    async def _scheduled_push_loop(self):
        """定时推送主循环，支持多个推送时间点"""
        while True:
            try:
                # 周末跳过，直接推到周一第一个推送时间
                if datetime.now().weekday() >= 5:
                    next_monday = self._next_weekday_morning()
                    wait = (next_monday - datetime.now()).total_seconds()
                    logger.info(f"周末跳过推送，下次推送: {next_monday.strftime('%Y-%m-%d %H:%M')}")
                    await asyncio.sleep(max(wait, 60))
                    continue

                push_groups = self.config.get("scheduled_push_groups", [])

                if not push_groups:
                    logger.warning("未配置推送群组，等待 1 小时后重试")
                    await asyncio.sleep(3600)
                    continue

                times = self._parse_push_times()
                if not times:
                    logger.warning("无有效推送时间，等待 1 小时")
                    await asyncio.sleep(3600)
                    continue

                now = datetime.now()
                now_t = now.time()

                # 找到下一个推送时间点
                next_push = None
                for t in sorted(times, key=lambda x: (x.hour, x.minute)):
                    if t > now_t:
                        next_push = datetime.combine(now.date(), t)
                        break

                # 如果今天的所有时间都已过，推到明天第一个
                if next_push is None:
                    t0 = min(times, key=lambda x: (x.hour, x.minute))
                    next_push = datetime.combine(now.date() + timedelta(days=1), t0)

                wait = (next_push - now).total_seconds()
                logger.info(f"下次大盘推送: {next_push.strftime('%Y-%m-%d %H:%M')}")
                await asyncio.sleep(wait)

                await self._push_to_groups(push_groups)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时推送异常: {e}", exc_info=True)
                await asyncio.sleep(300)

    def _next_weekday_morning(self) -> datetime:
        """Get the first configured push time on the next Monday."""
        times = self._parse_push_times()
        first = min(times, key=lambda x: (x.hour, x.minute)) if times else time(9, 30)

        now = datetime.now()
        # Saturday -> +2 days, Sunday -> +1 day
        days_to_monday = (7 - now.weekday()) % 7 or 7
        return datetime.combine(now.date() + timedelta(days=days_to_monday), first)

    async def _push_to_groups(self, group_list: list[str]):
        """向配置的群组推送大盘数据"""
        try:
            overview = await fetch_market_overview()
            # 根据配置决定是否使用 LLM 分析
            if self.config.get("enable_llm_analysis", False):
                text = await self._generate_llm_analysis(overview)
            else:
                text = format_market_overview_text(overview)

            success = 0
            for group_id in group_list:
                try:
                    clean_id = self._extract_group_id(group_id)
                    ok = await self._send_group_text(clean_id, text)
                    if ok:
                        success += 1
                    else:
                        # 回退：使用已学习的映射
                        umo = self._group_umo_map.get(clean_id)
                        if umo:
                            await self.context.send_message(
                                umo, self._make_chain(text)
                            )
                            success += 1
                except Exception as e:
                    logger.error(f"推送群组 {group_id} 失败: {e}")

            logger.info(f"大盘推送完成: 成功 {success}/{len(group_list)}")
        except Exception as e:
            logger.error(f"大盘推送数据获取失败: {e}")

    async def _send_group_text(self, group_id: str, text: str) -> bool:
        """通过 OneBot API 发送文本消息到群"""
        platforms = getattr(self.context, "platform_manager", None)
        if not platforms:
            return False
        insts = platforms.get_insts() if hasattr(platforms, "get_insts") else []
        for platform in insts:
            try:
                client = (
                    getattr(platform, "get_client", lambda: None)()
                    or getattr(platform, "client", None)
                    or getattr(platform, "bot", None)
                )
                if not client:
                    continue
                call = (
                    getattr(client, "call_action", None)
                    or (
                        hasattr(client, "api")
                        and getattr(client.api, "call_action", None)
                    )
                )
                if call:
                    await call(
                        "send_group_msg",
                        group_id=int(group_id),
                        message=[{"type": "text", "data": {"text": text}}],
                    )
                    return True
            except Exception:
                continue
        return False

    def _make_chain(self, text: str):
        """构造 MessageChain 对象"""
        return MessageChain().message(text)

    # ------------------------------------------------------------------
    # LLM 分析
    # ------------------------------------------------------------------

    async def _generate_llm_analysis(self, overview: MarketOverview, umo: str = "") -> str:
        """调用 LLM 生成市场分析"""
        try:
            market_text = format_market_overview_text(overview, detail=True)
            prompt = MARKET_ANALYSIS_PROMPT.format(
                current_time=overview.fetch_time.strftime("%Y-%m-%d %H:%M"),
                market_data=market_text,
            )

            model_override = self.config.get("llm_model", "") or None
            llm_kwargs = {}
            if model_override:
                llm_kwargs["model"] = model_override

            # 优先使用配置的提供商，否则自动检测
            configured_provider = self.config.get("llm_provider_id", "")
            provider_id = configured_provider or await self.context.get_current_chat_provider_id(umo)
            if provider_id:
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    **llm_kwargs,
                )
                if resp and getattr(resp, "completion_text", None):
                    return resp.completion_text.strip()

            # 回退：旧版 provider.text_chat
            provider = self.context.get_using_provider()
            if provider:
                resp = await provider.text_chat(
                    prompt=prompt, **({"model": model_override} if model_override else {})
                )
                if resp and getattr(resp, "completion_text", None):
                    return resp.completion_text.strip()

            # LLM 不可用时输出原始数据
            logger.warning("LLM 分析不可用，输出原始数据")
            return market_text + "\n\n（由于 LLM 暂不可用，以上为原始数据）"
        except Exception as e:
            logger.warning(f"LLM 分析失败: {e}")
            text = format_market_overview_text(overview, detail=True)
            return text + f"\n\n（LLM 分析暂不可用）"

    # ------------------------------------------------------------------
    # 命令处理器
    # ------------------------------------------------------------------

    @filter.command("大盘")
    async def cmd_market_overview(self, event: AstrMessageEvent):
        """获取大盘数据，并使用 LLM 分析"""
        self._learn_group_mapping(event)

        yield event.plain_result("正在获取大盘数据...")

        try:
            overview = await fetch_market_overview()
            if not overview.indices:
                yield event.plain_result("获取大盘数据失败，请稍后重试")
                return

            text = format_market_overview_text(overview)
            yield event.plain_result(text)
        except Exception as e:
            logger.error(f"获取大盘数据异常: {e}", exc_info=True)
            yield event.plain_result(f"获取大盘数据失败: {str(e)}")

    @filter.command("大盘分析")
    async def cmd_market_analysis(self, event: AstrMessageEvent):
        """获取大盘数据 + LLM 分析"""
        self._learn_group_mapping(event)

        yield event.plain_result("正在获取大盘数据并进行分析...")

        try:
            overview = await fetch_market_overview()
            if not overview.indices:
                yield event.plain_result("获取大盘数据失败，请稍后重试")
                return

            analysis = await self._generate_llm_analysis(overview, event.unified_msg_origin)
            yield event.plain_result(analysis)
        except Exception as e:
            logger.error(f"大盘分析异常: {e}", exc_info=True)
            yield event.plain_result(f"分析失败: {str(e)}")

    @filter.command("大盘速报")
    async def cmd_market_quick(self, event: AstrMessageEvent):
        """快速获取大盘数据（不含分析）"""
        self._learn_group_mapping(event)
        try:
            overview = await fetch_market_overview()
            if not overview.indices:
                yield event.plain_result("获取大盘数据失败，请稍后重试")
                return
            text = format_market_overview_text(overview, detail=True)
            yield event.plain_result(text)
        except Exception as e:
            logger.error(f"大盘速报异常: {e}", exc_info=True)
            yield event.plain_result(f"获取失败: {str(e)}")

    @filter.command("上证")
    async def cmd_shanghai(self, event: AstrMessageEvent):
        """查询上证指数"""
        result = await self._query_index("000001")
        if result is None:
            yield event.plain_result("查询失败，请稍后重试")
        else:
            yield event.plain_result(result)

    @filter.command("深证")
    async def cmd_shenzhen(self, event: AstrMessageEvent):
        """查询深证成指"""
        result = await self._query_index("399001")
        if result is None:
            yield event.plain_result("查询失败，请稍后重试")
        else:
            yield event.plain_result(result)

    @filter.command("创业板")
    async def cmd_cyb(self, event: AstrMessageEvent):
        """查询创业板指"""
        result = await self._query_index("399006")
        if result is None:
            yield event.plain_result("查询失败，请稍后重试")
        else:
            yield event.plain_result(result)

    @filter.command("科创50")
    async def cmd_kc50(self, event: AstrMessageEvent):
        """查询科创50"""
        result = await self._query_index("000688")
        if result is None:
            yield event.plain_result("查询失败，请稍后重试")
        else:
            yield event.plain_result(result)

    @filter.command("沪深300")
    async def cmd_hs300(self, event: AstrMessageEvent):
        """查询沪深300"""
        result = await self._query_index("000300")
        if result is None:
            yield event.plain_result("查询失败，请稍后重试")
        else:
            yield event.plain_result(result)

    async def _query_index(self, code: str) -> Optional[str]:
        """查询单只指数，返回格式化文本"""
        try:
            data = await fetch_single_index(code)
            if data is None:
                return None
            return (
                f"{data.change_symbol} {data.name}（{data.code}）\n"
                f"点位：{data.latest_price:.2f}\n"
                f"涨跌额：{data.change:+.2f}\n"
                f"涨幅：{data.change_pct:+.2f}%\n"
                f"最高：{data.high_price:.2f}  最低：{data.low_price:.2f}\n"
                f"昨收：{data.pre_close:.2f}  开盘：{data.open_price:.2f}\n"
                f"成交额：{data.amount / 1e8:.2f}亿"
            )
        except Exception as e:
            logger.error(f"查询指数[{code}]异常: {e}")
            return None

    # ------------------------------------------------------------------
    # 配置查看命令
    # ------------------------------------------------------------------

    @filter.command("大盘状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看插件推送配置状态"""
        enabled = self.config.get("enable_scheduled_push", False)
        times = self.config.get("scheduled_push_times", "09:30,11:30,15:00")
        groups = self.config.get("scheduled_push_groups", [])
        llm = self.config.get("enable_llm_analysis", False)
        llm_provider = self.config.get("llm_provider_id", "") or "自动检测"
        llm_model = self.config.get("llm_model", "") or "（默认）"

        text = (
            f"📊 A股大盘插件状态\n"
            f"定时推送：{'✅ 已开启' if enabled else '❌ 已关闭'}\n"
            f"推送时间：{times}\n"
            f"推送群组：{len(groups)} 个\n"
            f"LLM分析：{'✅ 已开启' if llm else '❌ 已关闭'}\n"
            f"LLM提供商：{llm_provider}\n"
            f"LLM模型：{llm_model}\n"
            f"已学习群映射：{len(self._group_umo_map)} 个"
        )
        yield event.plain_result(text)

    @filter.command("大盘提供商")
    async def cmd_list_providers(self, event: AstrMessageEvent):
        """查看可用的 LLM 提供商列表"""
        try:
            providers = self.context.get_all_providers()
            if not providers:
                yield event.plain_result("暂无可用 LLM 提供商")
                return

            lines = ["🤖 可用 LLM 提供商列表：\n"]
            for p in providers:
                try:
                    meta = p.meta()
                    provider_id = meta.id
                    model = meta.model or "（未设置）"
                    lines.append(f"• ID: {provider_id}")
                    lines.append(f"  模型: {model}")
                    # 尝试获取当前 key 状态
                    try:
                        key = p.get_current_key()
                        key_status = "✅" if key else "❌"
                        lines.append(f"  密钥: {key_status}")
                    except Exception:
                        pass
                    lines.append("")
                except Exception:
                    continue

            lines.append("💡 使用方式：")
            lines.append('1. 复制上方的 "ID" 值')
            lines.append("2. 在 AstrBot 管理后台 → 插件配置 → astrbot_plugin_astock_market")
            lines.append('   将 ID 填入 "LLM 提供商 ID" 字段')
            lines.append('3. （可选）如需指定模型，填入 "LLM 模型名" 字段')

            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error(f"获取提供商列表异常: {e}", exc_info=True)
            yield event.plain_result(f"获取失败: {str(e)}")
