Today 10:17 AM
Pasted text(20260925-171707).txt
Document
scanner.py
Pasted text(20260925-171857).txt
Document
logs
Pasted text(20260925-172044).txt
Document
new logs
Pasted text(20260925-172322).txt
Document
whatsapp-trading-bot-main(1).zip
Zip Archive
attached is the all codes all files everything double check evreything plz,i cannot give you codes 1 by 1 many times, now inspect all the code and find out mistakes and give me all replacements at onces of all files  if needed,  ill paste new codes
MEXC_GOLD_STANDARD_V1_FIXED_PROJECT(1).zip
Zip Archive
is this zip fixed? not have any error? check all codes files, and check are they same on which we agreed
Pasted text(20260925-201217).txt
Document
new logs
Pasted text(20260925-201435).txt
Document
engine
Pasted text(20260925-201612).txt
Document
scanner
gold_v1_4_replacements(1).zip
Zip Archive
give me easy to copy here.       engine and scanner
Pasted text(20260925-203211).txt
Document
here are new logs after deploying engine and scanner
Pasted text(20260925-203426).txt
Document
2026-09-25T20:31:56.85823475Z 2026-09-25 20:31:56,858 | INFO | httpx | HTTP Request: GET https://api.mexc.com/api/v1/contract/kline/PAXG_USDT?interval=Min15&start=1790140616&end=1790368316 "HTTP/1.1 200 OK"
2026-09-25T20:31:56.944682304Z 2026-09-25 20:31:56,944 | INFO | httpx | HTTP Request: GET https://api.mexc.com/api/v1/contract/kline/BP_USDT?interval=Day1&start=1784925116&end=1790368316 "HTTP/1.1 200 OK"
2026-09-25T20:31:57.011088585Z 2026-09-25 20:31:57,010 | INFO | app.automation.scanner | MEXC ANALYSIS | BP_USDT | setup=NO TRADE | score=15 | rr=None | stage=TECHNICAL | failures=['1H alignment'] | bos(L/S)=12/2 | retest(L/S)=True/False | trigger=NONE
2026-09-25T20:31:57.011109476Z 2026-09-25 20:31:57,010 | INFO | app.automation.scanner | MEXC REJECT | BP_USDT | stage=TECHNICAL | reason=['Analysis engine produced no valid LONG/SHORT setup'] | setup=NO TRADE | score=15 | rr=None
2026-09-25T20:31:57.486140484Z 2026-09-25 20:31:57,485 | INFO | httpx | HTTP Request: GET https://api.mexc.com/api/v1/contract/kline/PAXG_USDT?interval=Day1&start=1784925116&end=1790368316 "HTTP/1.1 200 OK"
2026-09-25T20:31:57.511059854Z 2026-09-25 20:31:57,510 | INFO | app.automation.scanner | MEXC ANALYSIS | PAXG_USDT | setup=NO TRADE | score=0 | rr=None | stage=TECHNICAL | failures=['4H regime', '1H alignment'] | bos(L/S)=27/12 | retest(L/S)=True/False | trigger=NONE
2026-09-25T20:31:57.511074594Z 2026-09-25 20:31:57,510 | INFO | app.automation.scanner | MEXC REJECT | PAXG_USDT | stage=TECHNICAL | reason=['Analysis engine produced no valid LONG/SHORT setup'] | setup=NO TRADE | score=0 | rr=None
2026-09-25T20:31:57.511722254Z 2026-09-25 20:31:57,511 | INFO | httpx | HTTP Request: GET https://api.mexc.com/api/v1/contract/kline/XPT_USDT?interval=Day1&start=1784925116&end=1790368316 "HTTP/1.1 200 OK" 
2026-09-25T20:31:57.61690248Z 2026-09-25 20:31:57,616 | INFO | app.automation.scanner | MEXC ANALYSIS | XPT_USDT | setup=NO TRADE | score=5 | rr=None | stage=TECHNICAL | failures=['4H regime', '1H alignment'] | bos(L/S)=18/7 | retest(L/S)=True/False | trigger=NONE 
2026-09-25T20:31:57.616926342Z 2026-09-25 20:31:57,616 | INFO | app.automation.scanner | MEXC REJECT | XPT_USDT | stage=TECHNICAL | reason=['Analysis engine produced no valid LONG/SHORT setup'] | setup=NO TRADE | score=5 | rr=None 
2026-09-25T20:31:57.617347647Z 2026-09-25 20:31:57,617 | INFO | app.automation.scanner | MEXC scan complete: symbols=120 valid=0 sent=0 errors=0 technical_candidates=0 rejected_setup=120 rejected_btc=0 rejected_futures=0 rejected_quote=0 rejected_freshness=0 rejected_execution_quality=0 rejected_final=0 
2026-09-25T20:31:57.617367109Z 2026-09-25 20:31:57,617 | INFO | app.automation.scheduler | MEXC scanner cycle completed in 68.50s: {'symbols': 120, 'valid': 0, 'sent': 0, 'errors': 0, 'technical_candidates': 0, 'rejected_setup': 120, 'rejected_futures': 0, 'rejected_final': 0, 'rejected_btc': 0, 'rejected_quote': 0, 'rejected_execution_quality': 0, 'rejected_freshness': 0} 
2026-09-25T20:31:57.834221657Z INFO:     10.236.26.124:44646 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:32:02.830936273Z INFO:     10.236.26.124:51156 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:32:02.833913035Z INFO:     10.236.26.124:51164 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:32:07.833822463Z INFO:     10.236.26.124:51180 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:32:12.83359602Z INFO:     10.236.26.124:48374 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:32:17.833378014Z INFO:     10.236.26.124:48384 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:32:22.834263812Z INFO:     10.236.26.124:43588 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:32:27.833702268Z INFO:     10.236.26.124:43592 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:32:32.831107898Z INFO:     10.236.26.124:56772 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:32:32.832966602Z INFO:     10.236.26.124:56778 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:32:37.833928655Z INFO:     10.236.26.124:56786 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:32:42.833416536Z INFO:     10.236.26.124:39822 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:32:47.833808848Z INFO:     10.236.26.124:39832 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:32:52.837315117Z INFO:     10.236.26.124:42624 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:32:57.833506178Z INFO:     10.236.26.124:42630 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:33:02.830856025Z INFO:     10.236.26.124:57492 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:33:02.833379439Z INFO:     10.236.26.124:57508 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:33:07.834129001Z INFO:     10.236.26.124:57524 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:33:12.833326055Z INFO:     10.236.26.124:38968 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:33:17.833469944Z INFO:     10.236.26.124:38984 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:33:22.833998543Z INFO:     10.236.26.124:40408 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:33:27.833526918Z INFO:     10.236.26.124:40418 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:33:32.830396257Z INFO:     10.236.26.124:39422 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:33:32.833402821Z INFO:     10.236.26.124:39424 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:33:37.83342577Z INFO:     10.236.26.124:39440 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:33:42.833901553Z INFO:     10.236.26.124:51200 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:33:47.836010003Z INFO:     10.236.26.124:51216 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:33:52.833325087Z INFO:     10.236.26.124:34454 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:33:57.833502733Z INFO:     10.236.26.124:34470 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:34:02.83095556Z INFO:     10.236.26.124:47136 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:34:02.834027307Z INFO:     10.236.26.124:47146 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:34:07.833265909Z INFO:     10.236.26.124:47150 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:34:12.834112386Z INFO:     10.236.26.124:45826 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:34:17.833981691Z INFO:     10.236.26.124:45830 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:34:22.834166122Z INFO:     10.236.26.124:59940 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:34:27.833804027Z INFO:     10.236.26.124:59954 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:34:32.831043052Z INFO:     10.236.26.124:34964 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:34:32.833689273Z INFO:     10.236.26.124:34972 - "GET /health HTTP/1.1" 200 OK 
2026-09-25T20:34:37.833655532Z INFO:     10.236.26.124:34986 - "GET /health HTTP/1.1" 200 OK                      attached is engine.py with new logs
Pasted text(20260925-203948).txt
Document
scanner.py
Pasted text(20260925-205728).txt
Document
new logs

