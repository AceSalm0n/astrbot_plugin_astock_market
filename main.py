"""
AstrBot A 股大盘数据插件
定时获取 A 股主要指数数据，支持 LLM 智能分析解读

支持指数：上证指数、深证成指、创业板指、科创50、沪深300
"""

import asyncio
import base64
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .card_renderer import render_market_card
from .market_api import (
    BEIJING_TZ,
    MarketOverview,
    close_session,
    fetch_market_overview,
    fetch_single_index,
    format_market_overview_text,
    reset_market_state,
)
from .market_history import HistoryClient
from .trend_analysis import (
    TREND_ANALYSIS_PROMPT,
    build_trend_context,
    load_trading_calendar,
    resolve_baseline,
)

LLM_TIMEOUT = 30
TREND_LLM_TIMEOUT = 90
PUSH_GRACE_SECONDS = 60

# ---------------------------------------------------------------------------
# LLM 分析提示词
# ---------------------------------------------------------------------------

MARKET_ANALYSIS_PROMPT = """请仅根据以下指数行情，输出约300字的简洁客观解读。

查询时间（北京时间）：{current_time}

{market_data}

请使用纯文本，按“指数表现、指数间差异、数据局限”三个标题分段输出。
每段简洁，优先保证三个部分齐全且句子完整；不要为了控制篇幅省略“数据局限”。
不要使用 Markdown 加粗标记或表格。
事实必须能在所给数据中核对；推测必须标明为推测。
这些数据不含行业板块、资金流向、新闻或全市场股票涨跌家数，请勿编造这些信息，
也不要将监控指数的涨跌数量解释为股票涨跌家数。
缺失数据不可视为零；行情时间未知不可称为实时行情。
来源时间不一致、缺失指数或缓存情况必须如实说明。不构成投资建议。"""


# ===========================================================================
# 插件主类
# ===========================================================================


