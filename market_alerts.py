"""Evaluate intraday index movements without network or delivery side effects."""

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .market_api import BEIJING_TZ, IndexData, MarketOverview, QuoteWindow
from .market_history import HistoryBatch

ALERT_AGE = 120
COOLDOWN = timedelta(minutes=5)
SESSIONS = ((time(9, 30), time(11, 30)), (time(13), time(15)))


def market_session(now: datetime) -> QuoteWindow | None:
    """Return the Beijing trading window containing an aware timestamp."""
    if now.utcoffset() is None:
        return None
    clock = now.astimezone(BEIJING_TZ).time()
    return next(
        (window for window in SESSIONS if window[0] <= clock <= window[1]), None
    )


def parse_alert_thresholds(raw: object) -> tuple[float, ...]:
    """Validate a nonempty list of positive percentage thresholds."""
    if (
        not isinstance(raw, list)
        or not raw
        or any(type(item) not in (int, float, str) for item in raw)
    ):
        raise ValueError(
            "Alert thresholds must be a nonempty list of numbers from 0.1 to 20"
        )
    values = {float(item) for item in raw}
    if any(not 0.1 <= value <= 20 for value in values):
        raise ValueError("Alert thresholds must be from 0.1 to 20")
    return tuple(sorted(values))


@dataclass(frozen=True)
class AlertEvent:
    """One index and direction, with its confirmed reasons for notification."""

    event_id: str
    trade_date: date
    code: str
    name: str
    direction: str
    rule_keys: tuple[str, ...]
    quote: IndexData
    created_at: datetime
    expires_at: datetime
    description: str


@dataclass(frozen=True)
class _Rule:
    key: str
    kind: str
    direction: int
    level: float
    label: str

    def is_outside(self, quote: IndexData) -> bool:
        """Test the correct field and boundary for this rule."""
        if self.kind == "range":
            return self.direction * (quote.latest_price - self.level) > 0
        pct = quote.change_pct
        return (
            pct is not None
            and math.isfinite(pct)
            and self.direction * (pct - self.level) >= 0
        )


