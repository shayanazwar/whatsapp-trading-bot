from __future__ import annotations


def fmt_price(value: float) -> str:
    """Human-readable price formatting used across WhatsApp messages."""
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if abs(value) >= 0.01:
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    return f"{value:.10f}".rstrip("0").rstrip(".")
