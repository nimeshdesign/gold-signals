"""Settings loaded from environment variables (and a .env file if present)."""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class StrategyParams:
    trend_ema: int = 200
    fast_ema: int = 9
    slow_ema: int = 21
    rsi_period: int = 14
    rsi_buy_min: float = 50
    rsi_buy_max: float = 70
    rsi_sell_min: float = 30
    rsi_sell_max: float = 50
    atr_period: int = 14
    atr_sl_mult: float = 1.5
    tp1_r: float = 1.5
    tp2_r: float = 3.0

    @classmethod
    def from_env(cls) -> "StrategyParams":
        return cls(
            trend_ema=_int("TREND_EMA", 200),
            fast_ema=_int("FAST_EMA", 9),
            slow_ema=_int("SLOW_EMA", 21),
            rsi_period=_int("RSI_PERIOD", 14),
            rsi_buy_min=_float("RSI_BUY_MIN", 50),
            rsi_buy_max=_float("RSI_BUY_MAX", 70),
            rsi_sell_min=_float("RSI_SELL_MIN", 30),
            rsi_sell_max=_float("RSI_SELL_MAX", 50),
            atr_period=_int("ATR_PERIOD", 14),
            atr_sl_mult=_float("ATR_SL_MULT", 1.5),
            tp1_r=_float("TP1_R", 1.5),
            tp2_r=_float("TP2_R", 3.0),
        )


@dataclass(frozen=True)
class Settings:
    brand_name: str = field(default_factory=lambda: _str("BRAND_NAME", "Gold Signals"))
    site_url: str = field(default_factory=lambda: _str("SITE_URL", "http://localhost:8000").rstrip("/"))
    # Shown at the top of every page when set (e.g. for a preview loaded with backtest data).
    site_banner: str = field(default_factory=lambda: _str("SITE_BANNER"))

    twelve_data_api_key: str = field(default_factory=lambda: _str("TWELVE_DATA_API_KEY"))
    symbol: str = field(default_factory=lambda: _str("SYMBOL", "XAU/USD"))
    display_symbol: str = field(default_factory=lambda: _str("DISPLAY_SYMBOL", "XAUUSD"))

    telegram_bot_token: str = field(default_factory=lambda: _str("TELEGRAM_BOT_TOKEN"))
    telegram_channel_id: str = field(default_factory=lambda: _str("TELEGRAM_CHANNEL_ID"))

    # Owner login for the website (hash made with: python -m scripts.set_password).
    admin_username: str = field(default_factory=lambda: _str("ADMIN_USERNAME", "admin"))
    admin_password_hash: str = field(default_factory=lambda: _str("ADMIN_PASSWORD_HASH"))
    # Signs login cookies. Keep it secret; changing it signs everyone out.
    session_secret: str = field(default_factory=lambda: _str("SESSION_SECRET"))

    database_path: Path = field(default_factory=lambda: ROOT / _str("DATABASE_PATH", "data/signals.db"))
    # Backtest trades for the analytics page (built with: python -m scripts.load_demo --db data/backtest.db)
    backtest_database_path: Path = field(default_factory=lambda: ROOT / _str("BACKTEST_DATABASE_PATH", "data/backtest.db"))

    htf_interval: str = field(default_factory=lambda: _str("HTF_INTERVAL", "4h"))
    ltf_interval: str = field(default_factory=lambda: _str("LTF_INTERVAL", "15min"))
    max_open_signals: int = field(default_factory=lambda: _int("MAX_OPEN_SIGNALS", 1))
    signal_expiry_hours: float = field(default_factory=lambda: _float("SIGNAL_EXPIRY_HOURS", 48))

    news_filter_enabled: bool = field(default_factory=lambda: _bool("NEWS_FILTER_ENABLED", True))
    news_block_before: int = field(default_factory=lambda: _int("NEWS_BLOCK_MINUTES_BEFORE", 30))
    news_block_after: int = field(default_factory=lambda: _int("NEWS_BLOCK_MINUTES_AFTER", 30))
    news_fail_closed: bool = field(default_factory=lambda: _bool("NEWS_FAIL_CLOSED", False))

    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    # Pip and lot maths shown in signals (XAUUSD: 1 pip = $0.10, 1 standard lot = 100 oz).
    pip_size: float = field(default_factory=lambda: _float("PIP_SIZE", 0.10))
    contract_oz: float = field(default_factory=lambda: _float("CONTRACT_SIZE_OZ", 100))
    # Live trade tracking: while a trade is open, check 1-minute prices this often (minutes); slow down to
    # TRACK_SLOW_MINUTES once the day's Twelve Data requests pass TRACK_CREDIT_BUDGET (free plan: 800/day).
    track_minutes: int = field(default_factory=lambda: _int("TRACK_MINUTES", 1))
    track_slow_minutes: int = field(default_factory=lambda: _int("TRACK_SLOW_MINUTES", 5))
    track_credit_budget: int = field(default_factory=lambda: _int("TRACK_CREDIT_BUDGET", 700))
    # Twelve Data requests allowed per UTC day (free plan: 800).
    daily_credit_limit: int = field(default_factory=lambda: _int("DAILY_CREDIT_LIMIT", 800))
    # Broker spread used when checking live SL/TP: a SELL closes at the ask (chart price + spread).
    live_spread: float = field(default_factory=lambda: _float("LIVE_SPREAD", 0.30))
    # Your usual lot size; signals show the $ risk and reward at this size (0 = don't show).
    lot_size: float = field(default_factory=lambda: _float("LOT_SIZE", 0))
    # Daily plan / day-end / weekly summary messages in the channel.
    daily_updates: bool = field(default_factory=lambda: _bool("DAILY_UPDATES", True))
    # Local time shown in Telegram messages (default India Standard Time).
    display_tz_offset: float = field(default_factory=lambda: _float("DISPLAY_TZ_OFFSET_HOURS", 5.5))
    display_tz_name: str = field(default_factory=lambda: _str("DISPLAY_TZ_NAME", "IST"))

    strategy: StrategyParams = field(default_factory=StrategyParams.from_env)
    # Which rules the engine runs (see app/strategies.py) and their settings as JSON.
    strategy_name: str = field(default_factory=lambda: _str("STRATEGY", "ema_cross"))
    strategy_params: dict = field(default_factory=lambda: json.loads(_str("STRATEGY_PARAMS") or "{}"))
    # Extra setups that run alongside the main one, as a JSON list:
    # [{"name": "orb", "label": "NY open breakout", "params": {...}}]
    extra_strategies: list = field(default_factory=lambda: json.loads(_str("EXTRA_STRATEGIES") or "[]"))


settings = Settings()
