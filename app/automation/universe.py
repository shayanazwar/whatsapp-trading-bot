from __future__ import annotations

import logging
from dataclasses import dataclass

from .mexc_client import MexcClient

LOGGER = logging.getLogger(__name__)


# ============================================================
# UNIVERSE TARGETS
# ============================================================

MIN_SCAN_SYMBOLS = 100
TARGET_SCAN_SYMBOLS = 120
MAX_ALLOWED_SYMBOLS = 500


@dataclass(frozen=True)
class ContractMeta:
    symbol: str
    quote_coin: str
    settle_coin: str
    contract_size: float
    price_unit: float
    vol_unit: float
    min_vol: float
    max_vol: float
    price_scale: int
    vol_scale: int
    state: int
    api_allowed: bool
    hidden: bool
    future_type: int
    pre_market: bool
    maker_fee_rate: float = 0.0
    taker_fee_rate: float = 0.0


class MexcUniverse:

    def __init__(
        self,
        client: MexcClient,
        max_symbols: int = TARGET_SCAN_SYMBOLS,
        test_symbols: list[str] | None = None,
    ) -> None:

        self.client = client

        requested = int(
            max_symbols
            or TARGET_SCAN_SYMBOLS
        )

        # Normal scanning target is at least 100.
        # Maximum remains capped for safety.
        self.max_symbols = max(
            MIN_SCAN_SYMBOLS,
            min(
                requested,
                MAX_ALLOWED_SYMBOLS,
            ),
        )

        self.test_symbols = {
            value.upper()
            for value in (
                test_symbols or []
            )
        }

        self._contracts: dict[
            str,
            ContractMeta,
        ] = {}

        self._last_symbols: list[str] = []

    # ============================================================
    # REFRESH UNIVERSE
    # ============================================================

    async def refresh(self) -> list[str]:

        raw = await self.client.get_contracts()

        contracts: dict[
            str,
            ContractMeta,
        ] = {}

        # ========================================================
        # 1. BUILD VALID CONTRACT LIST
        # ========================================================

        for item in raw:

            try:

                symbol = str(
                    item["symbol"]
                ).upper()

                meta = ContractMeta(
                    symbol=symbol,

                    quote_coin=str(
                        item.get(
                            "quoteCoin",
                            "",
                        )
                    ).upper(),

                    settle_coin=str(
                        item.get(
                            "settleCoin",
                            "",
                        )
                    ).upper(),

                    contract_size=float(
                        item.get(
                            "contractSize",
                            0,
                        )
                    ),

                    price_unit=float(
                        item.get(
                            "priceUnit",
                            0,
                        )
                    ),

                    vol_unit=float(
                        item.get(
                            "volUnit",
                            0,
                        )
                    ),

                    min_vol=float(
                        item.get(
                            "minVol",
                            0,
                        )
                    ),

                    max_vol=float(
                        item.get(
                            "maxVol",
                            0,
                        )
                    ),

                    price_scale=int(
                        item.get(
                            "priceScale",
                            8,
                        )
                    ),

                    vol_scale=int(
                        item.get(
                            "volScale",
                            8,
                        )
                    ),

                    state=int(
                        item.get(
                            "state",
                            99,
                        )
                    ),

                    api_allowed=bool(
                        item.get(
                            "apiAllowed",
                            False,
                        )
                    ),

                    hidden=bool(
                        item.get(
                            "isHidden",
                            False,
                        )
                    ),

                    future_type=int(
                        item.get(
                            "futureType",
                            1,
                        )
                    ),

                    pre_market=bool(
                        item.get(
                            "preMarket",
                            False,
                        )
                    ),

                    maker_fee_rate=float(
                        item.get(
                            "makerFeeRate",
                            0,
                        )
                        or 0
                    ),

                    taker_fee_rate=float(
                        item.get(
                            "takerFeeRate",
                            0,
                        )
                        or 0
                    ),
                )

            except (
                KeyError,
                TypeError,
                ValueError,
            ):

                continue

            # ====================================================
            # HARD CONTRACT FILTERS
            # ====================================================

            # Active contract
            if meta.state != 0:
                continue

            # MEXC contract type
            if int(
                item.get(
                    "type",
                    1,
                )
                or 1
            ) != 1:
                continue

            # API trading allowed
            if not meta.api_allowed:
                continue

            # USDT perpetual futures
            if (
                meta.quote_coin != "USDT"
                or meta.settle_coin != "USDT"
            ):
                continue

            if meta.future_type != 1:
                continue

            if meta.pre_market:
                continue

            if meta.hidden:
                continue

            # Valid contract mathematics
            if (
                meta.contract_size <= 0
                or meta.vol_unit <= 0
            ):
                continue

            contracts[symbol] = meta

        self._contracts = contracts

        # ========================================================
        # 2. TEST SYMBOL OVERRIDE
        # ========================================================

        if self.test_symbols:

            symbols = [
                symbol
                for symbol in sorted(self.test_symbols)
                if symbol in contracts
            ]

            missing = sorted(
                self.test_symbols.difference(
                    symbols
                )
            )

            if missing:

                LOGGER.warning(
                    "TEST_SYMBOLS not available "
                    "on MEXC: %s",
                    ",".join(missing),
                )

        # ========================================================
        # 3. NORMAL PRODUCTION UNIVERSE
        # ========================================================

        else:

            try:

                tickers = (
                    await self.client.get_tickers()
                )

                turnover: dict[
                    str,
                    float,
                ] = {}

                for item in tickers:

                    try:

                        symbol = str(
                            item.get(
                                "symbol",
                                "",
                            )
                        ).upper()

                        if not symbol:
                            continue

                        # MEXC 24h turnover / amount.
                        value = float(
                            item.get(
                                "amount24",
                                0,
                            )
                            or 0
                        )

                        turnover[
                            symbol
                        ] = max(
                            0.0,
                            value,
                        )

                    except (
                        TypeError,
                        ValueError,
                    ):

                        continue

            except Exception as exc:

                LOGGER.warning(
                    "MEXC ticker universe "
                    "ranking unavailable: %s",
                    exc,
                )

                turnover = {}

            # ====================================================
            # RANK BY 24H TURNOVER
            # ====================================================

            ranked_symbols = sorted(
                contracts,
                key=lambda symbol: (
                    turnover.get(
                        symbol,
                        0.0,
                    ),
                    symbol,
                ),
                reverse=True,
            )

            # ====================================================
            # SELECT 120 TARGET
            # ====================================================

            symbols = ranked_symbols[
                : self.max_symbols
            ]

            # ====================================================
            # SAFETY CHECK
            # ====================================================

            if len(symbols) < MIN_SCAN_SYMBOLS:

                LOGGER.warning(
                    "MEXC has only %d eligible "
                    "symbols available; target is %d",
                    len(symbols),
                    MIN_SCAN_SYMBOLS,
                )

        # ========================================================
        # 4. SAVE
        # ========================================================

        self._last_symbols = symbols

        LOGGER.info(
            "MEXC scanner universe refreshed: "
            "%d symbols "
            "(target=%d minimum=%d)",
            len(symbols),
            self.max_symbols,
            MIN_SCAN_SYMBOLS,
        )

        return list(symbols)

    # ============================================================
    # SYMBOLS
    # ============================================================

    def symbols(self) -> list[str]:

        return list(
            self._last_symbols
        )

    # ============================================================
    # CONTRACT METADATA
    # ============================================================

    def get(
        self,
        symbol: str,
    ) -> ContractMeta:

        key = symbol.upper()

        try:

            return self._contracts[
                key
            ]

        except KeyError as exc:

            raise ValueError(
                "MEXC contract metadata "
                f"not loaded for {key}"
            ) from exc
