from __future__ import annotations

import argparse
import json
import logging
import sys

def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _cmd_init_db(args: argparse.Namespace) -> int:
    from cn_hk_collector.db import connect, init_schema

    with connect(args.database_url) as conn:
        init_schema(conn)
    print("Postgres schema initialized.")
    return 0


def _cmd_refresh_tickers(args: argparse.Namespace) -> int:
    from cn_hk_collector.db import connect, count_rows
    from cn_hk_collector.ticker_entity_registry import refresh_ticker_entities

    results = []
    with connect(args.database_url) as conn:
        for market in args.market:
            written = refresh_ticker_entities(market, db_conn=conn, chunk_size=args.chunk_size)
            total = count_rows(conn, "ticker_entities", market=market)
            results.append({"market": market, "written": written, "total": total})
    _print_json(results)
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    from cn_hk_collector.runner import collect_ticker

    result = collect_ticker(
        market=args.market,
        ticker=args.ticker,
        lookback_days=args.lookback_days,
        window_start=args.window_start,
        window_end=args.window_end,
        database_url=args.database_url,
        task_id=args.task_id,
        collect_media=not args.no_media,
        collect_social=not args.no_social,
    )
    _print_json(result.to_dict())
    return 0


def _cmd_smoke(args: argparse.Namespace) -> int:
    from cn_hk_collector.runner import collect_ticker

    targets = [("cn", args.cn_ticker), ("hk", args.hk_ticker)]
    results = []
    for market, ticker in targets:
        result = collect_ticker(
            market=market,
            ticker=ticker,
            lookback_days=args.lookback_days,
            database_url=args.database_url,
            collect_media=not args.no_media,
            collect_social=not args.no_social,
        )
        results.append(result.to_dict())
    _print_json(results)
    if args.require_rows and any(item["media_written"] + item["social_written"] <= 0 for item in results):
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cn-hk-collector", description="Standalone CN/HK media and Guba collector.")
    parser.add_argument("--database-url", help="Postgres DSN. Defaults to DATABASE_URL.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="Initialize local Postgres tables and indexes.")
    init_db.set_defaults(func=_cmd_init_db)

    refresh = subparsers.add_parser("refresh-tickers", help="Import ticker entity data from AKShare into Postgres.")
    refresh.add_argument("--market", nargs="+", default=["cn", "hk"], choices=["cn", "hk"])
    refresh.add_argument("--chunk-size", type=int, default=500)
    refresh.set_defaults(func=_cmd_refresh_tickers)

    collect = subparsers.add_parser("collect", help="Collect one ticker and write raw_media/raw_social rows.")
    collect.add_argument("--market", required=True, choices=["cn", "hk"])
    collect.add_argument("--ticker", required=True)
    collect.add_argument("--lookback-days", type=int, default=7)
    collect.add_argument("--window-start", help="Optional ISO timestamp in UTC or with timezone.")
    collect.add_argument("--window-end", help="Optional ISO timestamp in UTC or with timezone.")
    collect.add_argument("--task-id", help="Optional external task id stored on raw rows.")
    collect.add_argument("--no-media", action="store_true", help="Skip CN/HK news media collection.")
    collect.add_argument("--no-social", action="store_true", help="Skip EastMoney Guba collection.")
    collect.set_defaults(func=_cmd_collect)

    smoke = subparsers.add_parser("smoke", help="Collect one A-share and one HK ticker.")
    smoke.add_argument("--cn-ticker", default="600519")
    smoke.add_argument("--hk-ticker", default="0700")
    smoke.add_argument("--lookback-days", type=int, default=1)
    smoke.add_argument("--no-media", action="store_true")
    smoke.add_argument("--no-social", action="store_true")
    smoke.add_argument("--require-rows", action="store_true", help="Exit non-zero if either market writes zero rows.")
    smoke.set_defaults(func=_cmd_smoke)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.exception("command failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
