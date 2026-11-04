from __future__ import annotations

from app.config import get_settings
from app.market.mexc import MexcPublicClient


def eligible_universe(client: MexcPublicClient | None = None) -> list[dict]:
    cfg = get_settings()
    own = client is None
    client = client or MexcPublicClient()
    try:
        contracts = client.contracts()
        tickers = {x.get("symbol"): x for x in client.tickers()}
        eligible = []
        for contract in contracts:
            symbol = contract.get("symbol")
            ticker = tickers.get(symbol, {})
            if not symbol or contract.get("state") != 0:
                continue
            if contract.get("futureType") != 1 or contract.get("settleCoin") != "USDT":
                continue
            if contract.get("quoteCoin") != "USDT" or contract.get("isHidden"):
                continue
            turnover = float(ticker.get("amount24") or 0)
            bid, ask = float(ticker.get("bid1") or 0), float(ticker.get("ask1") or 0)
            if turnover < cfg.min_24h_turnover_usdt or bid <= 0 or ask <= 0 or ask < bid:
                continue
            mid = (ask + bid) / 2
            spread_bps = (ask - bid) / mid * 10_000
            if spread_bps > cfg.max_spread_bps:
                continue
            eligible.append({
                "symbol": symbol, "turnover_24h": turnover,
                "last_price": float(ticker.get("lastPrice") or 0),
                "spread_bps": spread_bps,
                "open_interest_contracts": (
                    float(ticker["holdVol"]) if ticker.get("holdVol") is not None else None
                ),
                "funding_rate": (
                    float(ticker["fundingRate"]) if ticker.get("fundingRate") is not None else None
                ),
                "contract": contract,
            })
        eligible.sort(key=lambda x: x["turnover_24h"], reverse=True)
        return eligible[:cfg.universe_limit]
    finally:
        if own:
            client.close()