New logs are healthy — the bot is online, MEXC API is responding, and /health is 200 OK.

Current problem is not offline. It is 0 technical setups.

Main blockers shown:

4H regime
1H alignment
5M trigger
some 15M BOS
retests are actually working on several symbols.
Next fix

Do not loosen the rules yet.

We need one code change now: make the scanner print an aggregate gate report for all 120 symbols so we know exactly how many fail at each gate.

Send me the latest engine.py that you committed, and I’ll make that diagnostic version next.

Pasted text(20260925-205816).txt
Document
Analysis errored
Analysis errored
from pathlib import Path
import re, py_compile

src = Path("/mnt/data/Pasted text(20260925-203948).txt")
out = Path("/mnt/data/PAK_MEXC_Gold_V1_4_scanner_diagnostics.py")
text = src.read_text()

new_scan_once = '''    async def scan_once(self) -> dict[str, int]:
        symbols = await self.universe.refresh()
        if not symbols:
            return {"symbols": 0, "valid": 0, "sent": 0, "errors": 0}

        await self._refresh_btc_context()
        concurrency = max(1, int(getattr(self.settings, "scan_concurrency", 8)))
        semaphore = asyncio.Semaphore(concurrency)
        results = await asyncio.gather(
            *(self._scan_one(symbol, semaphore) for symbol in symbols),
            return_exceptions=True,
        )

        stats: dict[str, int] = {
            "symbols": len(symbols),
            "valid": 0,
            "sent": 0,
            "errors": 0,
            "technical_candidates": 0,
            "rejected_setup": 0,
            "rejected_futures": 0,
            "rejected_final": 0,
            "rejected_btc": 0,
            "rejected_quote": 0,
            "rejected_execution_quality": 0,
            "rejected_freshness": 0,
        }

        for result in results:
            if isinstance(result, Exception):
                stats["errors"] += 1
                continue
            if not isinstance(result, dict):
                continue

            stats["valid"] += int(bool(result.get("valid")))
            stats["sent"] += int(bool(result.get("sent")))
            stats["errors"] += int(bool(result.get("error")))

            stage = str(result.get("rejection_stage") or "")
            if stage == "TECHNICAL":
                stats["rejected_setup"] += 1
            elif stage == "FUTURES":
                stats["rejected_futures"] += 1
            elif stage == "BTC":
                stats["rejected_btc"] += 1
            elif stage == "QUOTE":
                stats["rejected_quote"] += 1
            elif stage == "FRESHNESS":
                stats["rejected_freshness"] += 1
            elif stage == "EXECUTION_QUALITY":
                stats["rejected_execution_quality"] += 1
            elif stage:
                stats["rejected_final"] += 1

            stats["technical_candidates"] += int(bool(result.get("technical_candidate")))

        self._aggregate_gate_diagnostics(results)

        LOGGER.info(
            "MEXC scan complete: symbols=%s valid=%s sent=%s errors=%s "
            "technical_candidates=%s rejected_setup=%s rejected_btc=%s "
            "rejected_futures=%s rejected_quote=%s rejected_freshness=%s "
            "rejected_execution_quality=%s rejected_final=%s",
            stats["symbols"], stats["valid"], stats["sent"], stats["errors"],
            stats["technical_candidates"], stats["rejected_setup"],
            stats["rejected_btc"], stats["rejected_futures"], stats["rejected_quote"],
            stats["rejected_freshness"], stats["rejected_execution_quality"],
            stats["rejected_final"],
        )
        return stats

    @staticmethod
    def _safe_gate_key(name: str) -> str:
        return (
            str(name).lower()
            .replace(">=", "gte")
            .replace("->", "_to_")
            .replace(" ", "_")
            .replace("-", "_")
            .replace(".", "")
            .replace("/", "_")
        )

    def _aggregate_gate_diagnostics(self, results: list[Any]) -> None:
        gate_names = (
            "4H regime",
            "1H alignment",
            "15M BOS",
            "15M post-BOS retest",
            "5M trigger",
            "BOS->retest->5M sequence",
            "setup freshness",
            "target path",
            "entry distance",
            "SL ATR",
            "RR >= 2.0",
            "volatility",
            "momentum",
            "volume/RVOL",
        )

        counts = {
            "LONG": {gate: 0 for gate in gate_names},
            "SHORT": {gate: 0 for gate in gate_names},
        }

        # The current engine exposes full side-specific gate maps. Count every
        # failed gate, not only the first failure, so the Render logs reveal the
        # actual bottleneck without weakening any rule.
        for result in results:
            if not isinstance(result, dict):
                continue
            analysis = result.get("analysis") or {}
            for side in ("LONG", "SHORT"):
                status = analysis.get(f"{side.lower()}_gate_status") or {}
                for gate in gate_names:
                    if gate in status and not bool(status[gate]):
                        counts[side][gate] += 1

        for side in ("LONG", "SHORT"):
            compact = " | ".join(
                f"{gate}={counts[side][gate]}" for gate in gate_names
            )
            LOGGER.info("MEXC GATE DIAGNOSTICS | %s | %s", side, compact)

        # Break the 4H and 1H gates into their individual components. These
        # values diagnose why the strict higher-timeframe regime is failing.
        four_h_map = {
            "LONG": {
                "close_above_ema200": "4H_LONG_close_above_ema200",
                "ema_stack_21_50_100_200": "4H_LONG_ema_stack",
                "ema50_slope_positive": "4H_LONG_ema50_slope_positive",
                "adx_at_least_25": "4H_LONG_adx",
                "protected_low_exists": "4H_LONG_protected_low_exists",
                "protected_low_intact": "4H_LONG_protected_low_intact",
            },
            "SHORT": {
                "close_below_ema200": "4H_SHORT_close_below_ema200",
                "ema_stack_21_50_100_200": "4H_SHORT_ema_stack",
                "ema50_slope_negative": "4H_SHORT_ema50_slope_negative",
                "adx_at_least_25": "4H_SHORT_adx",
                "protected_high_exists": "4H_SHORT_protected_high_exists",
                "protected_high_intact": "4H_SHORT_protected_high_intact",
            },
        }
        one_h_map = {
            "LONG": {
                "4h_bullish_regime": "1H_LONG_4H_bullish_regime",
                "1h_hh_hl": "1H_LONG_HH_HL",
                "1h_close_above_ema50": "1H_LONG_close_above_ema50",
                "1h_ema21_at_or_above_ema50": "1H_LONG_ema21_ge_ema50",
            },
            "SHORT": {
                "4h_bearish_regime": "1H_SHORT_4H_bearish_regime",
                "1h_lh_ll": "1H_SHORT_LH_LL",
                "1h_close_below_ema50": "1H_SHORT_close_below_ema50",
                "1h_ema21_at_or_below_ema50": "1H_SHORT_ema21_le_ema50",
            },
        }

        component_counts = {}
        for mapping in (*four_h_map.values(), *one_h_map.values()):
            component_counts.update({value: 0 for value in mapping.values()})

        for result in results:
            if not isinstance(result, dict):
                continue
            analysis = result.get("analysis") or {}

            failed_4h = analysis.get("four_h_failed_components") or {}
            for side in ("LONG", "SHORT"):
                for component in failed_4h.get(side) or []:
                    key = four_h_map[side].get(component)
                    if key:
                        component_counts[key] += 1

            failed_1h = analysis.get("one_h_failed_components") or {}
            for side in ("LONG", "SHORT"):
                for component in failed_1h.get(side) or []:
                    key = one_h_map[side].get(component)
                    if key:
                        component_counts[key] += 1

        LOGGER.info(
            "MEXC 4H/1H SUBGATE DIAGNOSTICS | "
            "4H L(close/stack/slope/adx/protected/intact)=%s/%s/%s/%s/%s/%s | "
            "4H S(close/stack/slope/adx/protected/intact)=%s/%s/%s/%s/%s/%s | "
            "1H L(4H/HHHL/close/EMA21)=%s/%s/%s/%s | "
            "1H S(4H/LHLL/close/EMA21)=%s/%s/%s/%s",
            component_counts["4H_LONG_close_above_ema200"],
            component_counts["4H_LONG_ema_stack"],
            component_counts["4H_LONG_ema50_slope_positive"],
            component_counts["4H_LONG_adx"],
            component_counts["4H_LONG_protected_low_exists"],
            component_counts["4H_LONG_protected_low_intact"],
            component_counts["4H_SHORT_close_below_ema200"],
            component_counts["4H_SHORT_ema_stack"],
            component_counts["4H_SHORT_ema50_slope_negative"],
            component_counts["4H_SHORT_adx"],
            component_counts["4H_SHORT_protected_high_exists"],
            component_counts["4H_SHORT_protected_high_intact"],
            component_counts["1H_LONG_4H_bullish_regime"],
            component_counts["1H_LONG_HH_HL"],
            component_counts["1H_LONG_close_above_ema50"],
            component_counts["1H_LONG_ema21_ge_ema50"],
            component_counts["1H_SHORT_4H_bearish_regime"],
            component_counts["1H_SHORT_LH_LL"],
            component_counts["1H_SHORT_close_below_ema50"],
            component_counts["1H_SHORT_ema21_le_ema50"],
        )

'''

