from __future__ import annotations

import hashlib
import hmac
import logging
import re
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from PIL import Image, ImageDraw
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import SessionLocal
from app.market.candles import aggregate_12h_from_4h
from app.market.mexc import MexcPublicClient, normalize_symbol
from app.models import Forecast, Job, SystemEvent, WhatsAppInboundEvent, WorkerHeartbeat
from app.notifications.whatsapp import WhatsAppCloudAPI
from app.services.jobs import enqueue_job, job_to_dict

log = logging.getLogger(__name__)
_REQUEST_ID: ContextVar[str] = ContextVar("kronos_whatsapp_request_id", default="")

HELP_TEXT = """KRONOS commands

PING
  Check that WhatsApp command processing and replies are working.

PRICE [symbol]
  Live MEXC Futures quote. Example: PRICE BTCUSDT

ANALYZE [symbol]
  Queue full 1D > 12H > 4H > 1H research analysis.

CHART [symbol] [1H|4H|1D]
  Send a candlestick chart image. Default: 4H.

FORECAST [symbol] / KRONOS [symbol]
  Queue a real Kronos model forecast (1H by default).

BACKTEST [symbol] [1D|7D|30D] [KRONOS]
  Queue a historical paper backtest. Example: BACKTEST BTCUSDT 7D KRONOS
  KRONOS runs the real model filter at each historical decision point and is expensive.
  You can also send: BACKTEST 1D

BACKTEST STATUS
  Show your latest backtest job.

STATUS [job_id]
  Show your latest job, or one of your own job IDs.

QUEUE
  Show queue counts and oldest queued job age.

WORKERS
  Show inference, compute and market worker heartbeats.

HEALTH / DIAGNOSTICS
  Show database, queue, worker and Kronos readiness diagnostics.

DASHBOARD / SIGNALS
  Open the authenticated dashboard for detailed research results. WhatsApp does not send entries, stops or targets.

SCAN
  Queue a MEXC universe scan.

HELP
  Show this command list.

Live exchange orders are disabled. All outputs are research/paper only."""


def normalize_phone(value: str) -> str:
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    if not 8 <= len(digits) <= 15:
        raise ValueError("Phone number must be 8–15 digits in international format")
    return digits


def allowed_senders() -> set[str]:
    cfg = get_settings()
    raw = cfg.whatsapp_allowed_recipients or cfg.whatsapp_default_recipient
    result = set()
    for item in str(raw or "").split(","):
        item = item.strip()
        if item:
            try:
                result.add(normalize_phone(item))
            except ValueError:
                log.warning("Ignoring malformed WHATSAPP_ALLOWED_RECIPIENTS entry")
    return result


def verify_webhook_signature(body: bytes, signature: str | None, app_secret: str) -> bool:
    if not body or not signature or not app_secret:
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def _format_number(value: Any, decimals: int = 8) -> str:
    try:
        number = float(value)
        if number == 0:
            return "0"
        return f"{number:,.{decimals}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError, OverflowError):
        return "n/a"


def _send_text(sender: str, text: str) -> dict:
    adapter = WhatsAppCloudAPI()
    try:
        return adapter.send_text(sender, text)
    finally:
        adapter.close()


def _job_payload(sender: str, **kwargs) -> dict:
    request_id = _REQUEST_ID.get() or uuid.uuid4().hex
    return {**kwargs, "request_id": request_id, "whatsapp_recipient": sender,
            "whatsapp_notify_on_completion": True}


def _enqueue_whatsapp_job(job_type: str, payload: dict) -> Job:
    job = enqueue_job(job_type, payload)
    request_id = str(payload.get("request_id") or "")
    log.info("[JOB_CREATED] job_id=%s request_id=%s type=%s", job.id, request_id, job_type)
    log.info("[JOB_QUEUED] job_id=%s request_id=%s type=%s", job.id, request_id, job_type)
    return job


def _required_worker(job_type: str) -> str:
    return {
        "analysis": "inference", "forecast": "inference", "evaluate_forecast": "inference",
        "backtest": "compute", "scan": "market",
    }.get(job_type, "unknown")


