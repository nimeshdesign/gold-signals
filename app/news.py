"""High-impact USD news filter, using the free ForexFactory weekly calendar feed."""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_SECONDS = 3600


@dataclass
class NewsEvent:
    title: str
    time: datetime
    country: str
    impact: str


def parse_events(raw: list[dict], countries=("USD",), impacts=("High",)) -> list[NewsEvent]:
    events = []
    for item in raw:
        if item.get("country") not in countries or item.get("impact") not in impacts:
            continue
        try:
            when = datetime.fromisoformat(item["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        events.append(NewsEvent(item.get("title", "?"), when, item["country"], item["impact"]))
    return events


class NewsFilter:
    def __init__(self, before_min: int = 30, after_min: int = 30, fail_closed: bool = False,
                 session: requests.Session | None = None):
        self.before = timedelta(minutes=before_min)
        self.after = timedelta(minutes=after_min)
        self.fail_closed = fail_closed
        self.http = session or requests.Session()
        self._events: list[NewsEvent] | None = None
        self._fetched_at = 0.0

    def _refresh(self) -> None:
        if self._events is not None and time.time() - self._fetched_at < CACHE_SECONDS:
            return
        try:
            resp = self.http.get(CALENDAR_URL, timeout=20, headers={"User-Agent": "gold-signals/1.0"})
            resp.raise_for_status()
            self._events = parse_events(resp.json())
            self._fetched_at = time.time()
            log.info("Loaded %d high-impact USD events", len(self._events))
        except (requests.RequestException, ValueError) as exc:
            # Keep using the last good copy; retry in 5 minutes rather than an hour.
            log.warning("News calendar fetch failed: %s", exc)
            self._fetched_at = time.time() - CACHE_SECONDS + 300

    def events_between(self, start: datetime, end: datetime) -> list[NewsEvent]:
        """High-impact events in [start, end), e.g. today's, for the daily plan message."""
        self._refresh()
        return sorted((e for e in self._events or [] if start <= e.time < end), key=lambda e: e.time)

    def blocking_event(self, now: datetime | None = None) -> NewsEvent | None:
        """The event that blocks trading right now, or None if signals are allowed."""
        now = now or datetime.now(timezone.utc)
        self._refresh()
        if self._events is None:
            if self.fail_closed:
                return NewsEvent("News calendar unavailable", now, "USD", "High")
            return None
        for ev in self._events:
            if ev.time - self.before <= now <= ev.time + self.after:
                return ev
        return None
