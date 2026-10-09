"""Trade analytics for the owner dashboard: profit, stop losses, and when they happen.

Works on any list of signal rows (live database or the backtest database), so the live
numbers can be compared one-to-one with the backtest as signals accumulate.
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..strategies import SETUP_LABELS

CLOSED = {"sl", "be", "tp2", "expired"}

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
OUTCOME_LABELS = {
    "sl": "Stop loss hit (full loss)",
    "be": "TP1 hit, then stopped at entry",
    "tp2": "TP1 and TP2 hit",
    "expired_win": "Closed at 48h in profit",
    "expired_loss": "Closed at 48h at a loss",
}
OUTCOME_KIND = {"sl": "bad", "be": "good", "tp2": "good", "expired_win": "good", "expired_loss": "bad"}


@dataclass
class Group:
    label: str
    trades: int
    win_rate: float
    sl_rate: float
    net_r: float
    avg_r: float


@dataclass
class Analytics:
    trades: int = 0
    open_count: int = 0
    wins: int = 0
    stops: int = 0
    win_rate: float = 0.0
    sl_rate: float = 0.0
    tp1_rate: float = 0.0
    tp2_rate: float = 0.0
    net_r: float = 0.0
    avg_r: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float | None = None
    max_dd: float = 0.0
    longest_losing: int = 0
    longest_winning: int = 0
    per_week: float = 0.0
    months_profitable: int = 0
    months_total: int = 0
    avg_hours_to_sl: float | None = None
    avg_hours_to_win: float | None = None
    first: pd.Timestamp | None = None
    last: pd.Timestamp | None = None
    outcomes: list[dict] = field(default_factory=list)
    monthly: list[dict] = field(default_factory=list)
    by_side: list[Group] = field(default_factory=list)
    by_setup: list[Group] = field(default_factory=list)
    by_weekday: list[Group] = field(default_factory=list)
    by_hour: list[Group] = field(default_factory=list)
    equity: list[tuple[str, float]] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    worst: dict | None = None      # Monte Carlo drawdown / losing-streak estimate
    tz_name: str = "IST"
    risk_usd: float | None = None  # $ per 1R at your lot size, for the money figures


def to_frame(rows) -> pd.DataFrame:
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True, format="ISO8601")
    df["closed_at"] = pd.to_datetime(df["closed_at"], utc=True, format="ISO8601", errors="coerce")
    return df


def filter_frame(df: pd.DataFrame, period: str, side: str, end: pd.Timestamp | None = None) -> pd.DataFrame:
    """Keep the last N days before `end` (today for live data; the last signal for a finished backtest)."""
    if df.empty:
        return df
    if period in ("30d", "90d", "365d"):
        end = end if end is not None else df["created_at"].max()
        df = df[df["created_at"] >= end - pd.Timedelta(days=int(period[:-1]))]
    if side in ("BUY", "SELL"):
        df = df[df["direction"] == side]
    return df


def _outcome_key(row) -> str:
    if row["status"] == "expired":
        return "expired_win" if row["result_r"] > 0 else "expired_loss"
    return row["status"]


def _group(label: str, g: pd.DataFrame) -> Group:
    r = g["result_r"]
    return Group(label, len(g), round((r > 0).mean() * 100, 1), round((g["status"] == "sl").mean() * 100, 1),
                 round(r.sum(), 2), round(r.mean(), 3))


def _streak(mask: pd.Series) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def worst_case(r: pd.Series, runs: int = 2000, seed: int = 7) -> dict:
    """Shuffle the same trades into thousands of random orders (resampling with replacement) and see how
    deep the drawdown and losing streak get. The 95th percentile is a fair "bad but realistic" case."""
    rng = np.random.default_rng(seed)
    values = r.to_numpy()
    sims = rng.choice(values, size=(runs, len(values)), replace=True)
    equity = sims.cumsum(axis=1)
    peak = np.maximum.accumulate(np.concatenate([np.zeros((runs, 1)), equity], axis=1), axis=1)[:, 1:]
    dd = (peak - equity).max(axis=1)
    losing = sims <= 0
    streaks = np.zeros(runs, dtype=int)
    cur = np.zeros(runs, dtype=int)
    for col in losing.T:
        cur = np.where(col, cur + 1, 0)
        streaks = np.maximum(streaks, cur)
    return {"dd_median": round(float(np.median(dd)), 1), "dd_95": round(float(np.percentile(dd, 95)), 1),
            "streak_95": int(np.percentile(streaks, 95)), "runs": runs}


def compute(df: pd.DataFrame, offset_hours: float = 5.5, tz_name: str = "IST", risk_usd: float | None = None) -> Analytics:
    """All numbers for the analytics page. Times are grouped in the display time zone (offset_hours)."""
    a = Analytics()
    a.tz_name = tz_name
    a.risk_usd = risk_usd
    if df.empty:
        return a
    a.open_count = int((~df["status"].isin(CLOSED)).sum())
    c = df[df["status"].isin(CLOSED) & df["result_r"].notna()].sort_values("closed_at").copy()
    if c.empty:
        return a
    r = c["result_r"]
    n = len(c)
    a.trades = n
    a.wins = int((r > 0).sum())
    a.stops = int((c["status"] == "sl").sum())
    a.win_rate = round(a.wins / n * 100, 1)
    a.sl_rate = round(a.stops / n * 100, 1)
    reached_tp1 = c["status"].isin(["be", "tp2"])
    if "tp1_hit_at" in c:
        reached_tp1 |= (c["status"] == "expired") & c["tp1_hit_at"].notna()
    a.tp1_rate = round(reached_tp1.mean() * 100, 1)
    a.tp2_rate = round((c["status"] == "tp2").mean() * 100, 1)
    a.net_r = round(r.sum(), 2)
    a.avg_r = round(r.mean(), 3)
    a.avg_win = round(r[r > 0].mean(), 2) if a.wins else 0.0
    a.avg_loss = round(r[r <= 0].mean(), 2) if n - a.wins else 0.0
    gross_loss = -r[r < 0].sum()
    a.profit_factor = round(r[r > 0].sum() / gross_loss, 2) if gross_loss > 0 else None
    eq = r.cumsum()
    a.max_dd = round(float((eq.cummax().clip(lower=0) - eq).max()), 2)
    a.longest_losing = _streak(r <= 0)
    a.longest_winning = _streak(r > 0)
    a.first, a.last = c["created_at"].min(), c["created_at"].max()
    weeks = max((a.last - a.first).days / 7, 1)
    a.per_week = round(n / weeks, 1)

    held = (c["closed_at"] - c["created_at"]).dt.total_seconds() / 3600
    if a.stops:
        a.avg_hours_to_sl = round(held[c["status"] == "sl"].mean(), 1)
    if a.wins:
        a.avg_hours_to_win = round(held[r > 0].mean(), 1)

    keys = c.apply(_outcome_key, axis=1)
    for k in OUTCOME_LABELS:
        cnt = int((keys == k).sum())
        if cnt:
            a.outcomes.append({"key": k, "label": OUTCOME_LABELS[k], "kind": OUTCOME_KIND[k], "count": cnt,
                               "pct": round(cnt / n * 100, 1), "net_r": round(r[keys == k].sum(), 2)})

    month = c["created_at"].dt.strftime("%Y-%m")
    for m, g in c.groupby(month):
        a.monthly.append({"month": m, "trades": len(g), "net_r": round(g["result_r"].sum(), 2),
                          "win_rate": round((g["result_r"] > 0).mean() * 100, 1),
                          "stops": int((g["status"] == "sl").sum())})
    a.months_total = len(a.monthly)
    a.months_profitable = sum(1 for m in a.monthly if m["net_r"] > 0)

    a.by_side = [_group(s, g) for s, g in c.groupby("direction")]
    if "strategy" in c and c["strategy"].nunique() > 1:
        a.by_setup = [_group(SETUP_LABELS.get(s, s), g) for s, g in c.groupby("strategy")]
    # Group by the time you see on the signal (display time zone), matching the table below.
    offset = pd.Timedelta(hours=offset_hours)
    local = c["created_at"] + offset
    wd = local.dt.weekday
    a.by_weekday = [_group(WEEKDAYS[d], c[wd == d]) for d in sorted(wd.unique())]
    hour = local.dt.hour
    a.by_hour = [_group(f"{(pd.Timestamp('2000-01-03') + pd.Timedelta(hours=h)):%I:00 %p} {tz_name}", c[hour == h])
                 for h in sorted(hour.unique())]
    if n >= 20:
        a.worst = worst_case(r)

    a.equity = [(t.isoformat(), round(v, 2)) for t, v in zip(c["closed_at"], eq)]
    for _, row in c.sort_values("created_at", ascending=False).iterrows():
        a.rows.append({
            "id": row["id"], "sent": row["created_at"], "sent_local": row["created_at"] + offset,
            "direction": row["direction"], "entry": row["entry"],
            "setup": SETUP_LABELS.get(row.get("strategy"), row.get("strategy") or ""), "sl": row["sl"], "tp1": row["tp1"], "tp2": row["tp2"],
            "outcome": OUTCOME_LABELS[_outcome_key(row)], "kind": OUTCOME_KIND[_outcome_key(row)],
            "result_r": row["result_r"], "hours": round((row["closed_at"] - row["created_at"]).total_seconds() / 3600, 1),
            "is_sl": row["status"] == "sl",
        })
    return a