pattern = r"    async def scan_once\(self\).*?(?=\n    async def _refresh_btc_context)"
patched, n = re.subn(pattern, new_scan_once.rstrip(), text, count=1, flags=re.S)
if n != 1:
    raise RuntimeError(f"scan_once replacement failed: {n}")

out.write_text(patched)
py_compile.compile(str(out), doraise=True)
print(f"Created {out}")
print(f"Lines: {len(patched.splitlines())}")
print("Syntax: OK")

---------------------------------------------------------------------------
PermissionError                           Traceback (most recent call last)
Cell In[7], line 228
    224 if n != 1:
    225     raise RuntimeError(f"scan_once replacement failed: {n}")
    226 
    227 out.write_text(patched)
--> 228 py_compile.compile(str(out), doraise=True)
    229 print(f"Created {out}")
    230 print(f"Lines: {len(patched.splitlines())}")
    231 print("Syntax: OK")

File /usr/lib/python3.13/py_compile.py:172, in compile(file, cfile, dfile, doraise, optimize, invalidation_mode, quiet)
    166     bytecode = importlib._bootstrap_external._code_to_hash_pyc(
    167         code,
    168         source_hash,
    169         (invalidation_mode == PycInvalidationMode.CHECKED_HASH),
    170     )
    171 mode = importlib._bootstrap_external._calc_mode(file)
