from __future__ import annotations

import logging
import os
import time
import uuid

from rslr.config import settings
from rslr.formatting import format_signal
from rslr.jobs import process_backtest_jobs
from rslr.services import scan_market
from rslr.paper import reconcile_paper_positions
from rslr.storage import Database
from rslr.whatsapp import WhatsAppClient

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
log = logging.getLogger("rslr.worker")


def deliver_outbox(database: Database) -> None:
    whatsapp = WhatsAppClient()
    for item in database.pending_notifications(40):
        try:
            response = whatsapp.send_template(item["recipient"], item["payload"]["body"])
            message_id = None
            messages = response.get("messages") or []
            if messages and isinstance(messages[0], dict):
                message_id = messages[0].get("id")
            database.update_notification(item["id"], "SENT", external_message_id=message_id)
        except Exception as exc:
            status = "FAILED" if item["attempts"] >= 4 else "PENDING"
            database.update_notification(item["id"], status, str(exc))
            log.warning("Notification %s failed: %s", item["id"], exc)


def scheduled_scan(database: Database) -> None:
    owner = f"{os.getpid()}-{uuid.uuid4()}"
    if not database.acquire_lock("rslr-market-scan", owner, ttl_seconds=14400):
        log.info("Another scheduler instance owns the market scan lock")
        return
    try:
        signals = scan_market(database=database)
        try:
            reconciled = reconcile_paper_positions(database=database) if settings.paper_trading_enabled else 0
            if reconciled:
                log.info("Resolved %s paper positions", reconciled)
        except Exception:
            log.exception("Paper position reconciliation failed")
        if not signals:
            log.info("Scheduled scan produced no new signals")
            return
        if not settings.whatsapp_signal_template_name:
            log.info("Signal template not configured; signals are stored but proactive messages are skipped")
            return
        for signal in signals:
            payload = signal.to_dict()
            body = format_signal(signal)
            dedupe_key = f"{signal.symbol}|{signal.side}|{signal.signal_time.isoformat()}"
            for number in settings.whatsapp_allowed_numbers:
                database.enqueue_notification(number, dedupe_key + "|" + number, {"body": body, "signal": payload})
    finally:
        database.release_lock("rslr-market-scan", owner)


def main() -> None:
    database = Database()
    log.info("RSLR worker started. Live execution hard-locked: True")
    last_scan = 0.0
    while True:
        try:
            processed = process_backtest_jobs(database)
            if processed:
                log.info("Processed %s backtest jobs", processed)
            deliver_outbox(database)
            now = time.monotonic()
            if now - last_scan >= max(60, settings.scan_interval_seconds):
                last_scan = now
                scheduled_scan(database)
        except Exception:
            log.exception("Worker cycle failed")
        time.sleep(10)


if __name__ == "__main__":
    main()
