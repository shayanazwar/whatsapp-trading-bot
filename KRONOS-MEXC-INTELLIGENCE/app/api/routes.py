from __future__ import annotations

import csv
import io
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from app.config import get_settings
from app.db import SessionLocal
from app.forecasting.kronos import _repo_path
from app.market.mexc import MexcPublicClient, MexcAPIError, normalize_symbol
from app.market.universe import eligible_universe
from app.models import (BacktestReport, Forecast, ForecastEvaluation, Job, Signal,
                        SystemEvent, WorkerHeartbeat)
from app.notifications.whatsapp import WhatsAppCloudAPI
from app.notifications.whatsapp_commands import process_whatsapp_payload, verify_webhook_signature
from app.security import AdminUser, issue_session
from app.services.jobs import enqueue_job, job_to_dict

router = APIRouter(prefix="/api")


class LoginRequest(BaseModel):
    username: str
    password: str


class AnalyzeRequest(BaseModel):
    symbol: str = "BTC_USDT"


class ForecastRequest(BaseModel):
    symbol: str = "BTC_USDT"
    timeframe: str = "Min60"
    samples: int = Field(default=1000, ge=1, le=1000)
    horizon: int = Field(default=24, ge=1, le=48)
    seed: int | None = None


class BacktestRequest(BaseModel):
    symbol: str = "BTC_USDT"
    days: int = Field(default=30, ge=1, le=180)
    use_kronos_filter: bool = False


class ScanRequest(BaseModel):
    confirm: bool = True


_LOGIN_FAILURES: dict[str, deque[float]] = defaultdict(deque)
_LOGIN_WINDOW_SECONDS = 600
_LOGIN_MAX_FAILURES = 8


def _auth_compare(username: str, password: str) -> bool:
    cfg = get_settings()
    return secrets.compare_digest(username, cfg.admin_username) and secrets.compare_digest(password, cfg.admin_password)


@router.post("/auth/login")
def login(body: LoginRequest, request: Request, response: Response):
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    failures = _LOGIN_FAILURES[client_ip]
    while failures and now - failures[0] > _LOGIN_WINDOW_SECONDS:
        failures.popleft()
    if len(failures) >= _LOGIN_MAX_FAILURES:
        raise HTTPException(status_code=429, detail="Too many failed login attempts; try again later")
    if not _auth_compare(body.username, body.password):
        failures.append(now)
        if len(_LOGIN_FAILURES) > 10_000:
            for key in list(_LOGIN_FAILURES)[:2_000]:
                if not _LOGIN_FAILURES[key] or now - _LOGIN_FAILURES[key][-1] > _LOGIN_WINDOW_SECONDS:
                    _LOGIN_FAILURES.pop(key, None)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    _LOGIN_FAILURES.pop(client_ip, None)
    csrf = issue_session(response, body.username)
    return {"authenticated": True, "username": body.username, "csrf_token": csrf}


@router.post("/auth/logout")
def logout(response: Response, _: AdminUser):
    response.delete_cookie("kronos_session", path="/")
    response.delete_cookie("kronos_csrf", path="/")
    return {"authenticated": False}


@router.get("/auth/me")
def me(user: AdminUser):
    return {"authenticated": True, "username": user}