--> 172 importlib._bootstrap_external._write_atomic(cfile, bytecode, mode)
    173 return cfile

File <frozen importlib._bootstrap_external>:206, in _write_atomic(path, data, mode)

PermissionError: [Errno 13] Permission denied: '/mnt/data/__pycache__/PAK_MEXC_Gold_V1_4_scanner_diagnostics.cpython-313.pyc.140421165995904'

Analyzed
from pathlib import Path
import ast

out = Path("/mnt/data/PAK_MEXC_Gold_V1_4_scanner_diagnostics.py")
source = out.read_text()
ast.parse(source)
print(f"Created: {out}")
print(f"Lines: {len(source.splitlines())}")
print("Syntax check: OK")

STDOUT/STDERR
Created: /mnt/data/PAK_MEXC_Gold_V1_4_scanner_diagnostics.py
Lines: 529
Syntax check: OK


Download the updated scanner.py

Next: replace app/automation/scanner.py with this file → commit → deploy Render. 

PAK_MEXC_Gold_V1_4_scanner_diagnostics.py
Code
Pasted text(20260925-210016).txt
Document
now are you sure this is the final version of engine> because im tired of changing

Yes. The current engine.py is the final V1.4 engine version for now. It already contains:

Detailed gate diagnostics
Strict 4H regime with full EMA stack, ADX, slope, and protected structure
Strict BOS → post-BOS retest → 5M trigger chronology
Structural TP1/TP2 only, with TP2 ≥ 2R
0.50–1.80 ATR SL limits and the existing score thresholds 82 / 5 families
No loosening of the gates.

Do not replace engine.py again.

Next: commit the scanner diagnostic version, deploy Render, and send me the new scan logs.

from future import annotations

import asyncio
import logging
import time
from typing import Any

from ..analysis.engine import (
analyze_candles,
btc_filter_ok,
build_btc_context,
closed_candle_rows,
)
from ..config import Settings
from .executor import MexcExecutor
from .mexc_client import MexcClient
from .signal_manager import SignalManager
from .signal_validator import validate_signal
from .universe import MexcUniverse

LOGGER = logging.getLogger(name)

MEXC_INTERVALS = {"4H": "Hour4", "1H": "Min60", "15M": "Min15", "5M": "Min5", "1D": "Day1"}

class MexcScanner:
"""Deterministic MEXC Futures scanner with technical + live-context gates."""

def __init__(self, *, settings: Settings, client: MexcClient, universe: MexcUniverse, signal_manager: SignalManager, executor: MexcExecutor | None = None) -> None:
    self.settings = settings
    self.client = client
    self.universe = universe
    self.signal_manager = signal_manager
    self.executor = executor
    self._btc_context: dict[str, Any] = {"ok": False, "reason": "not loaded"}

async def scan_once(self) -> dict[str, int]:
    symbols = await self.universe.refresh()
    if not symbols:
        return {"symbols": 0, "valid": 0, "sent": 0, "errors": 0}

    await self._refresh_btc_context()
    concurrency = max(1, int(getattr(self.settings, "scan_concurrency", 8)))
    semaphore = asyncio.Semaphore(concurrency)
    results = await asyncio.gather(
        *(self._scan_one(symbol, semaphore) for symbol in symbols),
        return_exceptions=True,
    )

    stats: dict[str, int] = {
        "symbols": len(symbols),
        "valid": 0,
        "sent": 0,
        "errors": 0,
        "technical_candidates": 0,
        "rejected_setup": 0,
        "rejected_futures": 0,
        "rejected_final": 0,
        "rejected_btc": 0,
        "rejected_quote": 0,
        "rejected_execution_quality": 0,
        "rejected_freshness": 0,
    }

    for result in results:
        if isinstance(result, Exception):
            stats["errors"] += 1
            continue
        if not isinstance(result, dict):
            continue

        stats["valid"] += int(bool(result.get("valid")))
        stats["sent"] += int(bool(result.get("sent")))
        stats["errors"] += int(bool(result.get("error")))

        stage = str(result.get("rejection_stage") or "")
        if stage == "TECHNICAL":
            stats["rejected_setup"] += 1
        elif stage == "FUTURES":
            stats["rejected_futures"] += 1
        elif stage == "BTC":
            stats["rejected_btc"] += 1
        elif stage == "QUOTE":
            stats["rejected_quote"] += 1
        elif stage == "FRESHNESS":
            stats["rejected_freshness"] += 1
        elif stage == "EXECUTION_QUALITY":
            stats["rejected_execution_quality"] += 1
        elif stage:
            stats["rejected_final"] += 1

        stats["technical_candidates"] += int(bool(result.get("technical_candidate")))

    self._aggregate_gate_diagnostics(results)

    LOGGER.info(
        "MEXC scan complete: symbols=%s valid=%s sent=%s errors=%s "
        "technical_candidates=%s rejected_setup=%s rejected_btc=%s "
        "rejected_futures=%s rejected_quote=%s rejected_freshness=%s "
        "rejected_execution_quality=%s rejected_final=%s",
        stats["symbols"], stats["valid"], stats["sent"], stats["errors"],
        stats["technical_candidates"], stats["rejected_setup"],
        stats["rejected_btc"], stats["rejected_futures"], stats["rejected_quote"],
        stats["rejected_freshness"], stats["rejected_execution_quality"],
        stats["rejected_final"],
    )
    return stats

