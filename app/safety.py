from __future__ import annotations

from app.config import Settings
from app.database import Database
from app.models import Candle


class AbnormalMoveGate:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    def update(self, candles: list[Candle], now: int) -> tuple[bool, str]:
        if len(candles) < 6:
            return True, "insufficient volatility history"
        five_min_change = abs((candles[-1].close / candles[-6].close - 1.0) * 100.0)
        last = candles[-1]
        one_min_range = ((last.high - last.low) / last.close) * 100.0
        paused = self.db.get("abnormal_paused", "0") == "1"
        if not paused:
            reason = None
            if five_min_change >= self.settings.abnormal_5m_move_pct:
                reason = f"5m move {five_min_change:.3f}%"
            elif one_min_range >= self.settings.abnormal_1m_range_pct:
                reason = f"1m range {one_min_range:.3f}%"
            if reason:
                self.db.set("abnormal_paused", "1", now)
                self.db.set("recovery_start", "0", now)
                self.db.set("abnormal_reason", reason, now)
                return True, reason
            return False, "normal"

        if (five_min_change < self.settings.recovery_5m_move_pct
                and one_min_range < self.settings.abnormal_1m_range_pct):
            recovery_start = int(self.db.get("recovery_start", "0") or 0)
            if recovery_start == 0:
                self.db.set("recovery_start", now, now)
                return True, "recovery observation started"
            if now - recovery_start >= self.settings.recovery_minutes * 60:
                self.db.set("abnormal_paused", "0", now)
                self.db.set("recovery_start", "0", now)
                self.db.set("abnormal_reason", "", now)
                return False, "recovered"
            return True, "recovery window not complete"
        self.db.set("recovery_start", "0", now)
        return True, "abnormal movement continues"
