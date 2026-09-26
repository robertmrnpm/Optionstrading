"""Application configuration.

Settings come from three layers (later wins):
  1. Defaults defined in the pydantic models below.
  2. ``config.yaml`` (or the file named by ``OPTRADER_CONFIG``).
  3. Runtime overrides saved from the dashboard (``<data_dir>/runtime_overrides.json``).

Secrets (API keys, webhook URLs) only come from environment variables / ``.env``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, ClassVar, Literal

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


class AppSettings(BaseModel):
    # sim   = synthetic market + paper broker (works any time, no keys needed)
    # paper = real market data + paper broker (simulated fills against real quotes)
    # live  = real market data + real Webull orders
    mode: Literal["sim", "paper", "live"] = "sim"
    # Where market data comes from in paper/live mode.
    data_source: Literal["webull", "yahoo"] = "webull"
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: str = "data"
    # Simulation speed multiplier (sim mode only). 30 = one trading day in ~13 minutes.
    sim_speed: float = 30.0
    sim_seed: int | None = None
    loop_interval_seconds: float = 5.0


class ExecutionSettings(BaseModel):
    # alerts   = agents only notify you, never place orders
    # approval = agents propose trades; you approve each one in the dashboard
    # auto     = agents place trades on their own within risk limits
    trade_mode: Literal["alerts", "approval", "auto"] = "approval"
    proposal_ttl_seconds: int = 120
    # Entry order management: start at mid, step toward the ask, give up after timeout.
    entry_timeout_seconds: int = 45
    entry_chase_steps: int = 3
    # Max entry price = mid + this fraction of (ask - mid). 1.0 = willing to pay the ask.
    max_chase_pct_of_spread: float = 0.75
    # How far (fraction of premium) the price may move between proposal and approval.
    max_price_drift_pct: float = 0.10


class RiskSettings(BaseModel):
    # Used when the broker does not report equity (e.g. sim/paper).
    starting_equity: float = 10_000.0
    account_type: Literal["margin", "cash"] = "margin"
    # Pattern Day Trader rule (FINRA): <$25k margin accounts get 3 day trades / 5 business days.
    pdt_enforce: bool = True
    pdt_threshold_equity: float = 25_000.0
    pdt_max_day_trades: int = 3
    # Keep this many day trades in reserve (e.g. 1 = never use the last one automatically).
    pdt_reserve: int = 0
    risk_per_trade_pct: float = 1.0          # % of equity risked (entry -> stop) per trade
    max_premium_per_trade: float = 750.0     # hard $ cap on premium paid per trade
    max_position_pct: float = 10.0           # max % of equity in a single position's premium
    max_daily_loss: float = 300.0            # $; realized + unrealized. Halts new entries.
    max_daily_loss_pct: float = 3.0          # % of equity; whichever is smaller wins
    max_open_positions: int = 2
    max_trades_per_day: int = 4
    max_consecutive_losses: int = 3
    min_signal_score: float = 65.0
    no_entry_first_minutes: int = 5          # skip the opening chaos
    last_entry_time: str = "15:30"           # ET
    zero_dte_last_entry_time: str = "15:00"  # ET
    flatten_time: str = "15:50"              # ET: everything is closed (day trading only)
    kill_switch: bool = False
    # Daily profit goal ($). 0 = off. When reached (realized + open P&L), the agents stop opening
    # new trades so a green day isn't given back. It is a stopping rule, never a reason to size up.
    daily_profit_target: float = 0.0
    stop_at_profit_target: bool = True


class ExitSettings(BaseModel):
    stop_loss_pct: float = 0.30        # exit if premium falls 30% from entry
    target1_pct: float = 0.40          # scale out part of the position at +40%
    target1_fraction: float = 0.5
    target2_pct: float = 0.90          # close the rest at +90%
    breakeven_after_target1: bool = True
    trail_activation_pct: float = 0.25 # start trailing once +25%
    trail_pct: float = 0.20            # give back at most 20% from the high-water mark
    time_stop_minutes: int = 60        # close stale trades that go nowhere
    zero_dte_stop_loss_pct: float = 0.35
    zero_dte_time_stop_minutes: int = 30


class ContractSettings(BaseModel):
    max_spread_pct: float = 0.10       # (ask-bid)/mid
    min_open_interest: int = 300
    min_volume: int = 50
    min_premium: float = 0.20
    max_premium: float = 10.00
    target_delta: float = 0.45
    zero_dte_target_delta: float = 0.35
    min_dte: int = 0
    max_dte: int = 10


class ScannerSettings(BaseModel):
    core_watchlist: list[str] = Field(default_factory=lambda: [
        "SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA", "AMD", "META", "MSFT",
        "AMZN", "GOOGL", "NFLX", "AVGO", "PLTR", "COIN", "MU", "SMCI", "UBER",
    ])
    zero_dte_symbols: list[str] = Field(default_factory=lambda: ["SPY", "QQQ", "IWM"])
    use_market_movers: bool = True     # add top gainers/losers/most-active from the data source
    max_hot_list: int = 25
    min_price: float = 5.0
    min_day_volume: int = 1_000_000
    interval_seconds: int = 60


class StrategySettings(BaseModel):
    momentum: bool = True
    unusual_options: bool = True
    zero_dte: bool = True
    catalyst: bool = True
    orb_minutes: int = 15
    min_relative_volume: float = 1.5
    uoa_min_vol_oi_ratio: float = 3.0
    uoa_min_premium: float = 250_000.0
    uoa_interval_seconds: int = 180
    catalyst_min_gap_pct: float = 4.0


class AISettings(BaseModel):
    enabled: bool = False              # also requires ANTHROPIC_API_KEY
    model: str = "claude-opus-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    web_search: bool = True            # let the analyst look up news / catalysts
    required_for_auto: bool = True     # in auto mode, only take trades the analyst OKs
    min_confidence: int = 60
    timeout_seconds: float = 90.0


class NotifySettings(BaseModel):
    on_signal: bool = False
    on_proposal: bool = True
    on_fill: bool = True
    on_exit: bool = True
    on_risk_block: bool = True


class Settings(BaseModel):
    app: AppSettings = Field(default_factory=AppSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    exits: ExitSettings = Field(default_factory=ExitSettings)
    contracts: ContractSettings = Field(default_factory=ContractSettings)
    scanner: ScannerSettings = Field(default_factory=ScannerSettings)
    strategies: StrategySettings = Field(default_factory=StrategySettings)
    ai: AISettings = Field(default_factory=AISettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)

    # ---- secrets (environment only) -------------------------------------------------
    @property
    def webull_app_key(self) -> str | None:
        return os.getenv("WEBULL_APP_KEY") or None

    @property
    def webull_app_secret(self) -> str | None:
        return os.getenv("WEBULL_APP_SECRET") or None

    @property
    def webull_account_id(self) -> str | None:
        return os.getenv("WEBULL_ACCOUNT_ID") or None

    @property
    def webull_region(self) -> str:
        return os.getenv("WEBULL_REGION", "us")

    @property
    def dashboard_token(self) -> str | None:
        return os.getenv("DASHBOARD_TOKEN") or None

    @property
    def allow_live_auto(self) -> bool:
        """Auto-trading real money needs an explicit, separate opt-in."""
        return os.getenv("ALLOW_LIVE_AUTO_TRADING", "").lower() in ("1", "true", "yes")

    @property
    def data_path(self) -> Path:
        p = Path(self.app.data_dir)
        if not p.is_absolute():
            p = ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def overrides_path(self) -> Path:
        return self.data_path / "runtime_overrides.json"

    # ---- runtime overrides -------------------------------------------------------------
    EDITABLE_SECTIONS: ClassVar[tuple[str, ...]] = ("execution", "risk", "exits", "contracts", "strategies", "ai", "notify")

    def apply_patch(self, patch: dict[str, Any]) -> "Settings":
        """Validate and apply a partial update (only editable sections)."""
        data = self.model_dump()
        for section, values in patch.items():
            if section not in self.EDITABLE_SECTIONS or not isinstance(values, dict):
                raise ValueError(f"section '{section}' is not editable at runtime")
            for key in values:
                if key not in data[section]:
                    raise ValueError(f"unknown setting {section}.{key}")
            data[section].update(values)
        new = Settings.model_validate(data)
        for section in self.EDITABLE_SECTIONS:
            setattr(self, section, getattr(new, section))
        return self

    def save_overrides(self) -> None:
        dump = {s: getattr(self, s).model_dump() for s in self.EDITABLE_SECTIONS}
        self.overrides_path.write_text(json.dumps(dump, indent=2))


def _deep_merge(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_settings(config_path: str | os.PathLike | None = None, use_overrides: bool = True) -> Settings:
    _load_dotenv(ROOT / ".env")
    path = Path(config_path or os.getenv("OPTRADER_CONFIG", ROOT / "config.yaml"))
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    settings = Settings.model_validate(data)
    if use_overrides and settings.overrides_path.exists():
        try:
            overrides = json.loads(settings.overrides_path.read_text())
            merged = _deep_merge(settings.model_dump(), overrides)
            settings = Settings.model_validate(merged)
        except Exception:  # corrupt overrides should never stop the app from starting
            pass
    return settings