@staticmethod
def _safe_gate_key(name: str) -> str:
    return (
        str(name).lower()
        .replace(">=", "gte")
        .replace("->", "_to_")
        .replace(" ", "_")
        .replace("-", "_")
        .replace(".", "")
        .replace("/", "_")
    )

def _aggregate_gate_diagnostics(self, results: list[Any]) -> None:
    gate_names = (
        "4H regime",
        "1H alignment",
        "15M BOS",
        "15M post-BOS retest",
        "5M trigger",
        "BOS->retest->5M sequence",
        "setup freshness",
        "target path",
        "entry distance",
        "SL ATR",
        "RR >= 2.0",
        "volatility",
        "momentum",
        "volume/RVOL",
    )

    counts = {
        "LONG": {gate: 0 for gate in gate_names},
        "SHORT": {gate: 0 for gate in gate_names},
    }

    # The current engine exposes full side-specific gate maps. Count every
    # failed gate, not only the first failure, so the Render logs reveal the
    # actual bottleneck without weakening any rule.
    for result in results:
        if not isinstance(result, dict):
            continue
        analysis = result.get("analysis") or {}
        for side in ("LONG", "SHORT"):
            status = analysis.get(f"{side.lower()}_gate_status") or {}
            for gate in gate_names:
                if gate in status and not bool(status[gate]):
                    counts[side][gate] += 1

    for side in ("LONG", "SHORT"):
        compact = " | ".join(
            f"{gate}={counts[side][gate]}" for gate in gate_names
        )
        LOGGER.info("MEXC GATE DIAGNOSTICS | %s | %s", side, compact)

    # Break the 4H and 1H gates into their individual components. These
    # values diagnose why the strict higher-timeframe regime is failing.
    four_h_map = {
        "LONG": {
            "close_above_ema200": "4H_LONG_close_above_ema200",
            "ema_stack_21_50_100_200": "4H_LONG_ema_stack",
            "ema50_slope_positive": "4H_LONG_ema50_slope_positive",
            "adx_at_least_25": "4H_LONG_adx",
            "protected_low_exists": "4H_LONG_protected_low_exists",
            "protected_low_intact": "4H_LONG_protected_low_intact",
        },
        "SHORT": {
            "close_below_ema200": "4H_SHORT_close_below_ema200",
            "ema_stack_21_50_100_200": "4H_SHORT_ema_stack",
            "ema50_slope_negative": "4H_SHORT_ema50_slope_negative",
            "adx_at_least_25": "4H_SHORT_adx",
            "protected_high_exists": "4H_SHORT_protected_high_exists",
            "protected_high_intact": "4H_SHORT_protected_high_intact",
        },
    }
    one_h_map = {
        "LONG": {
            "4h_bullish_regime": "1H_LONG_4H_bullish_regime",
            "1h_hh_hl": "1H_LONG_HH_HL",
            "1h_close_above_ema50": "1H_LONG_close_above_ema50",
            "1h_ema21_at_or_above_ema50": "1H_LONG_ema21_ge_ema50",
        },
        "SHORT": {
            "4h_bearish_regime": "1H_SHORT_4H_bearish_regime",
            "1h_lh_ll": "1H_SHORT_LH_LL",
            "1h_close_below_ema50": "1H_SHORT_close_below_ema50",
            "1h_ema21_at_or_below_ema50": "1H_SHORT_ema21_le_ema50",
        },
    }

    component_counts = {}
    for mapping in (*four_h_map.values(), *one_h_map.values()):
        component_counts.update({value: 0 for value in mapping.values()})

    for result in results:
        if not isinstance(result, dict):
            continue
        analysis = result.get("analysis") or {}

        failed_4h = analysis.get("four_h_failed_components") or {}
        for side in ("LONG", "SHORT"):
            for component in failed_4h.get(side) or []:
                key = four_h_map[side].get(component)
                if key:
                    component_counts[key] += 1

        failed_1h = analysis.get("one_h_failed_components") or {}
        for side in ("LONG", "SHORT"):
            for component in failed_1h.get(side) or []:
                key = one_h_map[side].get(component)
                if key:
                    component_counts[key] += 1

    LOGGER.info(
        "MEXC 4H/1H SUBGATE DIAGNOSTICS | "
        "4H L(close/stack/slope/adx/protected/intact)=%s/%s/%s/%s/%s/%s | "
        "4H S(close/stack/slope/adx/protected/intact)=%s/%s/%s/%s/%s/%s | "
        "1H L(4H/HHHL/close/EMA21)=%s/%s/%s/%s | "
        "1H S(4H/LHLL/close/EMA21)=%s/%s/%s/%s",
        component_counts["4H_LONG_close_above_ema200"],
        component_counts["4H_LONG_ema_stack"],
        component_counts["4H_LONG_ema50_slope_positive"],
        component_counts["4H_LONG_adx"],
        component_counts["4H_LONG_protected_low_exists"],
        component_counts["4H_LONG_protected_low_intact"],
        component_counts["4H_SHORT_close_below_ema200"],
        component_counts["4H_SHORT_ema_stack"],
        component_counts["4H_SHORT_ema50_slope_negative"],
        component_counts["4H_SHORT_adx"],
        component_counts["4H_SHORT_protected_high_exists"],
        component_counts["4H_SHORT_protected_high_intact"],
        component_counts["1H_LONG_4H_bullish_regime"],
        component_counts["1H_LONG_HH_HL"],
        component_counts["1H_LONG_close_above_ema50"],
        component_counts["1H_LONG_ema21_ge_ema50"],
        component_counts["1H_SHORT_4H_bearish_regime"],
        component_counts["1H_SHORT_LH_LL"],
        component_counts["1H_SHORT_close_below_ema50"],
        component_counts["1H_SHORT_ema21_le_ema50"],
    )
