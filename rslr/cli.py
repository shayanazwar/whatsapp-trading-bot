from __future__ import annotations

import argparse
import json

from rslr.backtest import run_backtest
from rslr.services import scan_market
from rslr.storage import Database
from rslr.walk_forward import run_walk_forward


def main() -> None:
    parser = argparse.ArgumentParser(prog="rslr")
    sub = parser.add_subparsers(dest="command", required=True)
    backtest = sub.add_parser("backtest")
    backtest.add_argument("period", choices=["1D", "7D", "30D", "1d", "7d", "30d"])
    sub.add_parser("scan")
    walk = sub.add_parser("walk-forward")
    walk.add_argument("period", choices=["30D", "30d"], default="30D", nargs="?")
    args = parser.parse_args()
    database = Database()
    if args.command == "backtest":
        result = run_backtest(args.period)
        payload = result.to_dict()
        database.save_backtest(payload)
        print(json.dumps(payload, indent=2, default=str))
    elif args.command == "scan":
        result = scan_market(database=database)
        print(json.dumps([item.to_dict() for item in result], indent=2, default=str))
    elif args.command == "walk-forward":
        result = run_walk_forward(args.period)
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