class AlertEngine:
    """Keep one intraday baseline per index and consume each rule once."""

    def __init__(
        self, thresholds: tuple[float, ...], *, enable_range_breakouts: bool = False
    ):
        self.thresholds = thresholds
        self.enable_range_breakouts = enable_range_breakouts
        self._day = None
        self._last_evaluation = None
        self._observed: set[str] = set()
        self._cooldowns: dict[str, datetime] = {}
        self._previous: dict[str, IndexData] = {}
        self._pending: dict[
            tuple[str, int, str], tuple[_Rule, tuple[str, ...], datetime]
        ] = {}

    def evaluate(
        self, overview: MarketOverview, histories: HistoryBatch | None, now: datetime
    ) -> tuple[AlertEvent, ...]:
        """Return still-valid events after applying baseline and cooldown rules.

        Args:
            overview: A strictly sourced snapshot.
            histories: Optional verified histories through the prior session.
            now: Aware evaluation time.

        Returns:
            A tuple with at most one notification per index and direction.
        """
        now = now.astimezone(BEIJING_TZ)
        day = now.date()
        if self._last_evaluation is not None and now < self._last_evaluation:
            self._previous.clear()
            self._pending.clear()
        self._last_evaluation = now
        if self._day != day:
            self._day = day
            self._observed.clear()
            self._cooldowns.clear()
            self._previous.clear()
            self._pending.clear()
        window = market_session(now)
        if window is None:
            self._previous.clear()
            self._pending.clear()
            return ()
        events = []
        for quote in overview.indices:
            stamp = quote.quote_time
            if (
                stamp is None
                or stamp.utcoffset() is None
                or stamp.astimezone(BEIJING_TZ).date() != day
                or market_session(stamp) != window
                or not -5 <= (now - stamp).total_seconds() <= ALERT_AGE
            ):
                continue
            previous = self._previous.get(quote.code)
            if (
                previous is not None
                and previous.source == quote.source
                and stamp == previous.quote_time
            ):
                continue
            self._previous[quote.code] = quote
            continuous = (
                previous is not None
                and previous.source == quote.source
                and market_session(previous.quote_time) == window
                and 0 < (stamp - previous.quote_time).total_seconds() <= ALERT_AGE
            )
            if not continuous:
                for key in tuple(self._pending):
                    if key[0] == quote.code:
                        del self._pending[key]
            rules = [
                _Rule(
                    f"{quote.code}:pct:{direction * level:+g}",
                    "pct",
                    direction,
                    direction * level,
                    f"{'上穿' if direction == 1 else '下穿'} {direction * level:+g}%",
                )
                for direction in (1, -1)
                for level in self.thresholds
            ]
            if self.enable_range_breakouts and histories is not None:
                item = histories.histories.get(quote.code)
                if (
                    item is not None
                    and len(item.bars) >= 20
                    and item.bars[-1].trade_date == histories.expected_date < day
                ):
                    bars = item.bars[-20:]
                    for direction, level, label in (
                        (1, max(bar.high for bar in bars), "突破前20日最高价"),
                        (-1, min(bar.low for bar in bars), "跌破前20日最低价"),
                    ):
                        rules.append(
                            _Rule(
                                f"{quote.code}:range:{direction}:{histories.expected_date}",
                                "range",
                                direction,
                                level,
                                f"{label} {level:.2f}",
                            )
                        )
            for rule in rules:
                if rule.key in self._observed or not rule.is_outside(quote):
                    continue
                if (
                    continuous
                    and rule.kind == "pct"
                    and (
                        previous.change_pct is None
                        or not math.isfinite(previous.change_pct)
                    )
                ):
                    self._observed.add(rule.key)
                    continue
                if continuous and rule.is_outside(previous):
                    continue
                self._observed.add(rule.key)
                if continuous:
                    key = (quote.code, rule.direction, rule.kind)
                    old = self._pending.get(key)
                    consumed = old[1] if old is not None else ()
                    self._pending[key] = (rule, (*consumed, rule.key), now + COOLDOWN)
            for direction in (1, -1):
                ready = []
                for kind in ("pct", "range"):
                    key = (quote.code, direction, kind)
                    candidate = self._pending.get(key)
                    if candidate is None:
                        continue
                    rule, _, expiry = candidate
                    if expiry <= now or not rule.is_outside(quote):
                        del self._pending[key]
                    else:
                        ready.append(candidate)
                cooldown_key = f"{quote.code}:{direction}"
                if not ready or self._cooldowns.get(cooldown_key, now) > now:
                    continue
                keys = tuple(
                    sorted(key for _, consumed, _ in ready for key in consumed)
                )
                for rule, _, _ in ready:
                    del self._pending[(quote.code, direction, rule.kind)]
                self._cooldowns[cooldown_key] = now + COOLDOWN
                events.append(
                    AlertEvent(
                        "|".join(keys),
                        day,
                        quote.code,
                        quote.name,
                        "up" if direction == 1 else "down",
                        keys,
                        quote,
                        now,
                        min(
                            stamp + timedelta(seconds=ALERT_AGE),
                            *(item[2] for item in ready),
                        ),
                        "；".join(rule.label for rule, _, _ in ready),
                    )
                )
        return tuple(events)

    def export_state(self) -> dict:
        """Export consumed rules and cooldowns without old sampling baselines."""
        return {
            "date": self._day.isoformat() if self._day else None,
            "observed": sorted(self._observed),
            "cooldowns": {
                key: value.isoformat() for key, value in self._cooldowns.items()
            },
        }

    def restore_state(self, data: dict) -> None:
        """Restore deduplication state while resetting sampling continuity."""
        day = date.fromisoformat(data["date"])
        cooldowns = {
            key: datetime.fromisoformat(value)
            for key, value in data["cooldowns"].items()
        }
        if any(
            value.utcoffset() is None or value.date() != day
            for value in cooldowns.values()
        ):
            raise ValueError("Invalid alert cooldown")
        self._day = day
        self._observed = set(data["observed"])
        self._cooldowns = cooldowns
        self._previous.clear()
        self._pending.clear()