def _worker_rows() -> list[WorkerHeartbeat]:
    with SessionLocal() as db:
        rows = db.scalars(select(WorkerHeartbeat).order_by(desc(WorkerHeartbeat.last_seen))).all()
        for row in rows:
            db.expunge(row)
        return rows


def _is_recent_heartbeat(row: WorkerHeartbeat, *, seconds: int = 60) -> bool:
    seen = row.last_seen
    if seen is None:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - seen).total_seconds() <= seconds


def _queue_snapshot() -> dict[str, Any]:
    with SessionLocal() as db:
        counts = {
            status: int(db.scalar(select(func.count(Job.id)).where(Job.status == status)) or 0)
            for status in ("queued", "running", "completed", "failed", "cancelled")
        }
        oldest = db.scalar(select(func.min(Job.created_at)).where(Job.status == "queued"))
    age = None
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        age = max(0, int((datetime.now(timezone.utc) - oldest).total_seconds()))
    return {"counts": counts, "oldest_queued_age_seconds": age}


def _queue_message() -> str:
    snapshot = _queue_snapshot()
    counts = snapshot["counts"]
    age = snapshot["oldest_queued_age_seconds"]
    lines = [
        "📥 KRONOS JOB QUEUE",
        f"⏳ Queued: {counts['queued']}",
        f"⚙️ Running: {counts['running']}",
        f"✅ Completed: {counts['completed']}",
        f"❌ Failed: {counts['failed']}",
        f"Oldest queued: {age}s" if age is not None else "Oldest queued: none",
    ]
    return "\n".join(lines)


def _workers_message() -> str:
    rows = _worker_rows()
    if not rows:
        return "🛠️ No worker heartbeat has been recorded yet. Check the Render market, inference and compute worker services."
    lines = ["🛠️ KRONOS WORKERS"]
    seen_roles = set()
    for row in rows:
        if row.role in seen_roles:
            continue
        seen_roles.add(row.role)
        live = _is_recent_heartbeat(row)
        age = max(0, int((datetime.now(timezone.utc) - (
            row.last_seen.replace(tzinfo=timezone.utc) if row.last_seen.tzinfo is None else row.last_seen
        )).total_seconds()))
        state = "🟢 ONLINE" if live else "🔴 STALE/OFFLINE"
        lines.append(f"{row.role.upper()}: {state} · heartbeat {age}s ago · {row.message[:90]}")
    for role in ("inference", "compute", "market"):
        if role not in seen_roles:
            lines.append(f"{role.upper()}: 🔴 NO HEARTBEAT")
    return "\n".join(lines)


def _diagnostics_message() -> str:
    try:
        with SessionLocal() as db:
            db.execute(select(1)).scalar_one()
        database_state = "✅ CONNECTED"
    except Exception as exc:
        database_state = f"❌ ERROR ({type(exc).__name__})"
    rows = _worker_rows()
    role_ok = {role: any(row.role in {role, "all"} and _is_recent_heartbeat(row) for row in rows)
               for role in ("inference", "compute", "market")}
    try:
        with SessionLocal() as db:
            last_forecast = db.scalar(select(Forecast.created_at).order_by(desc(Forecast.created_at)).limit(1))
        kronos_state = "✅ forecast record exists" if last_forecast else "🟡 not yet verified by a completed forecast"
    except Exception as exc:
        kronos_state = f"❌ unavailable ({type(exc).__name__})"
    snapshot = _queue_snapshot()
    c = snapshot["counts"]
    lines = [
        "🔎 KRONOS DIAGNOSTICS",
        f"Database: {database_state}",
        f"Inference worker: {'🟢 online' if role_ok['inference'] else '🔴 missing/stale'}",
        f"Backtest worker: {'🟢 online' if role_ok['compute'] else '🔴 missing/stale'}",
        f"Market worker: {'🟢 online' if role_ok['market'] else '🔴 missing/stale'}",
        f"Kronos: {kronos_state}",
        f"Queue: {c['queued']} queued / {c['running']} running / {c['failed']} failed",
    ]
    if c["queued"] and not (role_ok["inference"] or role_ok["compute"] or role_ok["market"]):
        lines.append("⚠️ No recent worker heartbeat: queued jobs cannot be consumed until the correct Render worker is online.")
    return "\n".join(lines)


