"""Persistent price, percentage-change and RSI crossing alerts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from app.market.candles import Candle
from app.market.indicators import rsi
from app.storage import Store


class AlertError(ValueError):
    pass


class AlertService:
    def __init__(self, store: Store) -> None:
        self.store = store

    def create(self, symbol: str, params: Mapping[str, Any]) -> str:
        kind = str(params.get("kind", ""))
        if kind not in {"price_cross", "percent_change", "rsi_cross"}:
            raise AlertError("Unsupported alert kind")
        return self.store.create_alert(symbol, kind, params)

    def list(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        return self.store.list_alerts(active_only=active_only)

    def delete(self, alert_id: str) -> bool:
        return self.store.delete_alert(alert_id)

    def set_active(self, alert_id: str, active: bool) -> bool:
        alerts = {a["id"]: a for a in self.store.list_alerts()}
        if alert_id not in alerts:
            return False
        return self.store.update_alert(alert_id, active=active)

    @staticmethod
    def _latest_metrics(alert: Mapping[str, Any], candles: Sequence[Candle], latest_price: float | None) -> tuple[bool, dict[str, Any], str, str] | None:
        params = alert["params"]
        kind = alert["kind"]
        bars = sorted((c for c in candles if c.complete), key=lambda c: c.open_time)
        if kind == "price_cross":
            if latest_price is None or latest_price <= 0:
                return None
            level = float(params["level"])
            current = latest_price >= level if params["direction"] == "ABOVE" else latest_price <= level
            state = {"condition_met": current, "observed_price": latest_price}
            key = f"price:{latest_price:.12g}"
            description = f"{alert['symbol']} price {latest_price:.8g} {params['direction'].lower()} {level:.8g}"
            return current, state, key, description
        if kind == "percent_change":
            if len(bars) < 2:
                return None
            previous, current = bars[-2].close, bars[-1].close
            if previous <= 0:
                return None
            change = (current / previous - 1.0) * 100
            threshold = float(params["threshold_pct"])
            current_condition = abs(change) >= threshold
            state = {"condition_met": current_condition, "change_pct": change, "bar_close_time": bars[-1].close_time}
            key = f"change:{bars[-1].close_time}"
            description = f"{alert['symbol']} {params['interval']} close changed {change:+.3f}% (threshold {threshold:.3f}%)"
            return current_condition, state, key, description
        if kind == "rsi_cross":
            if len(bars) < 16:
                return None
            values = rsi([c.close for c in bars], 14)
            current_rsi = values[-1]
            if current_rsi is None:
                return None
            threshold = float(params["threshold"])
            condition = current_rsi >= threshold if params["direction"] == "ABOVE" else current_rsi <= threshold
            state = {"condition_met": condition, "rsi14": current_rsi, "bar_close_time": bars[-1].close_time}
            key = f"rsi:{bars[-1].close_time}"
            desc = f"{alert['symbol']} {params['interval']} RSI(14)={current_rsi:.2f} {params['direction'].lower()} {threshold:g}"
            return condition, state, key, desc
        return None

    def evaluate_symbol(
        self,
        symbol: str,
        *,
        candles_by_interval: Mapping[str, Sequence[Candle]],
        latest_price: float | None,
        recipient: str | Sequence[str],
    ) -> list[dict[str, Any]]:
        """Evaluate once and enqueue each transition for all recipients.

        Accepting multiple recipients prevents the first recipient from consuming the
        false->true state transition before the remaining recipients are notified.
        """
        recipients = list(dict.fromkeys([recipient] if isinstance(recipient, str) else recipient))
        recipients = [str(x).strip() for x in recipients if str(x).strip()]
        fired: list[dict[str, Any]] = []
        for alert in self.store.list_alerts(active_only=True):
            if alert["symbol"] != symbol:
                continue
            interval = alert["params"].get("interval", "1H")
            series = candles_by_interval.get(interval, ())
            computed = self._latest_metrics(alert, series, latest_price)
            if computed is None:
                continue
            condition, state, trigger_key, description = computed
            previous = alert.get("last_state") or {}
            previous_condition = previous.get("condition_met")
            is_new_trigger = bool(condition and previous_condition is False)
            # Do not fire a threshold alert on its first observation if the condition was already true.
            should_notify = is_new_trigger
            if should_notify:
                transition_key = f"alert:{alert['id']}:{trigger_key}"
                for target in recipients:
                    self.store.enqueue_outbox(target, f"ALERT TRIGGERED [{alert['id']}]\n{description}",
                        category="utility", idempotency_key=f"{transition_key}:{target}")
                self.store.append_event("ALERT_TRIGGERED", {
                    "alert_id": alert["id"], "description": description, "state": state,
                    "recipient_count": len(recipients),
                }, symbol=symbol, idempotency_key=transition_key)
                fired.append({"id": alert["id"], "description": description,
                              "trigger_key": trigger_key, "recipient_count": len(recipients)})
            self.store.update_alert(alert["id"], last_state=state,
                trigger_at=datetime.now(timezone.utc).isoformat() if should_notify else None,
                trigger_key=trigger_key if should_notify else None)
        return fired