async def _refresh_btc_context(self) -> None:
    try:
        raw4, raw1, raw15 = await asyncio.gather(
            self.client.get_klines("BTC_USDT", MEXC_INTERVALS["4H"], 250),
            self.client.get_klines("BTC_USDT", MEXC_INTERVALS["1H"], 250),
            self.client.get_klines("BTC_USDT", MEXC_INTERVALS["15M"], 250),
        )
        c4 = closed_candle_rows(raw4, "4h")
        c1 = closed_candle_rows(raw1, "1h")
        c15 = closed_candle_rows(raw15, "15m")
        if len(c4) < 205 or len(c1) < 205 or len(c15) < 80:
            self._btc_context = {"ok": False, "reason": "insufficient BTC history"}
            return
        self._btc_context = build_btc_context(c4, c1, c15)
    except Exception as exc:
        LOGGER.warning("BTC market context unavailable: %s", exc)
        self._btc_context = {"ok": False, "reason": str(exc)}

async def _scan_one(self, symbol: str, semaphore: asyncio.Semaphore) -> dict[str, Any]:
    async with semaphore:
        try:
            limit = max(250, int(getattr(self.settings, "candle_limit", 250)))
            raw4, raw1, raw15, raw5 = await asyncio.gather(
                self.client.get_klines(symbol, MEXC_INTERVALS["4H"], limit),
                self.client.get_klines(symbol, MEXC_INTERVALS["1H"], limit),
                self.client.get_klines(symbol, MEXC_INTERVALS["15M"], limit),
                self.client.get_klines(symbol, MEXC_INTERVALS["5M"], limit),
            )
            c4 = closed_candle_rows(raw4, "4h"); c1 = closed_candle_rows(raw1, "1h"); c15 = closed_candle_rows(raw15, "15m"); c5 = closed_candle_rows(raw5, "5m")
            for candles, minimum, label in ((c4, 205, "4H"), (c1, 205, "1H"), (c15, 80, "15M"), (c5, 30, "5M")):
                if len(candles) < minimum:
                    return self._reject(symbol, f"Insufficient closed {label} candles", stage="DATA", analysis={})

            # 1D is fetched only after the core 4H/1H/15M/5M data are usable.
            raw1d = await self.client.get_klines(symbol, MEXC_INTERVALS["1D"], 60)
            c1d = closed_candle_rows(raw1d, "1d")
            analysis = analyze_candles(symbol, c4, c1, c15, c5, c1d)
            analysis.update({"mexc_4h_rows": c4, "mexc_1h_rows": c1, "mexc_15m_rows": c15, "mexc_5m_rows": c5, "mexc_1d_rows": c1d, "closed_4h_candles": len(c4), "closed_1h_candles": len(c1), "closed_15m_candles": len(c15), "closed_5m_candles": len(c5), "closed_5m_candle_time": int(c5[-1]["time"])})

            setup = str(analysis.get("setup") or "NO TRADE").upper()
            LOGGER.info(
                "MEXC ANALYSIS | %s | setup=%s | score=%s | rr=%s | "
                "stage=%s | failures=%s | bos(L/S)=%s/%s | retest(L/S)=%s/%s | "
                "trigger=%s",
                symbol, setup, analysis.get("score"), analysis.get("rr"),
                analysis.get("rejection_stage") or "CANDIDATE",
                analysis.get("technical_gate_failures", []),
                analysis.get("long_bos_event_count", 0),
                analysis.get("short_bos_event_count", 0),
                analysis.get("long_retest", False),
                analysis.get("short_retest", False),
                analysis.get("trigger_5m", "NONE"),
            )
            if setup not in {"LONG", "SHORT"}:
                return self._reject(symbol, "Analysis engine produced no valid LONG/SHORT setup", stage="TECHNICAL", analysis=analysis)

            # BTC/global filter is evaluated before spending extra live-context calls.
            btc_ok, btc_reason = btc_filter_ok(setup, self._btc_context, is_btc=(symbol.upper() == "BTC_USDT"))
            analysis["btc_filter_ok"] = btc_ok; analysis["btc_filter_reason"] = btc_reason; analysis["btc_context"] = self._btc_context
            if not btc_ok:
                return self._reject(symbol, btc_reason, stage="BTC", analysis=analysis)

            ticker = await self.client.get_ticker(symbol)
            if not ticker:
                return self._reject(symbol, "Missing MEXC ticker", stage="QUOTE", analysis=analysis)
            now_ms = int(time.time() * 1000)
            ts = self._safe_int(ticker.get("timestamp") or ticker.get("ts") or ticker.get("time"))
            if 0 < ts < 10**12:
                ts *= 1000
            max_age_ms = int(float(getattr(self.settings, "max_data_age_seconds", 5.0)) * 1000)
            data_fresh = bool(ts > 0 and abs(now_ms - ts) <= max_age_ms)
            analysis["ticker_timestamp"] = ts; analysis["data_fresh"] = data_fresh
            if not data_fresh:
                return self._reject(symbol, "MEXC ticker is stale", stage="FRESHNESS", analysis=analysis)

            bid = self._safe_float(ticker.get("bid1") or ticker.get("bidPrice") or ticker.get("bid"))
            ask = self._safe_float(ticker.get("ask1") or ticker.get("askPrice") or ticker.get("ask"))
            last = self._safe_float(ticker.get("lastPrice") or ticker.get("last") or ticker.get("price"))
            if bid <= 0 or ask <= 0 or ask < bid:
                return self._reject(symbol, "Invalid MEXC bid/ask", stage="QUOTE", analysis=analysis)
            executable = ask if setup == "LONG" else bid
            mid = (bid + ask) / 2.0
            spread = abs(ask - bid) / mid if mid > 0 else 999.0
            analysis.update({"mexc_bid": bid, "mexc_ask": ask, "mexc_last": last, "mexc_spread_pct": spread})
            if spread > float(getattr(self.settings, "max_mexc_spread_pct", 0.001)):
                return self._reject(symbol, "MEXC spread too high", stage="EXECUTION_QUALITY", analysis=analysis)

            index_price = self._safe_float(ticker.get("indexPrice") or ticker.get("index"))
            fair_price = self._safe_float(ticker.get("fairPrice") or ticker.get("fair") or ticker.get("markPrice"))
            funding = self._safe_float_or_none(ticker.get("fundingRate"))
            if index_price <= 0:
                index_data = await self._safe_call(self.client.get_index_price, symbol)
                index_price = self._safe_float(index_data.get("indexPrice") or index_data.get("index")) if index_data else 0.0
            if fair_price <= 0:
                fair_data = await self._safe_call(self.client.get_fair_price, symbol)
                fair_price = self._safe_float(fair_data.get("fairPrice") or fair_data.get("fair")) if fair_data else 0.0
            if funding is None:
                funding_data = await self._safe_call(self.client.get_funding_rate, symbol)
                funding = self._safe_float_or_none((funding_data or {}).get("fundingRate") or (funding_data or {}).get("rate"))
            reference = fair_price if fair_price > 0 else index_price
            if reference <= 0:
                return self._reject(symbol, "Missing MEXC index/fair reference", stage="EXECUTION_QUALITY", analysis=analysis)
            dislocation = abs(executable - reference) / reference
            analysis.update({"mexc_index_price": index_price, "mexc_fair_price": fair_price, "mexc_funding_rate": funding, "mexc_reference_dislocation_pct": dislocation})
            if dislocation > float(getattr(self.settings, "max_index_dislocation_pct", 0.002)):
                return self._reject(symbol, "MEXC executable price is too far from index/fair", stage="EXECUTION_QUALITY", analysis=analysis)

            depth, deals = await asyncio.gather(self._safe_call(self.client.get_depth, symbol, int(getattr(self.settings, "orderbook_levels", 10))), self._safe_call(self.client.get_deals, symbol, int(getattr(self.settings, "trade_flow_limit", 100))))
            if depth:
                analysis.update(self._calculate_depth(depth))
            if deals:
                analysis.update(self._calculate_trade_flow(deals))
            analysis["hold_vol"] = self._safe_float(ticker.get("holdVol") or ticker.get("holdVolume"))
            analysis["funding_available"] = funding is not None

            futures_ok = self._futures_context_ok(analysis, setup)
            analysis["futures_ok"] = futures_ok
            analysis["futures_context"] = "AVAILABLE" if futures_ok else "INSUFFICIENT_DIRECTIONAL_CONFIRMATION"

            # Futures context is a 5-point supporting family. Missing or
            # non-directional order-flow data must not erase a technically
            # valid setup before the final validator.
            self._update_confirmation_families(analysis)
            analysis["score"], analysis["score_groups"] = self._recalculate_score(analysis)

            planned_entry = self._safe_float(analysis.get("entry"))
            if planned_entry <= 0:
                return self._reject(symbol, "Invalid planned entry", stage="LEVELS", analysis=analysis)
            drift = abs(executable - planned_entry) / planned_entry
            analysis["entry_drift_pct"] = drift
            if drift > float(getattr(self.settings, "max_entry_drift_pct", 0.002)):
                return self._reject(symbol, "Executable entry drift exceeds limit", stage="EXECUTION_QUALITY", analysis=analysis)
            self._reprice_levels(analysis, executable)
            self._update_confirmation_families(analysis)
            analysis["score"], analysis["score_groups"] = self._recalculate_score(analysis)
            analysis["max_entry_drift_pct"] = float(getattr(self.settings, "max_entry_drift_pct", 0.002))
            analysis["max_signal_age_seconds"] = float(getattr(self.settings, "max_signal_age_seconds", 330.0))

            validated, reasons = validate_signal(analysis, min_confluence=int(getattr(self.settings, "min_confluence", 82)), min_rr=float(getattr(self.settings, "min_rr", 2.0)), require_increasing_volume=bool(getattr(self.settings, "require_increasing_volume", False)))
            if validated is None:
                return self._reject(symbol, reasons, stage="FINAL_VALIDATOR", analysis=analysis)

            sent = False
            if bool(getattr(self.settings, "auto_signal_enabled", False)):
                sent = await self._publish_signal(validated)
            # Executor remains hard-disabled by its own class gate.
            if bool(getattr(self.settings, "auto_trade_enabled", False)) and bool(getattr(self.settings, "allow_live_execution", False)) and self.executor is not None:
                await self._execute_signal(validated)
            return {"valid": True, "sent": sent, "error": False, "symbol": symbol, "analysis": analysis, "signal": validated, "technical_candidate": True}

        except Exception as exc:
            LOGGER.exception("MEXC scan failed for %s", symbol)
            return {"valid": False, "sent": False, "error": True, "symbol": symbol, "reason": str(exc), "rejection_stage": "ERROR"}

