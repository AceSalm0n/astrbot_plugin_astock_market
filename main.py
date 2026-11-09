"""
AstrBot A 股大盘数据插件
定时获取 A 股主要指数数据，支持 LLM 智能分析解读

支持指数：上证指数、深证成指、创业板指、科创50、沪深300
"""

import asyncio
import base64
import json
import re
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .card_renderer import render_market_card
from .market_alerts import (
    ALERT_AGE,
    AlertEngine,
    market_session,
    parse_alert_thresholds,
)
from .market_api import (
    BEIJING_TZ,
    MarketOverview,
    close_session,
    fetch_market_overview,
    fetch_single_index,
    format_market_overview_text,
    reset_market_state,
)
from .market_history import HistoryBatch, HistoryClient
from .market_reports import (
    MIDDAY_WINDOW,
    NAMES,
    REPORT_LABELS,
    REPORT_TIMES,
    MarketReport,
    build_close_report,
    build_midday_report,
    format_report,
    report_from_dict,
    report_to_dict,
)
from .notification_state import NotificationState, write_json_atomic
from .trading_calendar import TradingCalendar, load_trading_calendar, resolve_baseline
from .trend_analysis import (
    TREND_ANALYSIS_PROMPT,
    build_trend_context,
)
from .trend_renderer import build_chart_data, format_chart_summary, render_trend_chart

