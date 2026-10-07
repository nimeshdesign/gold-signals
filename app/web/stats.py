"""Performance statistics for the public track record."""
from dataclasses import dataclass


@dataclass
class Stats:
    closed: int
    open: int
    wins: int
    losses: int
    breakevens: int
    win_rate: float | None
    total_r: float
    avg_r: float | None
    profit_factor: float | None
    max_drawdown_r: float
    equity: list[tuple[str, float]]  # (closed_at, cumulative R)


def compute_stats(rows) -> Stats:
    closed = sorted((r for r in rows if r["result_r"] is not None and r["closed_at"]), key=lambda r: r["closed_at"])
    open_count = sum(1 for r in rows if r["status"] in ("open", "tp1"))
    results = [r["result_r"] for r in closed]
    wins = [x for x in results if x > 0]
    losses = [x for x in results if x < 0]
    gross_loss = -sum(losses)

    equity, cum, peak, max_dd = [], 0.0, 0.0, 0.0
    for r in closed:
        cum += r["result_r"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        equity.append((r["closed_at"], round(cum, 2)))

    return Stats(
        closed=len(closed),
        open=open_count,
        wins=len(wins),
        losses=len(losses),
        breakevens=len(results) - len(wins) - len(losses),
        win_rate=(len(wins) / len(results) * 100) if results else None,
        total_r=round(sum(results), 2),
        avg_r=round(sum(results) / len(results), 2) if results else None,
        profit_factor=round(sum(wins) / gross_loss, 2) if gross_loss > 0 else None,
        max_drawdown_r=round(max_dd, 2),
        equity=equity,
    )
