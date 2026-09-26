"""Command line entry point.

    python -m optrader run [--mode sim|paper|live] [--trade-mode alerts|approval|auto] [--port 8000]
    python -m optrader backtest --source sim|yahoo --symbols SPY,QQQ,TSLA --days 5
    python -m optrader diagnose          # test your Webull OpenAPI connection & show raw responses
    python -m optrader reset-paper       # reset the paper account to starting equity
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .config import load_settings

BANNER = r"""
  ___        _   _                  _                    _
 / _ \ _ __ | |_(_) ___  _ __  ___ / \   __ _  ___ _ __ | |_ ___
| | | | '_ \| __| |/ _ \| '_ \/ __/ _ \ / _` |/ _ \ '_ \| __/ __|
| |_| | |_) | |_| | (_) | | | \__ \/ ___ \ (_| |  __/ | | | |_\__ \
 \___/| .__/ \__|_|\___/|_| |_|___/_/   \_\__, |\___|_| |_|\__|___/
      |_|                                |___/
"""


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "uvicorn.access", "webull", "urllib3", "yfinance", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_run(args) -> int:
    import uvicorn

    from .api.server import create_app
    from .factory import build

    settings = load_settings(args.config)
    if args.mode:
        settings.app.mode = args.mode
    if args.trade_mode:
        settings.execution.trade_mode = args.trade_mode
    if args.port:
        settings.app.port = args.port
    if args.host:
        settings.app.host = args.host
    if args.speed:
        settings.app.sim_speed = args.speed

    local = settings.app.host in ("127.0.0.1", "localhost", "::1")
    if not local and not settings.dashboard_token:
        print("Refusing to listen on a non-local address without DASHBOARD_TOKEN set in .env "
              "(anyone who can reach the dashboard could place trades).")
        return 2
    if settings.app.mode == "live":
        print("\n*** LIVE MODE: orders will be sent to your real Webull account. ***")
        print(f"    trade mode: {settings.execution.trade_mode}"
              + (" (auto-trading real money requires ALLOW_LIVE_AUTO_TRADING=true)"
                 if settings.execution.trade_mode == "auto" and not settings.allow_live_auto else ""))
        if not args.yes:
            if input("Type LIVE to continue: ").strip() != "LIVE":
                print("Aborted.")
                return 1
    print(BANNER)
    try:
        orch = build(settings)
    except Exception as e:
        print(f"Startup failed: {e}")
        return 1
    app = create_app(orch)
    print(f"Dashboard: http://{settings.app.host}:{settings.app.port}   (mode={settings.app.mode}, "
          f"data={orch.ctx.data.name}, broker={orch.ctx.broker.name}, trade_mode={settings.execution.trade_mode})")
    uvicorn.run(app, host=settings.app.host, port=settings.app.port, log_level="warning")
    return 0


def cmd_backtest(args) -> int:
    from pathlib import Path

    from .backtest import Backtester, BacktestConfig, format_report, load_bars, save_report

    settings = load_settings(args.config, use_overrides=not args.defaults)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.equity:
        settings.risk.starting_equity = args.equity
    if args.no_pdt:
        settings.risk.pdt_enforce = False
    bars = asyncio.run(load_bars(args.source, symbols, args.days, args.interval))
    missing = [s for s, b in bars.items() if not b]
    if missing:
        print(f"warning: no data for {', '.join(missing)}")
    if len(missing) == len(bars):
        print("No data loaded — nothing to backtest.")
        return 1
    result = Backtester(settings, bars, BacktestConfig(iv_mult=args.iv_mult)).run()
    print(format_report(result))
    path = save_report(result, Path(settings.data_path.parent / "backtest_results"))
    print(f"\nSaved: {path}_trades.csv and {path}_summary.json")
    return 0


def cmd_diagnose(args) -> int:
    """Checks every Webull endpoint the app uses and prints the raw JSON so field names can be verified."""
    from datetime import date

    from .webull_client import make_api_client, response_json

    settings = load_settings(args.config)
    try:
        api = make_api_client(settings.webull_app_key, settings.webull_app_secret, settings.webull_region,
                              settings.data_path / "webull_token")
    except Exception as e:
        print(f"✗ {e}")
        return 1
    from webull.data.data_client import DataClient
    from webull.trade.trade_client import TradeClient
    print("Connecting... if this is the first run, approve the login request in your Webull app.")
    tc, dc = TradeClient(api), DataClient(api)

    def show(name, fn):
        try:
            data = response_json(fn(), name)
            print(f"\n✓ {name}\n{json.dumps(data, indent=2, default=str)[:args.max_chars]}")
            return data
        except Exception as e:
            print(f"\n✗ {name}: {e}")
            return None

    accounts = show("account list", tc.account_v2.get_account_list)
    acct = settings.webull_account_id
    if not acct and accounts:
        from .webull_client import find_dicts
        found = find_dicts(accounts, ["account_id"])
        acct = str(found[0]["account_id"]) if found else None
    if acct:
        show("account balance", lambda: tc.account_v2.get_account_balance(acct))
        show("positions", lambda: tc.account_v2.get_account_position(acct))
        show("open orders", lambda: tc.order_v3.get_order_open(acct))
    show("stock snapshot SPY,AAPL", lambda: dc.market_data.get_snapshot(["SPY", "AAPL"], "US_STOCK"))
    show("1m bars SPY", lambda: dc.market_data.get_batch_history_bar(["SPY"], "US_STOCK", "M1", "5"))
    contracts = show("option contracts SPY (near the money)",
                     lambda: dc.instrument.list_option_contracts(underlying_symbols="SPY",
                                                                 start_date=date.today().isoformat()))
    if contracts:
        from .webull_client import find_dicts, pick
        rows = find_dicts(contracts, ["strike_price"])[:3]
        syms = [str(pick(r, "symbol", "option_symbol")) for r in rows if pick(r, "symbol", "option_symbol")]
        if syms:
            show("option snapshot", lambda: dc.option_market_data.get_option_snapshot(",".join(syms), "US_OPTION"))
    show("screener gainers", lambda: dc.screener.list_gainers_losers("DAY_1", "US_STOCK", "CHANGE_RATIO", "DESC"))
    print("\nIf any field names differ from what optrader/data/webull_data.py expects, open an issue / adjust pick() keys.")
    return 0


def cmd_reset_paper(args) -> int:
    settings = load_settings(args.config)
    for name in ("paper_account.json", "paper_sim.json"):
        p = settings.data_path / name
        if p.exists():
            p.unlink()
            print(f"removed {p}")
    print(f"Paper accounts reset (next start uses ${settings.risk.starting_equity:,.0f}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="optrader", description="Options day-trading agents for Webull")
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="start the agents + dashboard")
    r.add_argument("--mode", choices=["sim", "paper", "live"])
    r.add_argument("--trade-mode", choices=["alerts", "approval", "auto"])
    r.add_argument("--host")
    r.add_argument("--port", type=int)
    r.add_argument("--speed", type=float, help="sim mode speed multiplier")
    r.add_argument("--yes", action="store_true", help="skip the live-mode confirmation prompt")

    b = sub.add_parser("backtest", help="replay history through the strategies")
    b.add_argument("--source", choices=["sim", "yahoo"], default="sim")
    b.add_argument("--symbols", default="SPY,QQQ,IWM,AAPL,NVDA,TSLA,AMD,META")
    b.add_argument("--days", type=int, default=5)
    b.add_argument("--interval", choices=["1m", "5m"], default="1m")
    b.add_argument("--iv-mult", type=float, default=1.15)
    b.add_argument("--equity", type=float)
    b.add_argument("--no-pdt", action="store_true", help="ignore the PDT limit (to see raw strategy edge)")
    b.add_argument("--defaults", action="store_true", help="ignore dashboard setting overrides")

    d = sub.add_parser("diagnose", help="test the Webull OpenAPI connection")
    d.add_argument("--max-chars", type=int, default=1500)

    sub.add_parser("reset-paper", help="reset paper trading accounts")

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    if args.cmd is None:
        args = ap.parse_args(["run"] + (argv or sys.argv[1:]))
    return {"run": cmd_run, "backtest": cmd_backtest, "diagnose": cmd_diagnose,
            "reset-paper": cmd_reset_paper}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