def _format_job_status(job: Job) -> str:
    lines = [
        f"Job: {job.id}",
        f"Type: {job.job_type.upper()}",
        f"Status: {job.status.upper()}",
        f"Progress: {float(job.progress or 0):.0f}%",
        f"Update: {job.progress_message or 'n/a'}",
    ]
    if job.status == "failed" and job.error:
        lines.append(f"Error: {job.error[:700]}")
    if job.status == "queued":
        role = _required_worker(job.job_type)
        rows = _worker_rows()
        online = any(row.role in {role, "all"} and _is_recent_heartbeat(row) for row in rows)
        if online:
            lines.append(f"Worker: {role.upper()} heartbeat detected; waiting for job claim/retry time.")
            if job.next_attempt_at:
                retry_at = job.next_attempt_at
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                lines.append(f"Next eligible retry: {retry_at.isoformat()}")
        else:
            lines.append(f"⚠️ Wait reason: no recent {role.upper()} worker heartbeat. Check that Render worker service is running and uses the same DATABASE_URL.")
        queued_at = job.created_at
        if queued_at is not None:
            if queued_at.tzinfo is None:
                queued_at = queued_at.replace(tzinfo=timezone.utc)
            age_seconds = max(0, int((datetime.now(timezone.utc) - queued_at).total_seconds()))
            if age_seconds > 60 and online:
                lines.append(f"⚠️ Worker heartbeat exists but this job has not been claimed after {age_seconds}s. Check worker logs, role routing and shared DATABASE_URL.")
    if job.status == "completed" and job.result:
        if job.job_type == "backtest":
            m = (job.result or {}).get("metrics") or {}
            lines.extend([
                f"Trades: {m.get('trades', 0)}",
                f"Win rate: {_format_number((m.get('win_rate') or 0) * 100, 2)}%",
                f"Net return: {_format_number(m.get('net_return_pct'), 2)}%",
                f"Profit factor: {_format_number(m.get('profit_factor'), 2)}",
                f"Max drawdown: {_format_number(m.get('max_drawdown_pct'), 2)}%",
            ])
        elif job.job_type == "analysis":
            result = job.result
            strategy = result.get("strategy") or {}
            lines.extend([
                f"Decision: {result.get('decision', 'n/a')}",
                f"Side: {strategy.get('side') or 'n/a'}",
                f"Score: {_format_number(strategy.get('score'), 1)}",
            ])
        elif job.job_type == "scan":
            result = job.result
            candidates = result.get("candidates") or []
            lines.extend([
                f"Universe checked: {result.get('universe_size', 'n/a')}",
                f"Analysis candidates: {len(candidates)}",
                f"Rejected: {result.get('rejected_count', 'n/a')}",
            ])
            for item in candidates[:5]:
                lines.append(
                    f"{item.get('symbol')} {item.get('side')} | score {_format_number(item.get('score'), 1)}"
                )
    return "\n".join(lines)


def _find_sender_job(sender: str, job_id: str | None = None, job_type: str | None = None) -> Job | None:
    with SessionLocal() as db:
        if job_id:
            row = db.get(Job, job_id)
            if row is None or (row.payload or {}).get("whatsapp_recipient") != sender:
                return None
            if job_type and row.job_type != job_type:
                return None
        else:
            stmt = select(Job).where(Job.payload["whatsapp_recipient"].as_string() == sender)
            if job_type:
                stmt = stmt.where(Job.job_type == job_type)
            row = db.scalars(stmt.order_by(desc(Job.created_at)).limit(1)).first()
        if row is not None:
            db.expunge(row)
        return row


