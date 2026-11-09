"""Persist notification attempts before contacting the message platform."""

import asyncio
import json
import os
import tempfile
from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path

_STATES = {"attempting", "submitted", "unknown", "not_submitted"}
_SECTIONS = ("alert_state", "deliveries", "daily_counts", "report_jobs")


def write_json_atomic(path: Path, value: dict) -> None:
    """Replace a JSON file only after the new content was flushed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class NotificationState:
    """Serialize changes and reserve per-target attempts across restarts."""

    def __init__(self, path: Path):
        self.path = path
        self._data = {"version": 1, **{key: {} for key in _SECTIONS}}
        self._lock = asyncio.Lock()
        self.available = False

    async def load(self, now: datetime) -> None:
        """Load the ledger and mark interrupted sends as having unknown results."""
        async with self._lock:
            if self.available:
                return
            try:
                raw = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
            except FileNotFoundError:
                self.available = True
                return
            data = json.loads(raw)
            if (
                not isinstance(data, dict)
                or data.get("version") != 1
                or any(not isinstance(data.get(name), dict) for name in _SECTIONS)
            ):
                raise ValueError("Invalid notification ledger")
            try:
                for key, row in data["deliveries"].items():
                    date.fromisoformat(key.split("|", 1)[0])
                    if (
                        row["status"] not in _STATES
                        or type(row["attempts"]) is not int
                        or not 1 <= row["attempts"] <= 2
                    ):
                        raise ValueError("Invalid delivery record")
                    if row["status"] == "attempting":
                        row.update(status="unknown", updated_at=now.isoformat())
                for key, count in data["daily_counts"].items():
                    date.fromisoformat(key.split("|", 1)[0])
                    if type(count) is not int or not 0 <= count <= 6:
                        raise ValueError("Invalid alert count")
                for key, job in data["report_jobs"].items():
                    date.fromisoformat(key.split("|", 1)[0])
                    if (
                        type(job["attempts"]) is not int
                        or not 0 <= job["attempts"] <= 3
                        or not isinstance(job["targets"], list)
                    ):
                        raise ValueError("Invalid report job")
                alerts = data["alert_state"]
                if alerts:
                    alert_day = date.fromisoformat(alerts["date"])
                    if (
                        not isinstance(alerts["observed"], list)
                        or not all(isinstance(key, str) for key in alerts["observed"])
                        or not isinstance(alerts["cooldowns"], dict)
                    ):
                        raise ValueError("Invalid alert records")
                    for value in alerts["cooldowns"].values():
                        stamp = datetime.fromisoformat(value)
                        if stamp.utcoffset() is None or stamp.date() != alert_day:
                            raise ValueError("Invalid alert cooldown")
            except (KeyError, TypeError, AttributeError) as exc:
                raise ValueError("Invalid notification records") from exc
            self._data = data
            self.available = True

    def snapshot(self) -> dict:
        """Return an isolated copy of the current ledger."""
        return deepcopy(self._data)

    async def mutate(self, change: Callable[[dict], object], now: datetime):
        """Apply a serialized and durable mutation.

        Args:
            change: Synchronous mutation of a private copy of the ledger.
            now: Aware clock used to prune expired records.

        Returns:
            The result returned by change.

        Raises:
            ValueError: The ledger is unavailable.
            OSError: The updated state cannot be saved.
        """
        async with self._lock:
            if not self.available:
                raise ValueError("Notification ledger unavailable")
            updated = deepcopy(self._data)
            oldest = (now.date() - timedelta(days=6)).isoformat()
            for name in ("deliveries", "daily_counts", "report_jobs"):
                updated[name] = {
                    key: value
                    for key, value in updated[name].items()
                    if key.split("|", 1)[0] >= oldest
                }
            result = change(updated)
            if updated != self._data:
                write = asyncio.create_task(
                    asyncio.to_thread(write_json_atomic, self.path, updated)
                )
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    await write
                    self._data = updated
                    raise
                self._data = updated
            return result

    async def reserve(
        self, notification_id: str, target: str, now: datetime, *, alert: bool
    ) -> bool:
        """Reserve an attempt and its quota before making an external call."""
        key = f"{now.date()}|{target}|{notification_id}"
        count_key = f"{now.date()}|{target}"

        def change(data):
            previous = data["deliveries"].get(key)
            if previous is not None and (
                previous["status"] != "not_submitted" or previous["attempts"] >= 2
            ):
                return False
            count = data["daily_counts"].get(count_key, 0)
            if alert and count >= 6:
                return False
            data["deliveries"][key] = {
                "status": "attempting",
                "attempts": 1 if previous is None else previous["attempts"] + 1,
                "attempted_at": now.isoformat(),
                "updated_at": now.isoformat(),
            }
            if alert:
                data["daily_counts"][count_key] = count + 1
            return True

        return await self.mutate(change, now)

    async def finish(
        self,
        notification_id: str,
        target: str,
        started: datetime,
        status: str,
        *,
        alert: bool,
    ) -> None:
        """Record the submission outcome and release quota on definite failure."""
        key = f"{started.date()}|{target}|{notification_id}"

        def change(data):
            row = data["deliveries"][key]
            row.update(
                status=status, updated_at=datetime.now(started.tzinfo).isoformat()
            )
            if alert and status == "not_submitted":
                count_key = f"{started.date()}|{target}"
                data["daily_counts"][count_key] -= 1

        await self.mutate(change, started)