@staticmethod
def _futures_context_ok(analysis: dict[str, Any], side: str) -> bool:
    """Return True only when live futures flow has directional agreement.

    Funding availability is contextual data, not a directional vote.
    """
    imbalance = float(analysis.get("orderbook_imbalance", 0.0) or 0.0)
    flow = float(analysis.get("volume_delta_ratio", 0.0) or 0.0)
    threshold = 0.05
    if side == "LONG":
        return imbalance >= threshold or flow >= threshold
    if side == "SHORT":
        return imbalance <= -threshold or flow <= -threshold
    return False

@staticmethod
def _update_confirmation_families(analysis: dict[str, Any]) -> None:
    families = ("direction_ok", "structure_ok", "setup_ok", "momentum_ok", "volume_ok", "location_ok")
    analysis["confirmation_family_count"] = sum(bool(analysis.get(name, False)) for name in families)

@staticmethod
def _recalculate_score(analysis: dict[str, Any]) -> tuple[int, dict[str, int]]:
    groups = {
        "direction_regime": 20 if analysis.get("direction_ok") else 0,
        "market_structure": 20 if analysis.get("structure_ok") else 0,
        "setup_entry_trigger": 20 if analysis.get("setup_ok") else 0,
        "momentum": 10 if analysis.get("momentum_ok") else 0,
        "volume_participation": 10 if analysis.get("volume_ok") else 0,
        "location_target_path": 10 if analysis.get("location_ok") else 0,
        "futures_market_context": 5 if analysis.get("futures_ok") else 0,
        "volatility_execution": 5 if analysis.get("volatility_ok") else 0,
    }
    setup_q = float(analysis.get("trigger_quality_5m", 0.0) or 0.0)
    bos_q = float(analysis.get("bos_15m_strength", 0.0) or 0.0)
    ret_q = float((analysis.get("retest") or {}).get("quality", 0.0) or 0.0)
    quality = 0.50 * setup_q + 0.25 * bos_q + 0.25 * ret_q
    if groups["setup_entry_trigger"] and quality < 0.60:
        groups["setup_entry_trigger"] -= 5
    if groups["volume_participation"] and float(analysis.get("rvol_15m", 0.0) or 0.0) < 1.25:
        groups["volume_participation"] -= 2
    return max(0, min(100, sum(groups.values()))), groups