def _render_candlestick_chart(frame: pd.DataFrame, symbol: str, interval: str, path: Path) -> None:
    frame = frame.tail(80).reset_index(drop=True)
    if frame.empty:
        raise ValueError("No completed candles were returned for the chart")
    width, height = 1200, 700
    image = Image.new("RGB", (width, height), "#111827")
    draw = ImageDraw.Draw(image)
    draw.text((42, 24), f"{symbol}  |  {interval}  |  MEXC FUTURES", fill="#F9FAFB")
    draw.text((42, 52), f"Last close: {_format_number(frame.iloc[-1]['close'])}", fill="#9CA3AF")
    top, bottom, left, right = 100, 610, 85, 1130
    lows = frame["low"].astype(float)
    highs = frame["high"].astype(float)
    low = float(lows.min())
    high = float(highs.max())
    padding = (high - low) * 0.06 if high > low else max(abs(high) * 0.01, 1e-8)
    low -= padding
    high += padding
    draw.rectangle((left, top, right, bottom), outline="#374151", width=2)
    for grid in range(5):
        y = top + (bottom - top) * grid / 4
        price = high - (high - low) * grid / 4
        draw.line((left, y, right, y), fill="#273244", width=1)
        draw.text((8, y - 8), _format_number(price, 4), fill="#D1D5DB")
    candle_width = max(4, int((right - left) / max(len(frame), 1) * 0.55))
    slot = (right - left) / max(len(frame), 1)
    for index, row in frame.iterrows():
        x = left + slot * (index + 0.5)
        op, cl = float(row["open"]), float(row["close"])
        hi, lo = float(row["high"]), float(row["low"])
        y_hi = bottom - (hi - low) / (high - low) * (bottom - top)
        y_lo = bottom - (lo - low) / (high - low) * (bottom - top)
        y_op = bottom - (op - low) / (high - low) * (bottom - top)
        y_cl = bottom - (cl - low) / (high - low) * (bottom - top)
        rising = cl >= op
        color = "#22C55E" if rising else "#EF4444"
        draw.line((x, y_hi, x, y_lo), fill=color, width=2)
        body_top, body_bottom = sorted((y_op, y_cl))
        if body_bottom - body_top < 2:
            body_bottom = body_top + 2
        draw.rectangle(
            (x - candle_width / 2, body_top, x + candle_width / 2, body_bottom),
            fill=color, outline=color,
        )
    draw.text((left, 632), "Older", fill="#9CA3AF")
    draw.text((right - 50, 632), "Latest", fill="#9CA3AF")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)


def _create_chart(sender: str, symbol: str, interval: str, public_base_url: str) -> None:
    intervals = {"1H": "Min60", "4H": "Hour4", "12H": "12H", "1D": "Day1"}
    if interval not in intervals:
        raise ValueError("Chart timeframe must be 1H, 4H, 12H or 1D")
    mexc_interval = intervals[interval]
    seconds = {"1H": 3600, "4H": 14400, "12H": 43200, "1D": 86400}[interval]
    end = int(datetime.now(timezone.utc).timestamp())
    with MexcPublicClient() as client:
        if interval == "12H":
            h4 = client.klines(symbol, "Hour4", end - 320 * 14400, end)
            frame = aggregate_12h_from_4h(h4).tail(100)
        else:
            frame = client.klines(symbol, mexc_interval, end - 100 * seconds, end)
    cfg = get_settings()
    base_url = (cfg.public_base_url or public_base_url or "").rstrip("/")
    if not base_url.startswith("https://"):
        raise ValueError("Set PUBLIC_BASE_URL to your public HTTPS Render URL to send chart images")
    chart_id = uuid.uuid4().hex
    chart_dir = cfg.data_dir / "whatsapp_charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).timestamp()
    for old in chart_dir.glob("*.png"):
        try:
            if now - old.stat().st_mtime > 48 * 3600:
                old.unlink(missing_ok=True)
        except OSError:
            pass
    path = chart_dir / f"{chart_id}.png"
    _render_candlestick_chart(frame, symbol, interval, path)
    log.info("[CHART_GENERATED] symbol=%s timeframe=%s candles=%s chart_id=%s", symbol, interval, len(frame), chart_id)
    url = f"{base_url}/api/whatsapp/charts/{chart_id}.png"
    adapter = WhatsAppCloudAPI()
    try:
        adapter.send_image(sender, url, f"{symbol} {interval} completed-candle chart")
    finally:
        adapter.close()