@router.get("/status")
def status(_: AdminUser):
    cfg = get_settings()
    with SessionLocal() as db:
        queued = db.scalar(select(Job.id).where(Job.status == "queued").limit(1)) is not None
        last_forecast = db.scalar(select(Forecast.created_at).order_by(desc(Forecast.created_at)).limit(1))
        last_scan = db.scalar(select(Job.finished_at).where(Job.job_type == "scan", Job.status == "completed")
                              .order_by(desc(Job.finished_at)).limit(1))
        active_signals = db.scalar(select(Signal.id).where(
            Signal.status.in_(["APPROVED_FOR_PAPER", "ACTIVE_PAPER"]), Signal.expires_at > datetime.now(timezone.utc)
        ).limit(1)) is not None
        worker_rows = db.scalars(select(WorkerHeartbeat).order_by(desc(WorkerHeartbeat.last_seen)).limit(10)).all()
    now = datetime.now(timezone.utc)
    def worker_recent(roles: set[str]) -> bool:
        return any(
            x.role in roles
            and ((now - (x.last_seen.replace(tzinfo=timezone.utc) if x.last_seen.tzinfo is None else x.last_seen)).total_seconds() <= 45)
            for x in worker_rows
        )

    inference_worker_online = worker_recent({"inference", "all"})
    backtest_worker_online = worker_recent({"compute", "all"})
    market_worker_online = worker_recent({"market", "all"})
    compute_worker_online = inference_worker_online or backtest_worker_online
    local_kronos_source = (_repo_path() / "model" / "kronos.py").exists()
    return {
        "application": "KRONOS-MEXC-INTELLIGENCE",
        "mode": "RESEARCH_AND_PAPER_ONLY",
        "live_order_execution": False,
        "automatic_order_execution": False,
        "exchange": "MEXC USDT-M Perpetual Futures",
        "rest_base_url": cfg.mexc_api_base_url,
        "kronos_source_revision": cfg.kronos_repo_revision,
        "kronos_model_id": cfg.kronos_model_id,
        "kronos_model_revision": cfg.kronos_model_revision,
        "kronos_source_installed_on_api_service": local_kronos_source,
        "compute_worker_online": compute_worker_online,
        "inference_worker_online": inference_worker_online,
        "backtest_worker_online": backtest_worker_online,
        "market_worker_online": market_worker_online,
        "kronos_model_status": (
            "INFERENCE_WORKER_ONLINE_MODEL_LOADS_ON_FIRST_INFERENCE"
            if inference_worker_online else "INFERENCE_WORKER_NOT_SEEN_RECENTLY"
        ),
        "forecast_samples": cfg.forecast_sample_count,
        "forecast_horizon": cfg.forecast_horizon,
        "job_queue_has_pending": queued,
        "last_forecast_at": last_forecast.isoformat() if last_forecast else None,
        "last_market_scan_at": last_scan.isoformat() if last_scan else None,
        "active_paper_signals": active_signals,
        "workers": [{"worker_id": x.worker_id, "role": x.role, "pid": x.pid,
                     "status": x.status, "current_job_id": x.current_job_id,
                     "message": x.message, "last_seen": x.last_seen.isoformat()} for x in worker_rows],
        "whatsapp": WhatsAppCloudAPI().status().__dict__,
        "timeframes": ["1D", "12H (aggregated from 3 completed 4H bars)", "4H", "1H"],
    }


@router.get("/universe")
def universe(_: AdminUser):
    try:
        rows = eligible_universe()
        return {"count": len(rows), "items": rows}
    except Exception as exc:
        raise HTTPException(502, f"MEXC universe query failed: {exc}") from exc


@router.get("/market/{symbol}")
def market(symbol: str, _: AdminUser):
    symbol = normalize_symbol(symbol)
    try:
        with MexcPublicClient() as client:
            ticker = client.tickers(symbol)
            contract = client.contracts(symbol)
            funding = client.funding_rate(symbol)
            depth = client.depth(symbol, 20)
        return {
            "symbol": symbol,
            "ticker": ticker[0] if ticker else None,
            "contract": contract[0] if contract else None,
            "funding": funding,
            "depth": depth,
        }
    except Exception as exc:
        raise HTTPException(502, f"MEXC market query failed: {exc}") from exc


@router.get("/market/{symbol}/candles")
def candles(symbol: str, _: AdminUser,
            interval: str = Query(default="Min60", pattern="^(Min60|Hour4|Day1)$"),
            limit: int = Query(default=240, ge=10, le=2000)):
    symbol = normalize_symbol(symbol)
    seconds = {"Min60": 3600, "Hour4": 14400, "Day1": 86400}[interval]
    end = int(datetime.now(timezone.utc).timestamp())
    try:
        with MexcPublicClient() as client:
            frame = client.klines(symbol, interval, end - limit * seconds, end)
        return {"symbol": symbol, "interval": interval, "items": [
            {**row, "timestamp": row["timestamp"].isoformat()} for row in frame.to_dict(orient="records")
        ]}
    except Exception as exc:
        raise HTTPException(502, f"MEXC candle query failed: {exc}") from exc


@router.post("/analyze")
def analyze(body: AnalyzeRequest, _: AdminUser):
    symbol = normalize_symbol(body.symbol)
    job = enqueue_job("analysis", {"symbol": symbol})
    return job_to_dict(job)


@router.post("/forecast")
def forecast(body: ForecastRequest, _: AdminUser):
    symbol = normalize_symbol(body.symbol)
    job = enqueue_job("forecast", body.model_dump() | {"symbol": symbol})
    return job_to_dict(job)


@router.post("/backtests")
def backtest(body: BacktestRequest, _: AdminUser):
    requested = body.symbol.strip().upper()
    symbol = "ALL" if requested == "ALL" else normalize_symbol(requested)
    job = enqueue_job("backtest", body.model_dump() | {"symbol": symbol})
    return job_to_dict(job)


@router.post("/scan")
def scan(body: ScanRequest, _: AdminUser):
    if not body.confirm:
        raise HTTPException(400, "confirm=true is required")
    return job_to_dict(enqueue_job("scan", {"manual": True}))