@staticmethod
def _safe_float(value: Any) -> float:
    try: return float(value) if value is not None else 0.0
    except (TypeError, ValueError): return 0.0

@staticmethod
def _safe_float_or_none(value: Any) -> float | None:
    try:
        x = float(value)
        return x if x == x else None
    except (TypeError, ValueError):
        return None

@staticmethod
def _safe_int(value: Any) -> int:
    try: return int(float(value)) if value is not None else 0
    except (TypeError, ValueError): return 0

@staticmethod
async def _safe_call(fn, *args):
    try:
        result = await fn(*args)
        return result
    except Exception as exc:
        LOGGER.debug("Optional MEXC context call failed: %s", exc)
        return None

@staticmethod
def _calculate_depth(orderbook: dict[str, Any]) -> dict[str, float]:
    bids = orderbook.get("bids") or []; asks = orderbook.get("asks") or []
    def total(levels: list[Any]) -> float:
        value = 0.0
        for level in levels:
            try:
                if isinstance(level, dict): qty = level.get("quantity") or level.get("qty") or level.get("volume") or level.get("v") or 0
                else: qty = level[1] if len(level) > 1 else 0
                value += float(qty)
            except Exception: continue
        return value
    bid_depth = total(bids); ask_depth = total(asks); total_depth = bid_depth + ask_depth
    return {"bid_depth": bid_depth, "ask_depth": ask_depth, "orderbook_imbalance": (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0}

@staticmethod
def _calculate_trade_flow(deals: list[Any]) -> dict[str, float]:
    buy = 0.0; sell = 0.0
    for deal in deals:
        if not isinstance(deal, dict): continue
        try:
            qty = float(deal.get("v") or deal.get("volume") or deal.get("vol") or deal.get("quantity") or 0)
            side = deal.get("T", deal.get("side", deal.get("type", "")))
            if str(side).lower() in {"1", "buy", "purchase", "bid"}: buy += qty
            elif str(side).lower() in {"2", "sell", "ask"}: sell += qty
        except Exception:
            continue
    total = buy + sell; delta = buy - sell
    return {"buy_volume": buy, "sell_volume": sell, "volume_delta": delta, "volume_delta_ratio": delta / total if total > 0 else 0.0}

@staticmethod
def _reprice_levels(analysis: dict[str, Any], executable_price: float) -> None:
    old_entry = float(analysis["entry"]); delta = executable_price - old_entry
    analysis["entry"] = executable_price
    analysis["stop_loss"] = float(analysis["stop_loss"]) + delta
    analysis["tp1"] = float(analysis["tp1"]) + delta
    analysis["tp2"] = float(analysis["tp2"]) + delta
    risk = abs(executable_price - float(analysis["stop_loss"]))
    reward = abs(float(analysis["tp2"]) - executable_price)
    analysis["rr"] = reward / risk if risk > 0 else 0.0

@staticmethod
def _reject(symbol: str, reason: Any, *, stage: str, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = analysis if analysis is not None else {}
    payload["rejection_stage"] = stage
    if isinstance(reason, list): payload["rejection_reasons"] = reason
    else: payload["rejection_reasons"] = [str(reason)]
    LOGGER.info("MEXC REJECT | %s | stage=%s | reason=%s | setup=%s | score=%s | rr=%s", symbol, stage, payload["rejection_reasons"], payload.get("setup", "NO TRADE"), payload.get("score"), payload.get("rr"))
    return {"valid": False, "sent": False, "error": False, "symbol": symbol, "reason": reason, "analysis": payload, "rejection_stage": stage, "technical_candidate": bool(payload.get("technical_candidate", False))}

async def _publish_signal(self, signal):
    try: return bool(await self.signal_manager.publish(signal))
    except Exception: LOGGER.exception("Signal publication failed for %s", signal.symbol); return False

async def _execute_signal(self, signal):
    if self.executor is None: return None
    meta = self.universe.get(signal.symbol) if hasattr(self.universe, "get") else None
    if meta is None: return None
    return await self.executor.execute(signal, meta)
Close