def dispatch_command(sender: str, text: str, public_base_url: str = "") -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return "Send HELP to see available commands."
    parts = cleaned.replace(",", " ").split()
    command = parts[0].lstrip("/").upper()
    request_id = _REQUEST_ID.get() or uuid.uuid4().hex

    if command in {"HELP", "START", "MENU"}:
        return HELP_TEXT

    if command == "PING":
        return f"✅ KRONOS command handler is responding.\nRequest: {request_id[:12]}"

    if command in {"DASHBOARD", "SIGNALS"}:
        cfg = get_settings()
        base = (cfg.public_base_url or public_base_url or "").rstrip("/")
        if base.startswith("https://"):
            return ("📊 Detailed research and paper signals are available in the authenticated dashboard:\n"
                    f"{base}/\n🔐 Sign in to view account data.")
        return "📊 Detailed research and signals are available in the authenticated dashboard. Set PUBLIC_BASE_URL to show its link."

    if command in {"QUEUE", "JOBS"}:
        return _queue_message()

    if command == "WORKERS":
        return _workers_message()

    if command in {"HEALTH", "DIAGNOSTICS", "DIAG"}:
        return _diagnostics_message()

    if command == "PRICE":
        symbol = normalize_symbol(parts[1] if len(parts) > 1 else "BTCUSDT")
        with MexcPublicClient() as client:
            rows = client.tickers(symbol)
        ticker = next((row for row in rows if str(row.get("symbol", "")).upper() == symbol), rows[0] if rows else None)
        if not ticker:
            raise ValueError(f"No MEXC ticker found for {symbol}")
        return "\n".join([
            f"{symbol} — MEXC Futures",
            f"Last: {_format_number(ticker.get('lastPrice'))}",
            f"Mark: {_format_number(ticker.get('fairPrice') or ticker.get('markPrice'))}",
            f"Index: {_format_number(ticker.get('indexPrice'))}",
            f"Bid / Ask: {_format_number(ticker.get('bid1'))} / {_format_number(ticker.get('ask1'))}",
            f"24h turnover: {_format_number(ticker.get('amount24'), 2)} USDT",
            f"Funding rate: {_format_number(ticker.get('fundingRate'), 6)}",
            f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        ])

    if command == "ANALYZE":
        symbol = normalize_symbol(parts[1] if len(parts) > 1 else "BTCUSDT")
        job = _enqueue_whatsapp_job("analysis", _job_payload(sender, request_id=request_id, symbol=symbol))
        return (
            f"⏳ Analysis queued for {symbol}.\nJob: {job.id}\n"
            "You will receive the result when the inference worker finishes. Send STATUS to check progress."
        )

    if command == "CHART":
        symbol = normalize_symbol(parts[1] if len(parts) > 1 else "BTCUSDT")
        interval = parts[2].upper() if len(parts) > 2 else "4H"
        _create_chart(sender, symbol, interval, public_base_url)
        return f"📈 Chart sent: {symbol} {interval}."

    if command in {"FORECAST", "KRONOS"}:
        cfg = get_settings()
        symbol = normalize_symbol(parts[1] if len(parts) > 1 else "BTCUSDT")
        timeframe = "Min60"
        if len(parts) > 2:
            timeframe = {"1H": "Min60", "4H": "Hour4", "1D": "Day1"}.get(parts[2].upper(), "")
            if not timeframe:
                raise ValueError("Forecast timeframe must be 1H, 4H or 1D")
        job = _enqueue_whatsapp_job("forecast", _job_payload(
            sender, request_id=request_id, symbol=symbol, timeframe=timeframe,
            samples=cfg.forecast_sample_count, horizon=cfg.forecast_horizon,
            seed=getattr(cfg, "forecast_random_seed", 42),
        ))
        return (f"🔮 Kronos forecast queued for {symbol} ({timeframe}).\nJob: {job.id}\n"
                "⏳ The inference worker will send a completion notice. Send STATUS to check.")

    if command == "BACKTEST":
        if len(parts) > 1 and parts[1].upper() == "STATUS":
            job = _find_sender_job(sender, job_type="backtest")
            return _format_job_status(job) if job else "No backtest jobs found for your number."
        symbol = "BTC_USDT"
        days = 30
        use_kronos_filter = any(token.upper() == "KRONOS" for token in parts[1:])
        args = [token for token in parts[1:] if token.upper() != "KRONOS"]
        if args:
            token = args[0].upper()
            if re.fullmatch(r"1D|7D|30D", token):
                days = int(token[:-1])
            else:
                symbol = normalize_symbol(token)
        if len(args) > 1:
            duration = args[1].upper()
            if not re.fullmatch(r"1D|7D|30D", duration):
                raise ValueError("Backtest duration must be 1D, 7D or 30D")
            days = int(duration[:-1])
        if len(args) > 2:
            raise ValueError("Usage: BACKTEST [symbol] [1D|7D|30D] [KRONOS]")
        job = _enqueue_whatsapp_job("backtest", _job_payload(
            sender, request_id=request_id, symbol=symbol, days=days,
            use_kronos_filter=use_kronos_filter,
        ))
        return (
            f"📚 {days}D backtest queued for {symbol}.\n"
            f"🔮 Kronos historical filter: {'ON' if use_kronos_filter else 'OFF'}\nJob: {job.id}\n"
            "You will receive the report when the compute worker finishes. Send BACKTEST STATUS to check."
        )

    if command == "STATUS":
        job_id = parts[1] if len(parts) > 1 else None
        job = _find_sender_job(sender, job_id=job_id)
        return _format_job_status(job) if job else "No matching jobs found for your number."

    if command == "SCAN":
        job = _enqueue_whatsapp_job("scan", _job_payload(sender, request_id=request_id, source="whatsapp"))
        return f"MEXC universe scan queued.\nJob: {job.id}\nSend STATUS to check progress."

    raise ValueError("Unknown command. Send HELP to see available commands.")