@router.get("/jobs")
def jobs(_: AdminUser, limit: int = Query(default=30, ge=1, le=100)):
    with SessionLocal() as db:
        rows = db.scalars(select(Job).order_by(desc(Job.created_at)).limit(limit)).all()
        return {"items": [job_to_dict(x) for x in rows]}


@router.get("/jobs/{job_id}")
def job_detail(job_id: str, _: AdminUser):
    with SessionLocal() as db:
        row = db.get(Job, job_id)
        if not row:
            raise HTTPException(404, "Job not found")
        return job_to_dict(row)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, _: AdminUser):
    with SessionLocal.begin() as db:
        row = db.get(Job, job_id)
        if not row:
            raise HTTPException(404, "Job not found")
        if row.status != "queued":
            raise HTTPException(409, "Only queued jobs can be cancelled")
        row.status = "cancelled"
        row.finished_at = datetime.now(timezone.utc)
        row.progress_message = "Cancelled by administrator"
        return job_to_dict(row)


@router.get("/forecasts")
def forecasts(_: AdminUser, symbol: str | None = None, limit: int = Query(default=20, ge=1, le=100)):
    with SessionLocal() as db:
        stmt = select(Forecast).order_by(desc(Forecast.created_at)).limit(limit)
        if symbol:
            stmt = select(Forecast).where(Forecast.symbol == normalize_symbol(symbol)).order_by(desc(Forecast.created_at)).limit(limit)
        rows = db.scalars(stmt).all()
        return {"items": [{
            "id": x.id, "symbol": x.symbol, "timeframe": x.timeframe,
            "model_id": x.model_id, "model_revision": x.model_revision,
            "created_at": x.created_at.isoformat(), "input_end_time": x.input_end_time.isoformat(),
            "sample_count": x.sample_count, "horizon": x.horizon, "seed": x.seed,
            "summary": x.summary, "metadata": x.metadata_json,
        } for x in rows]}


@router.get("/forecasts/{forecast_id}")
def forecast_detail(forecast_id: str, _: AdminUser, include_paths: bool = False):
    with SessionLocal() as db:
        row = db.get(Forecast, forecast_id)
        if not row:
            raise HTTPException(404, "Forecast not found")
        result = {
            "id": row.id, "symbol": row.symbol, "timeframe": row.timeframe,
            "created_at": row.created_at.isoformat(), "input_end_time": row.input_end_time.isoformat(),
            "sample_count": row.sample_count, "horizon": row.horizon,
            "summary": row.summary, "metadata": row.metadata_json,
        }
        if include_paths:
            import gzip
            import numpy as np
            from io import BytesIO
            paths = np.load(BytesIO(gzip.decompress(row.paths_compressed)), allow_pickle=False)
            result["paths"] = paths.astype(float).tolist()
        return result


@router.post("/forecasts/{forecast_id}/evaluate")
def evaluate_forecast(forecast_id: str, _: AdminUser):
    with SessionLocal() as db:
        if db.get(Forecast, forecast_id) is None:
            raise HTTPException(404, "Forecast not found")
    job = enqueue_job("evaluate_forecast", {"forecast_id": forecast_id})
    return job_to_dict(job)


@router.get("/forecasts/{forecast_id}/evaluation")
def forecast_evaluation(forecast_id: str, _: AdminUser):
    with SessionLocal() as db:
        row = db.scalar(select(ForecastEvaluation).where(ForecastEvaluation.forecast_id == forecast_id))
        if not row:
            raise HTTPException(404, "Forecast has not been evaluated yet")
        return {"forecast_id": row.forecast_id, "evaluated_at": row.evaluated_at.isoformat(),
                "horizon": row.horizon, "metrics": row.metrics}


@router.get("/signals")
def signals(_: AdminUser, limit: int = Query(default=50, ge=1, le=200), status_filter: str | None = None):
    with SessionLocal() as db:
        stmt = select(Signal).order_by(desc(Signal.created_at)).limit(limit)
        if status_filter:
            stmt = select(Signal).where(Signal.status == status_filter).order_by(desc(Signal.created_at)).limit(limit)
        rows = db.scalars(stmt).all()
        return {"items": [{
            "id": x.id, "symbol": x.symbol, "side": x.side, "status": x.status,
            "entry": x.entry, "stop": x.stop, "tp1": x.tp1, "tp2": x.tp2,
            "score": x.score, "details": x.details,
            "created_at": x.created_at.isoformat(), "expires_at": x.expires_at.isoformat(),
        } for x in rows]}