LLM_TIMEOUT = 30
TREND_LLM_TIMEOUT = 90
PUSH_GRACE_SECONDS = 60
INDEX_ALIASES = {
    **{code: code for code in NAMES},
    **{name: code for code, name in NAMES.items()},
    "上证": "000001",
    "深证": "399001",
    "创业板": "399006",
}

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
    "1.2.1",
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
        self._group_mapping_conflicts: set[str] = set()
        self._group_accounts: dict[str, str] = {}
        self._load_group_mapping()
        self._initialize_market_features(self._mapping_path().parent)
        if self.config.get("enable_index_alerts", False):
            self._alert_task = asyncio.create_task(self._alert_loop())
        if any(self.config.get(f"enable_{kind}_report", False) for kind in REPORT_TIMES):
            self._report_task = asyncio.create_task(self._report_loop())

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
                raw = json.loads(path.read_text(encoding="utf-8"))
                self._group_umo_map = {
                    key: value for key, value in raw.items() if key.isdigit() and isinstance(value, str)
                }
                conflicts = raw.get("_conflicts", [])
                if isinstance(conflicts, list):
                    self._group_mapping_conflicts = {value for value in conflicts if isinstance(value, str)}
                accounts = raw.get("_accounts", {})
                if isinstance(accounts, dict):
                    self._group_accounts = {
                        key: value for key, value in accounts.items()
                        if key.isdigit() and isinstance(value, str) and value.isdigit()
                    }
                logger.info(f"已加载 {len(self._group_umo_map)} 个群组映射")
            except Exception as e:
                logger.warning(f"加载群组映射失败: {e}")

    def _save_group_mapping(self):
        try:
            path = self._mapping_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        **self._group_umo_map,
                        "_conflicts": sorted(self._group_mapping_conflicts),
                        "_accounts": self._group_accounts,
                    },
                    ensure_ascii=False, indent=2,
                ),
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
        """Bind only actual group events and mark ambiguous account mappings."""
        group_id = str(event.get_group_id())
        umo = event.unified_msg_origin
        account = str(event.get_self_id())
        if not group_id.isdigit() or not isinstance(umo, str) or ":GroupMessage:" not in umo:
            return
        existing = self._group_umo_map.get(group_id)
        bound = self._group_accounts.get(group_id)
        if group_id in self._group_mapping_conflicts:
            return
        if bound and (
            (existing and existing.split(":", 1)[0] != umo.split(":", 1)[0])
            or (account.isdigit() and bound != account)
        ):
            self._group_mapping_conflicts.add(group_id)
            self._save_group_mapping()
        elif existing is None or ":GroupMessage:" not in existing or (not bound and account.isdigit()):
            self._group_umo_map[group_id] = umo
            if account.isdigit():
                self._group_accounts[group_id] = account
            self._save_group_mapping()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def terminate(self):
        self._terminated = True
        try:
            tasks = (
                self._startup_task, self._push_task,
                getattr(self, "_alert_task", None), getattr(self, "_report_task", None),
                *getattr(self, "_report_workers", {}).values(),
                *getattr(self, "_report_finalizers", {}).values(),
            )
            active = {task for task in tasks if task is not None and not task.done()}
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            lock = getattr(self, "_report_lock", None)
            if lock is not None:
                async with lock:
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
        self, group_id: str, text: str, image: bytes | None = None,
        *, platform_id: str | None = None, account: str | None = None,
    ) -> bool:
        """Send a market payload through the existing OneBot clients.

        Args:
            group_id: Numeric group ID.
            text: Market text or an optional interpretation following the image.
            image: In-memory PNG, or None for a text-only payload.
            platform_id: Exact OneBot adapter for a bound notification.
            account: Exact OneBot account for a bound notification.

        Returns:
            True if a OneBot call completed without an exception.
        """
        message = []
        if image is not None:
            encoded = base64.b64encode(image).decode("ascii")
            message.append({"type": "image", "data": {"file": "base64://" + encoded}})
        if text:
            message.append({"type": "text", "data": {"text": text}})
        if platform_id is not None:
            if account is None or not account.isdigit():
                return False
            platform = self.context.get_platform_inst(platform_id)
            if platform is None or platform.meta().name != "aiocqhttp":
                return False
            await platform.get_client().call_action(
                "send_group_msg", self_id=account, group_id=int(group_id), message=message
            )
            return True
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

    async def _request_llm(self, prompt: str, umo: str = "") -> str:
        """Resolve the configured provider and return a single completion."""
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
                calendar = self._get_calendar()
                baseline = resolve_baseline(checked_at, calendar)
                if baseline is None:
                    raise ValueError("Trading calendar does not cover the query baseline")
                batch = await self._get_history_client().fetch(baseline)
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

        try:
            analysis = await asyncio.wait_for(
                self._request_llm(prompt, umo),
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

    def _initialize_market_features(self, data_dir: Path) -> None:
        """Prepare isolated storage and task handles for the new features."""
        self._notifications = NotificationState(data_dir / "notification_state.json")
        self._reports_dir = data_dir / "reports"
        self._report_lock = asyncio.Lock()
        self._report_workers: dict[tuple[str, date], asyncio.Task] = {}
        self._report_finalizers: dict[tuple[str, date], asyncio.Task] = {}
        self._report_finals: dict[tuple[str, date], MarketReport] = {}
        self._seen_report_jobs: set[str] = set()
        self._notification_limit = asyncio.Semaphore(2)
        self._chart_limit = asyncio.Semaphore(1)
        self._alert_task: asyncio.Task | None = None
        self._report_task: asyncio.Task | None = None
        self._alert_count = None
        self._last_report = None

    def _get_calendar(self) -> TradingCalendar:
        """Reuse one validated calendar for all historical features."""
        if self._trend_calendar is None:
            self._trend_calendar = load_trading_calendar(
                Path(__file__).parent / "assets" / "trading_calendar.json"
            )
        return self._trend_calendar

    def _get_history_client(self) -> HistoryClient:
        """Reuse one history client across reports, charts and analysis."""
        if self._history_client is None:
            self._history_client = HistoryClient(self._get_calendar())
        return self._history_client

    def _is_notification_day(self, day: date) -> bool:
        """Apply the verified calendar plus extra configured closures."""
        closed = self.config.get("market_closed_dates", [])
        return self._get_calendar().is_session(day) and (
            not isinstance(closed, list) or day.isoformat() not in closed
        )

    def _notification_targets(self) -> tuple[str, ...]:
        """Return configured, unambiguous group origins in stable order."""
        groups = self.config.get("scheduled_push_groups", [])
        if not isinstance(groups, list):
            return ()
        targets = {}
        for raw in groups:
            group_id = self._extract_group_id(raw)
            umo = self._group_umo_map.get(group_id)
            if (
                umo
                and group_id in self._group_accounts
                and group_id not in self._group_mapping_conflicts
            ):
                parts = umo.split(":", 2)
                if (
                    len(parts) == 3
                    and parts[0]
                    and parts[1] == "GroupMessage"
                    and self._extract_group_id(umo) == group_id
                ):
                    targets[f"{parts[0]}:GroupMessage:{group_id}"] = None
        return tuple(targets)

    async def _submit_notification(
        self,
        notification_id: str,
        trade_date: date,
        targets: tuple[str, ...],
        text: str,
        image: bytes | None = None,
        *,
        counts_as_alert: bool = False,
        deadline: datetime,
    ) -> dict[str, str]:
        """Submit to known sessions and avoid retrying uncertain platform results.

        Args:
            notification_id: Stable ID used for deduplication.
            trade_date: Beijing date of the notification.
            targets: Frozen, already-resolved group session identifiers.
            text: Complete text payload and image fallback.
            image: Optional in-memory chart.
            counts_as_alert: Whether to charge the daily alert quota.
            deadline: Last allowed time for this notification.

        Returns:
            Per-target outcome labels.
        """
        await self._notifications.load(datetime.now(BEIJING_TZ))

        async def submit(target):
            async with self._notification_limit:
                for _ in range(2):
                    now = datetime.now(BEIJING_TZ)
                    if (
                        self._terminated
                        or now.date() != trade_date
                        or now >= deadline
                        or target not in self._notification_targets()
                    ):
                        return target, "not_submitted"
                    platform_id, _, group_id = target.split(":", 2)
                    account = self._group_accounts[group_id]
                    reserved = await self._notifications.reserve(
                        notification_id, target, now, alert=counts_as_alert
                    )
                    if not reserved:
                        return target, "skipped"
                    remaining = (deadline - datetime.now(BEIJING_TZ)).total_seconds()
                    if (
                        self._terminated
                        or remaining <= 0
                        or target not in self._notification_targets()
                    ):
                        status = "not_submitted"
                    else:
                        try:
                            ok = await asyncio.wait_for(
                                self._send_group_text(
                                    group_id,
                                    text,
                                    image,
                                    platform_id=platform_id,
                                    account=account,
                                ),
                                timeout=min(10, remaining),
                            )
                            status = "submitted" if ok else "not_submitted"
                        except Exception as exc:
                            logger.warning(
                                "Market notification submission uncertain: %s",
                                type(exc).__name__,
                            )
                            status = "unknown"
                    await self._notifications.finish(
                        notification_id, target, now, status, alert=counts_as_alert
                    )
                    if status != "not_submitted":
                        return target, status
                return target, "not_submitted"

        tasks = [
            asyncio.create_task(submit(target)) for target in dict.fromkeys(targets)
        ]
        try:
            return dict(await asyncio.gather(*tasks))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _alert_loop(self) -> None:
        """Sample the market only within enabled exchange sessions."""
        await asyncio.sleep(15)
        engine, engine_config = None, None
        range_history = None
        retry_after = None
        while not self._terminated:
            started = asyncio.get_running_loop().time()
            now = datetime.now(BEIJING_TZ)
            window = market_session(now)
            try:
                targets = self._notification_targets()
                if (
                    self.config.get("enable_index_alerts", False)
                    and window
                    and targets
                    and self._is_notification_day(now.date())
                ):
                    await self._notifications.load(now)
                    raw_thresholds = self.config.get(
                        "alert_thresholds_pct", [1.0, 2.0, 3.0]
                    )
                    try:
                        thresholds = parse_alert_thresholds(raw_thresholds)
                    except ValueError:
                        thresholds = ()
                    ranges = self.config.get("enable_range_breakout_alerts", False)
                    if (thresholds, ranges) != engine_config:
                        replacement = AlertEngine(
                            thresholds, enable_range_breakouts=ranges
                        )
                        saved = self._notifications.snapshot()["alert_state"]
                        if saved.get("date") == now.date().isoformat():
                            replacement.restore_state(saved)
                        engine, engine_config = replacement, (thresholds, ranges)
                    if thresholds or ranges:
                        if ranges and (retry_after is None or now >= retry_after):
                            retry_after = now + timedelta(minutes=15)
                            baseline = resolve_baseline(now, self._get_calendar())
                            if (
                                range_history is not None
                                and range_history.expected_date != baseline
                            ):
                                range_history = None
                            if baseline is not None and baseline < now.date():
                                range_history = await self._get_history_client().fetch(
                                    baseline
                                )
                        snapshot = await fetch_market_overview(
                            max_quote_age_seconds=ALERT_AGE,
                            quote_window=window,
                        )
                        checked = datetime.now(BEIJING_TZ)
                        self._alert_count = len(snapshot.indices)
                        if (
                            self._terminated
                            or market_session(checked) != window
                            or checked.date() != now.date()
                        ):
                            continue
                        events = engine.evaluate(snapshot, range_history, checked)
                        saved = engine.export_state()
                        await self._notifications.mutate(
                            lambda data, state=saved: data.update(alert_state=state),
                            checked,
                        )
                        if events:
                            lines = ["📣 A股指数异动"]
                            for event in events:
                                quote = event.quote
                                pct = (
                                    f"（{quote.change_pct:+.2f}%）"
                                    if quote.change_pct is not None
                                    else ""
                                )
                                stamp = quote.quote_time.astimezone(BEIJING_TZ)
                                lines.append(
                                    f"{event.name} {event.description}｜{quote.latest_price:.2f}{pct}｜{stamp:%H:%M:%S} {quote.source}"
                                )
                            lines.append("基于定时采样，非逐笔监控。")
                            event_id = "alert:" + "|".join(
                                sorted(event.event_id for event in events)
                            )
                            session_end = checked.replace(
                                hour=window[1].hour,
                                minute=window[1].minute,
                                second=0,
                                microsecond=0,
                            )
                            await self._submit_notification(
                                event_id,
                                checked.date(),
                                targets,
                                "\n".join(lines),
                                counts_as_alert=True,
                                deadline=min(
                                    session_end, *(event.expires_at for event in events)
                                ),
                            )
                elif engine is not None:
                    engine = engine_config = None
            except (ValueError, OSError) as exc:
                logger.warning("Market alert cycle paused: %s", type(exc).__name__)
            except Exception:
                logger.exception("Market alert cycle failed")
            await asyncio.sleep(
                max(1, 60 - (asyncio.get_running_loop().time() - started))
            )

    async def _get_or_build_report(
        self,
        kind: str,
        trade_date: date,
        *,
        final_attempt: bool = False,
        deadline: datetime | None = None,
        recover_final_only: bool = False,
    ) -> MarketReport:
        """Share report inputs, and freeze only a complete or final attempt."""
        key = (kind, trade_date)
        path = self._reports_dir / f"{trade_date}-{kind}.json"
        now = datetime.now(BEIJING_TZ)
        last_slot, stop = REPORT_TIMES[kind]
        if kind == "midday" and now.date() == trade_date and now.time() < time(11, 30):
            raise ValueError("午间尚未结束，请在 11:30 后查询。")

        async def fetch():
            if kind == "midday":
                quotes = await fetch_market_overview(
                    max_quote_age_seconds=900, quote_window=MIDDAY_WINDOW
                )
                return build_midday_report(quotes, trade_date, datetime.now(BEIJING_TZ))
            batch = await self._get_history_client().fetch(trade_date)
            return build_close_report(batch, datetime.now(BEIJING_TZ))

        async with self._report_lock:
            if self._terminated:
                raise asyncio.CancelledError
            if key in self._report_finals:
                return self._report_finals[key]
            finalizing = self._report_finalizers.get(key)
            if finalizing is None:
                saved = None
                try:
                    data = await asyncio.to_thread(path.read_text, encoding="utf-8")
                    saved = report_from_dict(json.loads(data))
                    if (saved.kind, saved.trade_date) != key:
                        raise ValueError("Report archive date or kind mismatch")
                except FileNotFoundError:
                    pass
                except (OSError, ValueError):
                    logger.warning(
                        "Invalid market report archive [%s %s]", trade_date, kind
                    )
                    saved = None
                if self._terminated:
                    raise asyncio.CancelledError
                if saved is not None and saved.finalized:
                    return saved
                if recover_final_only:
                    raise ValueError("No finalized report to recover")
                now = datetime.now(BEIJING_TZ)
                after_midday = kind == "midday" and (
                    now.date() != trade_date or now.time() >= stop
                )
                expired = deadline is not None and now >= deadline
                if after_midday or expired:
                    if (
                        after_midday
                        and deadline is None
                        and key in self._report_workers
                    ):
                        if saved is not None:
                            return saved
                        raise ValueError("午间报告正在完成，请稍后查询。")
                    if saved is None:
                        raise ValueError("没有该时段的报告存档，采集窗口已结束。")
                    finalizing = asyncio.create_task(
                        self._freeze_report(saved, path, False)
                    )
                    self._report_finalizers[key] = finalizing
                else:
                    worker = self._report_workers.get(key)
                    if worker is None:
                        worker = asyncio.create_task(fetch())
                        self._report_workers[key] = worker
        if finalizing is not None:
            return await asyncio.shield(finalizing)

        try:
            candidate = await asyncio.shield(worker)
            async with self._report_lock:
                if self._terminated:
                    raise asyncio.CancelledError
                if key in self._report_finals:
                    return self._report_finals[key]
                finalizing = self._report_finalizers.get(key)
                if finalizing is None:
                    if not candidate.count and saved is not None and saved.count:
                        candidate = saved
                    clock = datetime.now(BEIJING_TZ)
                    should_freeze = (
                        candidate.count == len(NAMES)
                        or final_attempt
                        or (
                            deadline is None
                            and (trade_date < now.date() or now.time() >= last_slot[-1])
                        )
                    )
                    if not should_freeze:
                        await self._save_report_archive(path, candidate)
                        return candidate
                    may_analyze = (
                        bool(candidate.count)
                        and self.config.get("enable_llm_analysis", False)
                        and (deadline is None or clock < deadline)
                        and (kind != "midday" or clock.time() < stop)
                    )
                    finalizing = asyncio.create_task(
                        self._freeze_report(candidate, path, may_analyze)
                    )
                    self._report_finalizers[key] = finalizing
            return await asyncio.shield(finalizing)
        finally:
            if worker.done() and self._report_workers.get(key) is worker:
                self._report_workers.pop(key)

    async def _save_report_archive(self, path: Path, report: MarketReport) -> None:
        """Wait for an already-started atomic archive write during cancellation."""
        write = asyncio.create_task(
            asyncio.to_thread(write_json_atomic, path, report_to_dict(report))
        )
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            raise

    async def _freeze_report(
        self, report: MarketReport, path: Path, analyze: bool
    ) -> MarketReport:
        """Save one final report and optionally add a bounded interpretation."""
        key = (report.kind, report.trade_date)
        final = replace(report, finalized=True)
        try:
            if analyze:
                prompt = (
                    "请用约200字解读下方指数报告，只引用报告内可核对的事实。"
                    "注明样本限制，不预测后市，不推断资金流、新闻、量能或买卖点。\n\n"
                    + format_report(final)
                )
                try:
                    analysis = await asyncio.wait_for(
                        self._request_llm(prompt), timeout=LLM_TIMEOUT
                    )
                    if analysis:
                        final = replace(final, analysis=analysis)
                except Exception as exc:
                    logger.warning(
                        "Market report analysis unavailable: %s", type(exc).__name__
                    )
            if self._terminated:
                raise asyncio.CancelledError
            await self._save_report_archive(path, final)
            self._report_finals[key] = final
            oldest = (datetime.now(BEIJING_TZ).date() - timedelta(days=29)).isoformat()
            self._report_finals = {
                item: saved
                for item, saved in self._report_finals.items()
                if item[1].isoformat() >= oldest
            }
            for archived in self._reports_dir.glob("*.json"):
                if (
                    re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}-(midday|close)\.json", archived.name
                    )
                    and archived.name[:10] < oldest
                ):
                    try:
                        archived.unlink()
                    except OSError as exc:
                        logger.warning(
                            "Market report pruning failed: %s", type(exc).__name__
                        )
            return final
        finally:
            self._report_finalizers.pop(key, None)

    async def _run_report_job(self, kind: str, now: datetime) -> None:
        """Advance an eligible automatic report by at most one scheduled attempt."""
        if not self.config.get(
            f"enable_{kind}_report", False
        ) or not self._is_notification_day(now.date()):
            return
        times, stop = REPORT_TIMES[kind]
        due = [i for i, slot in enumerate(times) if slot <= now.time()]
        cutoff = now.replace(
            hour=stop.hour, minute=stop.minute, second=0, microsecond=0
        )
        delivery_end = cutoff + timedelta(seconds=120)
        targets = self._notification_targets()
        if not targets or not due or now >= delivery_end:
            return
        await self._notifications.load(now)
        key = f"{now.date()}|{kind}"
        job = self._notifications.snapshot()["report_jobs"].get(key)
        if job and job.get("finished"):
            return
        recovering = key not in self._seen_report_jobs
        self._seen_report_jobs = {
            item
            for item in self._seen_report_jobs
            if item.startswith(now.date().isoformat())
        }
        self._seen_report_jobs.add(key)
        attempts = job["attempts"] if job else 0
        if job and (job.get("ready") or now >= cutoff or attempts >= 3):
            targets = tuple(job["targets"])
            data_deadline = now
            final_attempt = True
        elif now < cutoff and (not job or due[-1] > job["slot"] or recovering):
            targets = tuple(job["targets"]) if job else targets
            job = {"targets": list(targets), "slot": due[-1], "attempts": attempts + 1}
            await self._notifications.mutate(
                lambda data: data["report_jobs"].update({key: job}), now
            )
            data_deadline = cutoff
            final_attempt = due[-1] == len(times) - 1 or attempts == 2
        else:
            return
        try:
            report = await self._get_or_build_report(
                kind,
                now.date(),
                final_attempt=final_attempt,
                deadline=data_deadline,
                recover_final_only=now >= cutoff,
            )
        except ValueError:
            if data_deadline != now or now >= cutoff:
                return
            report = MarketReport(
                kind,
                now.date(),
                now,
                True,
                (),
                {},
                {code: "数据不可用" for code in NAMES},
            )
        self._last_report = (
            f"{REPORT_LABELS[kind]} {now.date()} {report.count}/{len(NAMES)}"
        )
        if not report.finalized or self._terminated:
            return
        job["ready"] = True
        await self._notifications.mutate(
            lambda data: data["report_jobs"].update({key: job}), now
        )
        image = None
        if (
            kind == "close"
            and report.histories
            and self.config.get("enable_image_output", False)
        ):
            try:
                chart = build_chart_data(
                    HistoryBatch(report.trade_date, report.histories, report.missing),
                    calendar=self._get_calendar(),
                )
                async with self._chart_limit:
                    image = await asyncio.to_thread(render_trend_chart, chart)
            except Exception as exc:
                logger.warning("Close report chart unavailable: %s", type(exc).__name__)
        await self._submit_notification(
            "report:" + kind,
            now.date(),
            targets,
            format_report(report),
            image,
            deadline=delivery_end,
        )
        job["finished"] = True
        await self._notifications.mutate(
            lambda data: data["report_jobs"].update({key: job}), now
        )

    async def _report_loop(self) -> None:
        """Process report slots independently of the intraday sampling loop."""
        await asyncio.sleep(15)
        while not self._terminated:
            for kind in REPORT_TIMES:
                try:
                    await self._run_report_job(kind, datetime.now(BEIJING_TZ))
                except (OSError, ValueError) as exc:
                    logger.warning(
                        "Market report job paused [%s]: %s", kind, type(exc).__name__
                    )
                except Exception:
                    logger.exception("Market report job failed [%s]", kind)
            await asyncio.sleep(15)

    @filter.command("午间小结")
    async def cmd_midday_report(self, event: AstrMessageEvent):
        """Return today's morning report or the saved cutoff snapshot."""
        self._learn_group_mapping(event)
        try:
            day = datetime.now(BEIJING_TZ).date()
            if not self._get_calendar().is_session(day):
                yield event.plain_result("今日休市，无午间报告。")
                return
            report = await self._get_or_build_report("midday", day)
            if not self._terminated:
                yield event.plain_result(format_report(report))
        except ValueError as exc:
            yield event.plain_result(f"午间小结不可用：{exc}")
        except Exception as exc:
            logger.warning("Midday report unavailable: %s", type(exc).__name__)
            yield event.plain_result("午间小结暂不可用。")

    @filter.command("收盘复盘")
    async def cmd_close_report(self, event: AstrMessageEvent):
        """Return a report for the latest required confirmed close."""
        self._learn_group_mapping(event)
        try:
            baseline = resolve_baseline(datetime.now(BEIJING_TZ), self._get_calendar())
            if baseline is None:
                yield event.plain_result("交易日历未覆盖，无法确定收盘基准。")
                return
            report = await self._get_or_build_report("close", baseline)
            if not self._terminated:
                yield event.plain_result(format_report(report))
        except Exception as exc:
            logger.warning("Close report unavailable: %s", type(exc).__name__)
            yield event.plain_result("收盘复盘暂不可用。")

    async def _trend_result(
        self, event: AstrMessageEvent, sessions: int, code: str | None
    ):
        """Prepare a chart response and fall back to its equivalent facts."""
        self._learn_group_mapping(event)
        try:
            calendar = self._get_calendar()
            baseline = resolve_baseline(datetime.now(BEIJING_TZ), calendar)
            if baseline is None:
                return event.plain_result("交易日历未覆盖，无法确定收盘基准。")
            batch = await self._get_history_client().fetch(baseline)
            data = build_chart_data(
                batch, sessions=sessions, code=code, calendar=calendar
            )
        except ValueError as exc:
            return event.plain_result(f"走势不可用：{exc}")
        except Exception as exc:
            logger.warning("Trend chart data unavailable: %s", type(exc).__name__)
            return event.plain_result("走势数据暂不可用。")
        summary = format_chart_summary(data)
        try:
            async with self._chart_limit:
                image = await asyncio.to_thread(render_trend_chart, data)
            return event.chain_result(self._make_chain("", image).chain)
        except Exception as exc:
            logger.warning("Trend chart rendering unavailable: %s", type(exc).__name__)
            return event.plain_result(summary)

    @filter.command("大盘走势")
    async def cmd_market_trend(self, event: AstrMessageEvent, sessions: int = 20):
        """Show the five-index close comparison for 20 or 60 sessions."""
        if sessions not in (20, 60):
            yield event.plain_result("用法：/大盘走势 [20或60]")
            return
        result = await self._trend_result(event, sessions, None)
        if not self._terminated:
            yield result

    @filter.command("指数走势")
    async def cmd_index_trend(
        self, event: AstrMessageEvent, index: str = "", sessions: int = 20
    ):
        """Show the selected index close and moving averages."""
        code = INDEX_ALIASES.get(index.strip())
        if code is None or sessions not in (20, 60):
            yield event.plain_result(
                "用法：/指数走势 <上证、深证、创业板、科创50、沪深300或指数代码> [20或60]"
            )
            return
        result = await self._trend_result(event, sessions, code)
        if not self._terminated:
            yield result

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
        if hasattr(self, "_notifications"):
            now = datetime.now(BEIJING_TZ)
            targets = self._notification_targets()
            try:
                threshold_values = parse_alert_thresholds(
                    self.config.get("alert_thresholds_pct", [1.0, 2.0, 3.0])
                )
                thresholds = "、".join(f"{value:g}%" for value in threshold_values)
            except ValueError:
                thresholds = "阈值配置无效，涨跌幅规则已停用"
            try:
                active_day = "交易日" if self._is_notification_day(now.date()) else "今日停发"
            except (OSError, ValueError):
                active_day = "日历不可用或当年未覆盖，新增主动通知暂停"
            ledger = self._notifications.snapshot()
            remaining = [
                6 - ledger["daily_counts"].get(f"{now.date()}|{target}", 0)
                for target in targets
            ]
            status = [
                f"新通知日历：{active_day}",
                f"异动提醒：{'开启' if self.config.get('enable_index_alerts') else '关闭'}｜{thresholds}",
                f"20 日区间突破：{'开启' if self.config.get('enable_range_breakout_alerts') else '关闭'}",
                f"可投递群：{len(targets)}｜未绑定或冲突：{max(0, len(groups) - len(targets))}",
                f"最近异动采样：{str(self._alert_count) + '/5 指数有效' if self._alert_count is not None else '尚未采样'}",
                f"最近报告：{self._last_report or '尚未生成'}",
            ]
            if remaining:
                status.append("各目标剩余异动条数：" + "、".join(map(str, remaining)))
            for kind, (slots, _) in REPORT_TIMES.items():
                enabled_kind = self.config.get(f"enable_{kind}_report", False)
                when = next((slot.strftime("%H:%M") for slot in slots if slot > now.time()), None)
                if ledger["report_jobs"].get(f"{now.date()}|{kind}", {}).get("finished"):
                    when = None
                detail = f"｜今日下一时点 {when}" if enabled_kind and when and active_day == "交易日" else ""
                status.append(f"{REPORT_LABELS[kind]}：{'开启' if enabled_kind else '关闭'}{detail}")
            if self._notifications.path.exists() and not self._notifications.available:
                status.append("通知状态尚未加载或文件异常")
            text += "\n\n" + "\n".join(status)
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
