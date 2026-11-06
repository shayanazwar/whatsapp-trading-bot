from __future__ import annotations

from math import isfinite
from typing import Any

from rslr.models import TradeSignal


def _money(value: float) -> str:
    return f"${value:,.4f}" if abs(value) < 1 else f"${value:,.2f}"


def format_signal(signal: TradeSignal) -> str:
    direction = "📈 LONG" if signal.side == "LONG" else "📉 SHORT"
    return (
        "🚨 *RSLR SIGNAL*\n\n"
        f"🪙 {signal.symbol}  {direction}\n"
        f"⚙️ Strategy: {signal.module}\n"
        f"🎯 Entry: {_money(signal.reference_entry)}\n"
        f"🛑 Stop: {_money(signal.stop_loss)}\n"
        f"💰 Take Profit: {_money(signal.take_profit)}\n\n"
        f"⚖️ Risk: {signal.risk_pct * 100:.3f}% ({_money(signal.risk_amount_usd)})\n"
        f"📊 Gross R:R: {signal.gross_rr:.2f}:1 | Net R:R est.: {signal.net_rr:.2f}:1\n"
        f"🧾 Fees: {_money(signal.estimated_fees_usd)} | Slippage: {_money(signal.estimated_slippage_usd)}\n"
        f"📌 BTC Bias: {signal.btc_bias}\n"
        "⏳ Trigger: completed 1H candle | Paper mode"
    )


def format_backtest(result: dict[str, Any]) -> str:
    def money(x: Any) -> str:
        return f"${float(x):+.2f}"

    hit = result.get("tp_hit_rate_pct")
    pf = result.get("profit_factor")
    avg_r = result.get("average_r")
    return (
        "🧪 *RSLR BACKTEST*\n\n"
        f"📅 Period: {result['period']} | 💰 Start: ${result['starting_equity']:.2f}\n"
        f"🔍 Coins tested: {result['coins_tested']} | 📡 Generated signals: {result['total_signals']}\n"
        f"🎟️ Trades entered: {result.get('trades_taken', result['total_signals'])}\n"
        f"🟢 LONG signals: {result['long_signals']} | 🔴 SHORT signals: {result['short_signals']}\n"
        f"✅ TP: {result['tp_hits']} | ❌ SL: {result['sl_hits']} | ⏳ Open: {result['open_unresolved']} | ⌛ Expired: {result['expired']}\n"
        f"🎯 TP hit rate: {f'{hit:.2f}%' if hit is not None else 'N/A'}\n"
        f"💵 Net P/L: {money(result['net_pnl_usd'])} | 📈 Return: {result['net_return_pct']:+.2f}%\n"
        f"📉 Max DD: {result['max_drawdown_pct']:.2f}% | ⚖️ PF: {f'{pf:.2f}' if pf is not None and isfinite(float(pf)) else 'N/A'}\n"
        f"🧾 Fees: ${result['total_fees_usd']:.4f} | Slippage: ${result['total_slippage_usd']:.4f} | Funding P/L: ${result['funding_pnl_usd']:.4f}\n"
        f"⚙️ Sweep: {result['sweep_signals']} ({money(result['sweep_net_pnl'])}) | Donchian: {result['donchian_signals']} ({money(result['donchian_net_pnl'])})\n"
        f"📐 Average R: {f'{avg_r:.2f}' if avg_r is not None else 'N/A'}\n"
        "⚠️ Historical simulation; not a profit guarantee."
    )
