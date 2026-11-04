from __future__ import annotations

import hashlib
import hmac
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from PIL import Image, ImageDraw
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import SessionLocal
from app.market.mexc import MexcPublicClient, normalize_symbol
from app.models import Job, SystemEvent, WhatsAppInboundEvent
from app.notifications.whatsapp import WhatsAppCloudAPI
from app.services.jobs import enqueue_job, job_to_dict

log = logging.getLogger(__name__)

HELP_TEXT = """KRONOS commands

PRICE [symbol]
  Live MEXC Futures quote. Example: PRICE BTCUSDT

ANALYZE [symbol]
  Queue full 1D > 12H > 4H > 1H research analysis.

CHART [symbol] [1H|4H|1D]
  Send a candlestick chart image. Default: 4H.

BACKTEST [symbol] [1D|7D|30D]
  Queue a historical paper backtest. Example: BACKTEST BTCUSDT 7D
  You can also send: BACKTEST 1D

BACKTEST STATUS
  Show your latest backtest job.

STATUS [job_id]
  Show your latest job, or one of your own job IDs.

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
    return {**kwargs, "whatsapp_recipient": sender, "whatsapp_notify_on_completion": True}


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
        rows = db.scalars(select(Job).order_by(desc(Job.created_at)).limit(150)).all()
        for row in rows:
            payload = row.payload or {}
            if payload.get("whatsapp_recipient") != sender:
                continue
            if job_id and row.id != job_id:
                continue
            if job_type and row.job_type != job_type:
                continue
            db.expunge(row)
            return row
    return None


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
    intervals = {"1H": "Min60", "4H": "Hour4", "1D": "Day1"}
    if interval not in intervals:
        raise ValueError("Chart timeframe must be 1H, 4H or 1D")
    mexc_interval = intervals[interval]
    seconds = {"1H": 3600, "4H": 14400, "1D": 86400}[interval]
    end = int(datetime.now(timezone.utc).timestamp())
    with MexcPublicClient() as client:
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
    command = parts[0].upper()

    if command in {"HELP", "START", "MENU"}:
        return HELP_TEXT

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
        job = enqueue_job("analysis", _job_payload(sender, symbol=symbol))
        return (
            f"Analysis queued for {symbol}.\nJob: {job.id}\n"
            "You will receive the result when the inference worker finishes. Send STATUS to check progress."
        )

    if command == "CHART":
        symbol = normalize_symbol(parts[1] if len(parts) > 1 else "BTCUSDT")
        interval = parts[2].upper() if len(parts) > 2 else "4H"
        _create_chart(sender, symbol, interval, public_base_url)
        return f"Chart sent: {symbol} {interval}."

    if command == "BACKTEST":
        if len(parts) > 1 and parts[1].upper() == "STATUS":
            job = _find_sender_job(sender, job_type="backtest")
            return _format_job_status(job) if job else "No backtest jobs found for your number."
        symbol = "BTC_USDT"
        days = 30
        if len(parts) > 1:
            token = parts[1].upper()
            if re.fullmatch(r"1D|7D|30D", token):
                days = int(token[:-1])
            else:
                symbol = normalize_symbol(token)
        if len(parts) > 2:
            duration = parts[2].upper()
            if not re.fullmatch(r"1D|7D|30D", duration):
                raise ValueError("Backtest duration must be 1D, 7D or 30D")
            days = int(duration[:-1])
        job = enqueue_job("backtest", _job_payload(
            sender, symbol=symbol, days=days, use_kronos_filter=False,
        ))
        return (
            f"{days}D backtest queued for {symbol}.\nJob: {job.id}\n"
            "You will receive the report when the compute worker finishes. Send BACKTEST STATUS to check."
        )

    if command == "STATUS":
        job_id = parts[1] if len(parts) > 1 else None
        job = _find_sender_job(sender, job_id=job_id)
        return _format_job_status(job) if job else "No matching jobs found for your number."

    if command == "SCAN":
        job = enqueue_job("scan", _job_payload(sender, source="whatsapp"))
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
        message = f"KRONOS {job.job_type.upper()} FAILED\nJob: {job.id}\n{str(error)[:900]}"
    else:
        result = result or {}
        if job.job_type == "analysis":
            strategy = result.get("strategy") or {}
            reasons = result.get("reasons") or []
            message_lines = [
                f"KRONOS ANALYSIS — {result.get('symbol', 'UNKNOWN')}",
                f"Decision: {result.get('decision', 'n/a')}",
                f"Side: {strategy.get('side') or 'n/a'}",
                f"Score: {_format_number(strategy.get('score'), 1)}",
                f"Market price: {_format_number((result.get('market') or {}).get('last_price'))}",
                f"Timeframes: 1D / 12H / 4H / 1H",
            ]
            if strategy.get("entry") is not None:
                message_lines.append(f"Entry: {_format_number(strategy.get('entry'))}")
            if strategy.get("stop") is not None:
                message_lines.append(f"Stop: {_format_number(strategy.get('stop'))}")
            if strategy.get("tp1") is not None:
                message_lines.append(f"TP1: {_format_number(strategy.get('tp1'))}")
            if strategy.get("tp2") is not None:
                message_lines.append(f"TP2: {_format_number(strategy.get('tp2'))}")
            for reason in reasons[:3]:
                message_lines.append(f"• {str(reason)[:180]}")
            message_lines.append("Research/paper output only — no exchange order was placed.")
            message = "\n".join(message_lines)
        elif job.job_type == "backtest":
            metrics = result.get("metrics") or {}
            days = (payload.get("days") or "n/a")
            message = "\n".join([
                f"KRONOS BACKTEST COMPLETE — {result.get('symbol', payload.get('symbol', 'UNKNOWN'))}",
                f"Period: {days}D",
                f"Trades: {metrics.get('trades', 0)} | Wins: {metrics.get('wins', 0)} | Losses: {metrics.get('losses', 0)}",
                f"Win rate: {_format_number((metrics.get('win_rate') or 0) * 100, 2)}%",
                f"Net return: {_format_number(metrics.get('net_return_pct'), 2)}%",
                f"Profit factor: {_format_number(metrics.get('profit_factor'), 2)}",
                f"Max drawdown: {_format_number(metrics.get('max_drawdown_pct'), 2)}%",
                f"Funding data: {metrics.get('funding_data_quality', 'unknown')}",
                f"Job: {job.id}",
                "Historical research only; results are not a forecast or a guarantee.",
            ])
        elif job.job_type == "scan":
            candidates = result.get("candidates") or []
            lines = [
                "KRONOS MEXC SCAN COMPLETE",
                f"Universe: {result.get('universe_size', 'n/a')}",
                f"Analysis jobs queued: {len(candidates)}",
                f"Rejected: {result.get('rejected_count', 'n/a')}",
            ]
            for item in candidates[:8]:
                lines.append(
                    f"{item.get('symbol')} {item.get('side')} | score {_format_number(item.get('score'), 1)}"
                )
            lines.append(f"Job: {job.id}")
            message = "\n".join(lines)
        else:
            message = f"KRONOS {job.job_type.upper()} COMPLETE\nJob: {job.id}\nSend STATUS for details."
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
                                "recipient_id": str(status.get("recipient_id") or "")[:32],
                                "status": str(status.get("status") or "")[:40],
                            },
                        ))
                except Exception:
                    log.exception("Could not persist WhatsApp delivery status")
            for message in value.get("messages", []) or []:
                sender_raw = str(message.get("from") or "")
                try:
                    sender = normalize_phone(sender_raw)
                except ValueError:
                    log.warning("Ignoring WhatsApp event with invalid sender number")
                    continue
                allowed = allowed_senders()
                if sender not in allowed:
                    log.warning("Ignoring WhatsApp command from non-allowlisted sender %s", sender)
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
                    continue
                except Exception:
                    log.exception("Unable to record inbound WhatsApp event")
                    continue

                try:
                    if msg_type not in {"text", "interactive", "button"} or not body.strip():
                        reply = "I can process text commands. Send HELP for the command list."
                    else:
                        reply = dispatch_command(sender, body, public_base_url)
                    _send_text(sender, reply)
                    status_value = "PROCESSED"
                    error_value = None
                except Exception as exc:
                    log.exception("WhatsApp command failed sender=%s type=%s", sender, msg_type)
                    status_value = "FAILED"
                    error_value = f"{type(exc).__name__}: {exc}"[:1000]
                    try:
                        _send_text(sender, f"Command failed: {str(exc)[:800]}\nSend HELP for available commands.")
                    except Exception:
                        log.exception("Could not send WhatsApp command error reply")
                finally:
                    with SessionLocal.begin() as db:
                        event = db.get(WhatsAppInboundEvent, message_id[:128])
                        if event:
                            event.status = status_value
                            event.error = error_value
                            event.processed_at = datetime.now(timezone.utc)