def notify_job_completion(job: Job, result: dict | None = None, error: str | None = None) -> None:
    payload = job.payload or {}
    if not payload.get("whatsapp_notify_on_completion"):
        return
    recipient = payload.get("whatsapp_recipient")
    if not recipient:
        return
    if error:
        # Keep sensitive provider/network exception details in server logs only.
        message = (f"❌ KRONOS {job.job_type.upper()} JOB FAILED\nJob: {job.id}\n"
                   f"Error type: {type(error).__name__ if isinstance(error, Exception) else 'ExecutionError'}\n"
                   "Use STATUS with this job ID for progress and check the authenticated dashboard for diagnostics.")
    else:
        message = (f"✅ KRONOS {job.job_type.upper()} JOB COMPLETED\nJob: {job.id}\n"
                   "📊 Open the authenticated dashboard for the full research output.\n"
                   "Research/paper mode only; no exchange order was placed.")
    adapter = WhatsAppCloudAPI()
    try:
        adapter.send_text(str(recipient), message)
    finally:
        adapter.close()


def process_whatsapp_payload(payload: dict, public_base_url: str = "") -> None:
    """Process inbound text messages and delivery statuses from a verified webhook."""
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            for status in value.get("statuses", []) or []:
                try:
                    with SessionLocal.begin() as db:
                        db.add(SystemEvent(
                            level="INFO",
                            event_type="WHATSAPP_DELIVERY_STATUS",
                            message=f"WhatsApp message delivery status: {str(status.get('status') or 'unknown')[:40]}",
                            details={
                                "message_id": str(status.get("id") or "")[:128],
                                "status": str(status.get("status") or "")[:40],
                            },
                        ))
                except Exception:
                    log.exception("Could not persist WhatsApp delivery status")
            for message in value.get("messages", []) or []:
                request_id = uuid.uuid4().hex
                sender_raw = str(message.get("from") or "")
                try:
                    sender = normalize_phone(sender_raw)
                except ValueError:
                    log.warning("Ignoring WhatsApp event with invalid sender number")
                    continue
                allowed = allowed_senders()
                if sender not in allowed:
                    log.warning("[COMMAND_REJECTED] request_id=%s reason=sender_not_allowlisted", request_id)
                    continue

                msg_type = str(message.get("type") or "unknown")
                message_id = str(message.get("id") or "")
                body = ""
                if msg_type == "text":
                    body = str((message.get("text") or {}).get("body") or "")
                elif msg_type == "interactive":
                    interactive = message.get("interactive") or {}
                    button = interactive.get("button_reply") or {}
                    list_reply = interactive.get("list_reply") or {}
                    body = str(button.get("title") or button.get("id") or list_reply.get("title") or list_reply.get("id") or "")
                elif msg_type == "button":
                    body = str((message.get("button") or {}).get("text") or "")
                if not message_id:
                    message_id = hashlib.sha256(f"{sender}|{body}|{message.get('timestamp')}".encode()).hexdigest()

                try:
                    with SessionLocal.begin() as db:
                        db.add(WhatsAppInboundEvent(
                            message_id=message_id[:128],
                            sender=sender,
                            message_type=msg_type[:24],
                            message_text=body[:4000],
                            status="RECEIVED",
                        ))
                        db.flush()
                except IntegrityError:
                    # Meta retries webhook notifications. The primary key prevents duplicate commands.
                    log.info("[WEBHOOK_DUPLICATE] request_id=%s", request_id)
                    continue
                except Exception:
                    log.exception("Unable to record inbound WhatsApp event")
                    continue

                try:
                    log.info("[WEBHOOK_RECEIVED] request_id=%s message_id=%s type=%s",
                             request_id, message_id[:24], msg_type)
                    if msg_type not in {"text", "interactive", "button"} or not body.strip():
                        reply = "I can process text commands. Send HELP for the command list."
                    else:
                        log.info("[PAYLOAD_PARSED] request_id=%s body_length=%s", request_id, len(body))
                        log.info("[COMMAND_IDENTIFIED] request_id=%s command=%s",
                                 request_id, body.strip().split()[0].lstrip("/").upper())
                        context_token = _REQUEST_ID.set(request_id)
                        try:
                            reply = dispatch_command(sender, body, public_base_url)
                        finally:
                            _REQUEST_ID.reset(context_token)
                        log.info("[COMMAND_VALIDATED] request_id=%s", request_id)
                    log.info("[RESPONSE_FORMATTED] request_id=%s command=%s chars=%s",
                             request_id, body.strip().split()[0].lstrip("/").upper() if body.strip() else "UNKNOWN", len(reply))
                    _send_text(sender, reply)
                    log.info("[RESPONSE_SENT] request_id=%s message_id=%s", request_id, message_id[:24])
                    status_value = "PROCESSED"
                    error_value = None
                except Exception as exc:
                    log.exception("[COMMAND_FAILED] request_id=%s type=%s error_type=%s",
                                  request_id, msg_type, type(exc).__name__)
                    status_value = "FAILED"
                    error_value = f"{type(exc).__name__}: {exc}"[:1000]
                    try:
                        _send_text(sender, f"❌ Command failed ({type(exc).__name__}).\nSend HELP for available commands.")
                    except Exception:
                        log.exception("[RESPONSE_FAILED] request_id=%s", request_id)
                finally:
                    with SessionLocal.begin() as db:
                        event = db.get(WhatsAppInboundEvent, message_id[:128])
                        if event:
                            event.status = status_value
                            event.error = error_value
                            event.processed_at = datetime.now(timezone.utc)
