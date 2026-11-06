from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from rslr.backtest import run_backtest
from rslr.formatting import format_backtest
from rslr.storage import Database
from rslr.whatsapp import WhatsAppClient

log = logging.getLogger(__name__)


def process_backtest_jobs(
    database: Database | None = None,
    whatsapp: WhatsAppClient | None = None,
    backtest_fn: Callable[[str], Any] | None = None,
) -> int:
    """Claim and run every PENDING backtest job.

    Runs in the worker process (see rslr.worker), never in the webhook
    request path. Each job stores the real backtest result and reports
    completion (or failure, with the actual error) to the requester on
    WhatsApp. Returns the number of jobs processed.
    """
    database = database or Database()
    backtest_fn = backtest_fn or run_backtest
    processed = 0
    while True:
        job = database.claim_backtest_job()
        if job is None:
            return processed
        job_id = job["id"]
        period = job["period"]
        sender = job["sender"]
        log.info("Backtest job #%s (%s) started for sender ****%s", job_id, period, sender[-4:])
        try:
            result = backtest_fn(period)
            payload = result.to_dict()
            run_id = database.save_backtest(payload)
            summary = format_backtest(payload)
            database.update_backtest_job(
                job_id, "DONE", backtest_run_id=run_id, result_summary=summary
            )
            log.info("Backtest job #%s done: backtest run %s", job_id, run_id)
        except Exception as exc:
            log.exception("Backtest job #%s failed", job_id)
            database.update_backtest_job(job_id, "FAILED", error=str(exc))
            _notify(
                database,
                whatsapp,
                sender,
                f"⚠️ Backtest {period} (job #{job_id}) failed: {str(exc)[:250]}",
            )
            processed += 1
            continue
        _notify(
            database,
            whatsapp,
            sender,
            f"🧪 Backtest {period} (job #{job_id}) complete:\n\n{summary}",
        )
        processed += 1


def _notify(database: Database, whatsapp: WhatsAppClient | None, sender: str, body: str) -> None:
    try:
        client = whatsapp or WhatsAppClient()
        client.send_text(sender, body)
    except Exception:
        log.exception("Failed to send backtest report to sender ****%s", sender[-4:])
