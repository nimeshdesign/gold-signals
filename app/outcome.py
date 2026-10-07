"""Trade management rules shared by the live tracker and the backtester.

Position model published to subscribers:
  * Half the position closes at TP1 and the stop moves to entry (breakeven).
  * The other half runs to TP2 or gets stopped at breakeven.

So a signal's result in R is one of:
  SL before TP1        -> -1.00R
  TP1, then breakeven  -> +TP1/2 (e.g. +0.75R)
  TP1, then TP2        -> +TP1/2 + TP2/2 (e.g. +2.25R)
  expired              -> marked to market at the expiry price

When one candle touches both the stop and a target, we assume the stop was hit first.
That understates results slightly, which is the honest direction to be wrong in.
"""
from dataclasses import dataclass, field

OPEN = "open"  # waiting for TP1 or SL
TP1 = "tp1"  # TP1 hit, half closed, stop at entry
# Final states
STOPPED = "sl"
BREAKEVEN = "be"
TARGET = "tp2"
EXPIRED = "expired"
FINAL = {STOPPED, BREAKEVEN, TARGET, EXPIRED}


@dataclass
class TradeState:
    direction: str  # "BUY" or "SELL"
    entry: float
    sl: float
    tp1: float
    tp2: float
    status: str = OPEN
    result_r: float | None = None
    events: list[str] = field(default_factory=list)

    @property
    def sign(self) -> int:
        return 1 if self.direction == "BUY" else -1

    @property
    def risk(self) -> float:
        return abs(self.entry - self.sl)

    def r_at(self, price: float) -> float:
        return self.sign * (price - self.entry) / self.risk

    @property
    def is_closed(self) -> bool:
        return self.status in FINAL


def step(state: TradeState, high: float, low: float) -> list[str]:
    """Advance the trade through one candle. Returns the new events (also appended to state.events)."""
    if state.is_closed:
        return []
    new: list[str] = []
    buy = state.sign == 1
    adverse = low if buy else high
    favorable = high if buy else low

    def reached(price_extreme: float, level: float, toward_profit: bool) -> bool:
        if toward_profit:
            return price_extreme >= level if buy else price_extreme <= level
        return price_extreme <= level if buy else price_extreme >= level

    if state.status == OPEN:
        if reached(adverse, state.sl, toward_profit=False):
            state.status = STOPPED
            state.result_r = -1.0
            new.append(STOPPED)
        elif reached(favorable, state.tp1, toward_profit=True):
            state.status = TP1
            new.append(TP1)
            # Within the same candle we cannot know the order of later moves; stop here
            # and let the next candle decide between breakeven and TP2.
    elif state.status == TP1:
        half_tp1 = 0.5 * state.r_at(state.tp1)
        if reached(adverse, state.entry, toward_profit=False):
            state.status = BREAKEVEN
            state.result_r = round(half_tp1, 2)
            new.append(BREAKEVEN)
        elif reached(favorable, state.tp2, toward_profit=True):
            state.status = TARGET
            state.result_r = round(half_tp1 + 0.5 * state.r_at(state.tp2), 2)
            new.append(TARGET)

    state.events.extend(new)
    return new


def expire(state: TradeState, price: float) -> None:
    """Close the trade at `price` (time limit reached)."""
    if state.is_closed:
        return
    if state.status == OPEN:
        r = state.r_at(price)
    else:  # half already banked at TP1, rest marked to market
        r = 0.5 * state.r_at(state.tp1) + 0.5 * state.r_at(price)
    state.status = EXPIRED
    state.result_r = round(r, 2)
    state.events.append(EXPIRED)