@register(
    "astrbot_plugin_astock_market",
    "AstrBot",
    "A股大盘数据定时推送插件，支持 LLM 智能分析",
    "1.1.0",
    "",
)
class AStockMarketPlugin(Star):
    """A股大盘数据插件"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: AstrBotConfig = config if config is not None else {}
        self._terminated = False
        self._history_client = None
        self._trend_calendar = None
        self._history_state = "尚未查询"

        # 定时推送任务句柄
        self._push_task: asyncio.Task | None = None
        self._startup_task: asyncio.Task | None = None
        self._last_push_slot: datetime | None = None
        self._next_push_at: datetime | None = None
        reset_market_state()

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
        return (
            StarTools.get_data_dir("astrbot_plugin_astock_market")
            / "group_mapping.json"
        )

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
        self._terminated = True
        try:
            for t in (self._startup_task, self._push_task):
                if t and not t.done():
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
        finally:
            try:
                client = getattr(self, "_history_client", None)
                if client is not None:
                    await client.close()
            finally:
                await close_session()
        logger.info("A股大盘插件已卸载")

    # ------------------------------------------------------------------
    # 定时推送调度
    # ------------------------------------------------------------------

    def _parse_push_times(self) -> list[time]:
        """Parse scheduled_push_times config into a list of time objects."""
        push_times_str = self.config.get("scheduled_push_times", "09:30,11:30,15:00")
        if not isinstance(push_times_str, str):
            logger.warning("Invalid scheduled push times: expected a string")
            return []
        times: set[time] = set()
        for t in push_times_str.split(","):
            t = t.strip()
            try:
                h, m = map(int, t.split(":"))
                times.add(time(h, m))
            except (ValueError, AttributeError):
                logger.warning(f"Skipping invalid push time: {t}")
        return sorted(times)

    def _next_push_time(
        self, now: datetime, times: list[time], closed_dates: set[date]
    ) -> datetime | None:
        """Find the next future slot on an eligible local date.

        Args:
            now: Current time, with an explicit time zone.
            times: Sorted configured push times.
            closed_dates: Additional manually maintained closed dates.

        Returns:
            The next Beijing-time slot, or None when no times are configured.
        """
        if not times:
            return None
        now = now.astimezone(BEIJING_TZ)
        candidate_date = now.date()
        while True:
            if candidate_date.weekday() < 5 and candidate_date not in closed_dates:
                for push_time in times:
                    candidate = datetime.combine(candidate_date, push_time, BEIJING_TZ)
                    if candidate > now:
                        return candidate
            candidate_date += timedelta(days=1)

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
        """Keep a pending slot until it is handled, with bounded wake-up checks."""
        schedule_key = None
        while True:
            try:
                now = datetime.now(BEIJING_TZ)
                enabled = self.config.get("enable_scheduled_push", False)
                push_groups = self.config.get("scheduled_push_groups", [])
                times = self._parse_push_times()
                closed_dates: set[date] = set()
                raw_dates = self.config.get("market_closed_dates", [])
                if not isinstance(raw_dates, list):
                    logger.warning("Invalid market_closed_dates: expected a list")
                    raw_dates = []
                for raw_date in raw_dates:
                    try:
                        closed_dates.add(date.fromisoformat(raw_date))
                    except (ValueError, TypeError):
                        logger.warning(f"Skipping invalid closed date: {raw_date}")
                if (
                    not enabled
                    or not isinstance(push_groups, list)
                    or not push_groups
                    or not times
                ):
                    self._next_push_at = None
                    schedule_key = None
                    await asyncio.sleep(60)
                    continue
                current_key = (tuple(times), frozenset(closed_dates))
                if current_key != schedule_key:
                    pending = self._next_push_at
                    if (
                        pending is None
                        or pending > now
                        or pending.time() not in times
                        or pending.date().weekday() >= 5
                        or pending.date() in closed_dates
                    ):
                        self._next_push_at = None
                    schedule_key = current_key
                if self._next_push_at is None:
                    self._next_push_at = self._next_push_time(now, times, closed_dates)
                    logger.info(f"Next market push: {self._next_push_at}")
                target = self._next_push_at
                delay = (target - now).total_seconds()
                if delay > 0:
                    await asyncio.sleep(min(delay, 60))
                    continue
                self._next_push_at = None
                if (
                    now.date() != target.date()
                    or now.weekday() >= 5
                    or now.date() in closed_dates
                    or -delay > PUSH_GRACE_SECONDS
                    or (
                        self._last_push_slot is not None
                        and target <= self._last_push_slot
                    )
                ):
                    logger.warning(
                        f"Skipping expired or ineligible push slot: {target}"
                    )
                    continue
                # Mark the slot before sending; failures must not resend the batch.
                self._last_push_slot = target
                await self._push_to_groups(list(push_groups))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduled push failed: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _push_to_groups(self, group_list: list[str]):
        """Fetch fresh market data once and send it to the configured groups.

        Args:
            group_list: Group IDs eligible for this scheduled push.
        """
        try:
            overview = await fetch_market_overview(force_refresh=True)
            if not overview.indices:
                logger.warning("Skipping scheduled push: no valid market data")
                return
            image, text, fallback = await self._prepare_market_output(
                overview, analyze=self.config.get("enable_llm_analysis", False)
            )

            success = 0
            for group_id in group_list:
                try:
                    clean_id = self._extract_group_id(group_id)
                    ok = (
                        await self._send_group_text(clean_id, text, image)
                        if image is not None
                        else await self._send_group_text(clean_id, text)
                    )
                    umo = self._group_umo_map.get(clean_id)
                    if not ok and umo:
                        try:
                            ok = bool(
                                await self.context.send_message(
                                    umo, self._make_chain(text, image)
                                )
                            )
                        except Exception as e:
                            logger.warning(
                                f"Mapped market send failed [{clean_id}]: {e}"
                            )
                    # Only retry the failed group's payload, never the whole batch.
                    if not ok and image is not None:
                        logger.warning(f"Falling back to market text [{clean_id}]")
                        ok = await self._send_group_text(clean_id, fallback)
                        if not ok and umo:
                            ok = bool(
                                await self.context.send_message(
                                    umo, self._make_chain(fallback)
                                )
                            )
                    if ok:
                        success += 1
                except Exception as e:
                    logger.error(f"Market push failed [{group_id}]: {e}")

            logger.info(f"Market push complete: success {success}/{len(group_list)}")
        except Exception as e:
            logger.error(f"Market push preparation failed: {e}")

    async def _send_group_text(
        self, group_id: str, text: str, image: bytes | None = None
    ) -> bool:
        """Send a market payload through the existing OneBot clients.

        Args:
            group_id: Numeric group ID.
            text: Market text or an optional interpretation following the image.
            image: In-memory PNG, or None for a text-only payload.

        Returns:
            True if a OneBot call completed without an exception.
        """
        message = []
        if image is not None:
            encoded = base64.b64encode(image).decode("ascii")
            message.append({"type": "image", "data": {"file": "base64://" + encoded}})
        if text:
            message.append({"type": "text", "data": {"text": text}})
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
                call = getattr(client, "call_action", None) or (
                    hasattr(client, "api") and getattr(client.api, "call_action", None)
                )
                if call:
                    await call(
                        "send_group_msg",
                        group_id=int(group_id),
                        message=message,
                    )
                    return True
            except Exception as e:
                logger.warning(f"OneBot market send failed [{group_id}]: {e}")
                continue
        return False

    def _make_chain(self, text: str, image: bytes | None = None):
        """Build a fresh chain with an image before its interpretation.

        Args:
            text: Text to append, if nonempty.
            image: In-memory PNG, or None for a text-only chain.

        Returns:
            An independent AstrBot message chain.
        """
        chain = MessageChain()
        if image is not None:
            from astrbot.api.message_components import Image

            chain.chain.append(Image.fromBytes(image))
        if text:
            chain.message(text)
        return chain

    async def _prepare_market_output(
        self, overview: MarketOverview, umo: str = "", *, analyze: bool = False
    ) -> tuple[bytes | None, str, str]:
        """Prepare one payload and its full-text fallback for all recipients.

        Args:
            overview: Validated market data from the unchanged fetch layer.
            umo: Session origin used for model selection.
            analyze: Whether to request one bounded interpretation.

        Returns:
            PNG or None, primary text, and complete text for a send fallback.
        """
        image = None
        if self.config.get("enable_image_output", False):
            try:
                render_kwargs = {}
                backgrounds = self.config.get("card_background", [])
                if backgrounds:
                    try:
                        if not isinstance(backgrounds, list) or not isinstance(
                            backgrounds[0], str
                        ):
                            raise ValueError(
                                "Background configuration must be a file list"
                            )
                        relative = Path(backgrounds[0])
                        if (
                            relative.is_absolute()
                            or ".." in relative.parts
                            or relative.parts[:-1] != ("files", "card_background")
                            or relative.suffix.lower()
                            not in {".jpg", ".jpeg", ".png", ".webp"}
                        ):
                            raise ValueError("Invalid uploaded background path")
                        data_root = StarTools.get_data_dir(
                            "astrbot_plugin_astock_market"
                        ).resolve()
                        uploaded = (data_root / relative).resolve()
                        uploaded.relative_to(data_root / "files" / "card_background")
                        render_kwargs["background_path"] = uploaded
                    except Exception as error:
                        logger.warning(
                            "Ignoring invalid card background configuration (%s)",
                            type(error).__name__,
                        )
                image = await asyncio.to_thread(
                    render_market_card, overview, **render_kwargs
                )
                if not isinstance(image, bytes) or not image:
                    raise ValueError("Renderer returned no PNG bytes")
            except Exception as e:
                logger.warning(f"Market card unavailable; using text: {e}")
                image = None
        if analyze:
            text = await self._generate_llm_analysis(
                overview, umo, include_market=image is None
            )
            fallback = (
                format_market_overview_text(overview) + "\n\n" + text
                if image is not None
                else text
            )
        else:
            fallback = format_market_overview_text(overview)
            text = "" if image is not None else fallback
        return image, text, fallback

    # ------------------------------------------------------------------
    # LLM 分析
    # ------------------------------------------------------------------

    def _resolve_llm_provider_info(self) -> tuple[str, str]:
        """Return configured LLM provider id and model for status display."""
        provider_id = self.config.get("llm_provider_id", "") or ""
        legacy_model = self.config.get("llm_model", "") or ""

        if not provider_id:
            return "自动检测", legacy_model or "（默认）"

        model = legacy_model or "（默认）"
        for provider in self.context.get_all_providers():
            try:
                meta = provider.meta()
                if meta.id == provider_id:
                    model = legacy_model or meta.model or "（未设置）"
                    break
            except Exception:
                continue
        return provider_id, model

    async def _generate_llm_analysis(
        self, overview: MarketOverview, umo: str = "", *, include_market: bool = True
    ) -> str:
        """Interpret full source data and optionally include it in the response.

        Args:
            overview: Validated index data and its response time.
            umo: Session origin, or an empty string for a scheduled push.
            include_market: Include original market text in the returned response.

        Returns:
            Analysis or a fallback explanation, optionally preceded by market text.
        """
        market_text = format_market_overview_text(overview, detail=True)
        prefix = market_text + "\n\n" if include_market else ""
        if getattr(self, "_terminated", False):
            raise asyncio.CancelledError
        if not overview.indices:
            return prefix + "（暂无有效行情，已跳过分析）"
        prompt, analysis_context, is_trend = None, "", False
        if self.config.get("enable_trend_analysis", False):
            checked_at = datetime.now(BEIJING_TZ)
            try:
                calendar = getattr(self, "_trend_calendar", None)
                if calendar is None:
                    calendar = load_trading_calendar(
                        Path(__file__).parent / "assets" / "trading_calendar.json"
                    )
                    self._trend_calendar = calendar
                baseline = resolve_baseline(checked_at, calendar)
                if baseline is None:
                    raise ValueError("Trading calendar does not cover the query baseline")
                client = getattr(self, "_history_client", None)
                if client is None:
                    client = HistoryClient(calendar)
                    self._history_client = client
                batch = await client.fetch(baseline)
                if getattr(self, "_terminated", False):
                    raise asyncio.CancelledError
                checked_at = datetime.now(BEIJING_TZ)
                context = build_trend_context(
                    batch, checked_at, calendar,
                    self.config.get("market_closed_dates", []),
                )
                self._history_state = (
                    f"{checked_at:%Y-%m-%d %H:%M:%S}：基准{baseline}，"
                    f"合格历史{len(context.eligible_codes)}/5只"
                )
                if context.eligible_codes:
                    quality = context.quality_notes
                    if any(idx.quote_time is None for idx in overview.indices):
                        quality += "\n快照时间未知，不能视为实时行情或用于确认当前趋势。"
                    if any(idx.quote_time is not None and idx.quote_time.astimezone(BEIJING_TZ).date() < baseline for idx in overview.indices):
                        quality += "\n当前快照落后历史基准，不能用于确认当前趋势。"
                    prompt = TREND_ANALYSIS_PROMPT.format(
                        current_time=checked_at.strftime("%Y-%m-%d %H:%M"),
                        baseline=f"{baseline}日收盘", horizon=context.header,
                        market_data=market_text, trend_data=context.history_text,
                        quality_notes=quality,
                    )
                    analysis_context = context.header + "\n\n"
                    is_trend = True
                else:
                    analysis_context = "（趋势不可用：" + context.quality_notes + "）\n\n"
                    self._history_state += "，趋势不可用"
            except Exception as exc:
                logger.warning("Trend preparation failed: %s", exc)
                analysis_context = "（趋势不可用：历史数据或交易日历不可用）\n\n"
                self._history_state = f"{checked_at:%Y-%m-%d %H:%M:%S}：历史数据或交易日历不可用"
        if prompt is None:
            today = datetime.now(BEIJING_TZ).date()
            if any(
                idx.quote_time is not None
                and idx.quote_time.astimezone(BEIJING_TZ).date() < today
                for idx in overview.indices
            ):
                return prefix + analysis_context + "（包含历史行情，已跳过当日分析）"
            prompt = MARKET_ANALYSIS_PROMPT.format(
                current_time=overview.fetch_time.strftime("%Y-%m-%d %H:%M"),
                market_data=market_text,
            )

        async def request_analysis() -> str:
            """Resolve the selected provider and request a single interpretation."""
            if getattr(self, "_terminated", False):
                raise asyncio.CancelledError
            configured = self.config.get("llm_provider_id", "") or ""
            model_override = self.config.get("llm_model", "") or None
            kwargs = {"model": model_override} if model_override else {}
            generate = getattr(self.context, "llm_generate", None)
            provider_id = configured
            provider = None
            if not configured:
                resolve = getattr(self.context, "get_current_chat_provider_id", None)
                if umo and callable(resolve) and callable(generate):
                    provider_id = await resolve(umo)
                else:
                    get_async = getattr(self.context, "get_using_provider_async", None)
                    if callable(get_async):
                        provider = await get_async(umo=umo or None)
                    else:
                        get_provider = getattr(self.context, "get_using_provider", None)
                        if callable(get_provider):
                            provider = get_provider(umo) if umo else get_provider()
                    if provider and callable(generate):
                        provider_id = provider.meta().id
            if getattr(self, "_terminated", False):
                raise asyncio.CancelledError
            if provider_id and callable(generate):
                response = await generate(
                    chat_provider_id=provider_id, prompt=prompt, **kwargs
                )
            else:
                if configured:
                    # Legacy SDK lookup must still honor the explicit selection.
                    for item in self.context.get_all_providers():
                        if item.meta().id == configured:
                            provider = item
                            break
                if provider is None:
                    return ""
                response = await provider.text_chat(prompt=prompt, **kwargs)
            completion = getattr(response, "completion_text", None)
            return completion.strip() if isinstance(completion, str) else ""

        try:
            analysis = await asyncio.wait_for(
                request_analysis(),
                timeout=TREND_LLM_TIMEOUT if is_trend else LLM_TIMEOUT,
            )
            if getattr(self, "_terminated", False):
                raise asyncio.CancelledError
            if analysis:
                title = "🤖 趋势解读\n" if is_trend else "🤖 指数解读\n"
                return prefix + analysis_context + title + analysis
            logger.warning("LLM returned no usable analysis")
        except Exception as e:
            logger.warning(f"LLM analysis unavailable: {e}")
        explanation = (
            "（LLM 分析暂不可用，以上为原始行情）"
            if include_market
            else "（LLM 分析暂不可用）"
        )
        return prefix + analysis_context + explanation

    # ------------------------------------------------------------------
    # 命令处理器
    # ------------------------------------------------------------------

    @filter.command("大盘")
    async def cmd_market_overview(self, event: AstrMessageEvent):
        """Fetch and return the market overview without model analysis."""
        self._learn_group_mapping(event)

        yield event.plain_result("正在获取大盘数据...")

        try:
            overview = await fetch_market_overview()
            if not overview.indices:
                yield event.plain_result("获取大盘数据失败，请稍后重试")
                return

            image, text, fallback = await self._prepare_market_output(overview)
            if image is not None:
                try:
                    result = event.chain_result(self._make_chain(text, image).chain)
                except Exception as e:
                    logger.warning(f"Market image result unavailable; using text: {e}")
                    result = event.plain_result(fallback)
            else:
                result = event.plain_result(text)
            yield result
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

            image, text, fallback = await self._prepare_market_output(
                overview, event.unified_msg_origin, analyze=True
            )
            if image is not None:
                try:
                    result = event.chain_result(self._make_chain(text, image).chain)
                except Exception as e:
                    logger.warning(f"Market image result unavailable; using text: {e}")
                    result = event.plain_result(fallback)
            else:
                result = event.plain_result(text)
            yield result
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
            image, text, fallback = await self._prepare_market_output(overview)
            if image is not None:
                try:
                    result = event.chain_result(self._make_chain(text, image).chain)
                except Exception as e:
                    logger.warning(f"Market image result unavailable; using text: {e}")
                    result = event.plain_result(fallback)
            else:
                result = event.plain_result(text)
            yield result
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

    async def _query_index(self, code: str) -> str | None:
        """Return a single index with the shared missing-data formatting.

        Args:
            code: Supported index code.

        Returns:
            Formatted index data, or None when every source failed.
        """
        try:
            data = await fetch_single_index(code)
            if data is None:
                return None
            return format_market_overview_text(
                MarketOverview([data], datetime.now(BEIJING_TZ), data.source),
                detail=False,
            )
        except Exception as e:
            logger.error(f"Index query failed [{code}]: {e}")
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
        image = self.config.get("enable_image_output", False)
        backgrounds = self.config.get("card_background", [])
        background_status = (
            "自定义背景（使用第一张；异常时回退默认底色）"
            if isinstance(backgrounds, list) and backgrounds
            else "默认浅色背景"
        )
        llm_provider, llm_model = self._resolve_llm_provider_info()
        closed_dates = self.config.get("market_closed_dates", [])
        calendar_status = (
            f"周末及手动休市日期（{len(closed_dates)} 条，请按年度维护）"
            if isinstance(closed_dates, list) and closed_dates
            else "仅过滤周末（未配置休市日期）"
        )
        try:
            trend_calendar = load_trading_calendar(
                Path(__file__).parent / "assets" / "trading_calendar.json"
            )
            trend_years = "、".join(str(year) for year in sorted(trend_calendar.years))
        except Exception as exc:
            logger.warning("Trend calendar unavailable for status: %s", exc)
            trend_years = "不可用"
        trend_state = getattr(self, "_history_state", "尚未查询")

        text = (
            f"📊 A股大盘插件状态\n"
            f"定时推送：{'✅ 已开启' if enabled else '❌ 已关闭'}\n"
            f"推送时间：{times}\n"
            f"时区：北京时间 UTC+8\n"
            f"休市过滤：{calendar_status}\n"
            f"推送群组：{len(groups)} 个\n"
            f"图片输出配置：{'已开启（失败时尝试文字回退）' if image else '已关闭（文字输出）'}\n"
            f"卡片背景配置：{background_status}，仅图片模式生效\n"
            f"LLM分析：{'✅ 已开启' if llm else '❌ 已关闭'}\n"
            f"3—5交易日趋势：{'已开启' if self.config.get('enable_trend_analysis', False) else '已关闭'}\n"
            f"趋势日历覆盖：{trend_years}（不改变原推送休市配置）\n"
            f"最近历史检查：{trend_state}（仅历史记录，不代表当前可用）\n"
            f"LLM提供商：{llm_provider}\n"
            f"LLM模型：{llm_model}\n"
            f"已学习群映射：{len(self._group_umo_map)} 个\n"
            f"💡 可在 AstrBot 管理后台 → 插件配置中选择 LLM 提供商"
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
            lines.append(
                "1. 推荐在 AstrBot 管理后台 → 插件配置 → astrbot_plugin_astock_market"
            )
            lines.append("   点击「LLM 提供商」旁的「选择提供商」直接挑选模型")
            lines.append('2. 也可复制上方的 "ID" 值手动填入配置')

            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error(f"获取提供商列表异常: {e}", exc_info=True)
            yield event.plain_result(f"获取失败: {str(e)}")