@router.post("/signals/{signal_id}/dismiss")
def dismiss_signal(signal_id: str, _: AdminUser):
    with SessionLocal.begin() as db:
        row = db.get(Signal, signal_id)
        if not row:
            raise HTTPException(404, "Signal not found")
        row.status = "DISMISSED"
        return {"id": row.id, "status": row.status}


@router.get("/backtests")
def backtests(_: AdminUser, limit: int = Query(default=30, ge=1, le=100)):
    with SessionLocal() as db:
        rows = db.scalars(select(BacktestReport).order_by(desc(BacktestReport.created_at)).limit(limit)).all()
        return {"items": [{
            "id": x.id, "job_id": x.job_id, "symbol": x.symbol,
            "start_time": x.start_time.isoformat(), "end_time": x.end_time.isoformat(),
            "metrics": x.metrics, "warnings": x.warnings,
            "unresolved_positions": x.unresolved_positions,
        } for x in rows]}


@router.get("/backtests/{report_id}")
def backtest_detail(report_id: str, _: AdminUser):
    with SessionLocal() as db:
        row = db.get(BacktestReport, report_id)
        if not row:
            raise HTTPException(404, "Backtest report not found")
        return {
            "id": row.id, "job_id": row.job_id, "symbol": row.symbol,
            "start_time": row.start_time.isoformat(), "end_time": row.end_time.isoformat(),
            "metrics": row.metrics, "trades": row.trades,
            "unresolved_positions": row.unresolved_positions,
            "equity_curve": row.equity_curve, "warnings": row.warnings,
        }


@router.get("/backtests/{report_id}/trades.csv")
def backtest_csv(report_id: str, _: AdminUser):
    with SessionLocal() as db:
        row = db.get(BacktestReport, report_id)
        if not row:
            raise HTTPException(404, "Backtest report not found")
        trades = row.trades or []
    stream = io.StringIO()
    if trades:
        writer = csv.DictWriter(stream, fieldnames=list(trades[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(trades)
    else:
        stream.write("no_trades\n")
    stream.seek(0)
    return StreamingResponse(iter([stream.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{report_id}-trades.csv"'})


@router.get("/events")
def events(_: AdminUser, limit: int = Query(default=50, ge=1, le=200)):
    with SessionLocal() as db:
        rows = db.scalars(select(SystemEvent).order_by(desc(SystemEvent.created_at)).limit(limit)).all()
        return {"items": [{"id": x.id, "level": x.level, "event_type": x.event_type,
                            "message": x.message, "details": x.details,
                            "created_at": x.created_at.isoformat()} for x in rows]}


@router.get("/whatsapp/webhook", include_in_schema=False)
def verify_whatsapp_webhook(
    mode: str = Query(default="", alias="hub.mode"),
    verify_token: str = Query(default="", alias="hub.verify_token"),
    challenge: str = Query(default="", alias="hub.challenge"),
):
    cfg = get_settings()
    if not cfg.whatsapp_enabled or not cfg.whatsapp_verify_token:
        raise HTTPException(503, "WhatsApp webhook is not configured")
    if mode != "subscribe" or not secrets.compare_digest(verify_token, cfg.whatsapp_verify_token):
        raise HTTPException(403, "WhatsApp webhook verification failed")
    return PlainTextResponse(challenge, status_code=200)


@router.post("/whatsapp/webhook", include_in_schema=False)
async def receive_whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    cfg = get_settings()
    if not cfg.whatsapp_enabled:
        raise HTTPException(503, "WhatsApp integration is disabled")
    if not cfg.whatsapp_app_secret:
        raise HTTPException(503, "Set WHATSAPP_APP_SECRET (or META_APP_SECRET) before accepting webhooks")
    raw_body = await request.body()
    if len(raw_body) > 1_000_000:
        raise HTTPException(413, "Webhook payload is too large")
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_webhook_signature(raw_body, signature, cfg.whatsapp_app_secret):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Webhook body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Webhook body must be a JSON object")
    background_tasks.add_task(process_whatsapp_payload, payload, str(request.base_url).rstrip("/"))
    return {"accepted": True}


@router.get("/whatsapp/charts/{chart_id}.png", include_in_schema=False)
def whatsapp_chart_image(chart_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", chart_id):
        raise HTTPException(404, "Chart not found")
    path = get_settings().data_dir / "whatsapp_charts" / f"{chart_id}.png"
    if not path.is_file():
        raise HTTPException(404, "Chart not found or expired")
    if time.time() - path.stat().st_mtime > 48 * 3600:
        path.unlink(missing_ok=True)
        raise HTTPException(404, "Chart not found or expired")
    return FileResponse(path, media_type="image/png", filename=f"kronos-{chart_id}.png")


@router.get("/notifications/policy")
def notification_policy(_: AdminUser):
    return WhatsAppCloudAPI().status().__dict__
