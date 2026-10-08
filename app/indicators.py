"""Indicators used by the strategy. Wilder smoothing for RSI and ATR, matching TradingView/MT5."""
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100 - 100 / (1 + rs)
    # No losses in the window means RSI is 100, not NaN.
    return out.where(avg_loss != 0, 100.0).where(avg_gain.notna())


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index (Wilder): trend strength from 0 to 100, regardless of direction."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    smooth = lambda s: s.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()  # noqa: E731
    tr = atr(df, 1)  # true range per bar
    atr_s = smooth(tr)
    plus_di = 100 * smooth(plus_dm) / atr_s
    minus_di = 100 * smooth(minus_dm) / atr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return smooth(dx)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
