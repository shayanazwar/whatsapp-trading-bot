"""FastAPI webhook/health entry point. Long-running command processing is backgrounded."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import mimetypes
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from app.config import Settings, load_settings
from app.mexc.client import MexcClient
from app.services.command_processor import CommandProcessor
from app.commands import (
    CommandError, WHATSAPP_SUPPORT_COMMANDS, WHATSAPP_SUPPORT_HELP_TEXT, parse_command,
)
from app.services.scheduler import ResearchScheduler
from app.storage import Store
from app.whatsapp import (
    WHATSAPP_POLICY_BLOCKED_REPLY,
    WhatsAppCloud,
    WhatsAppPolicyBlocked,
    extract_incoming_messages,
    extract_status_updates,
    sender_key,
    verify_webhook_signature,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)



_RESEARCH_DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pak Trading Academy — MEXC Research Dashboard</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{font:16px system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 14px;background:#101418;color:#edf2f7}
h1{font-size:clamp(28px,6vw,44px);margin-bottom:8px}.muted{color:#aab7c4;line-height:1.5}.card{background:#1a222b;border:1px solid #34404d;border-radius:14px;padding:18px;margin:16px 0}
input,button{font:inherit;border-radius:9px;padding:12px;border:1px solid #4b5968}input{background:#0e1318;color:#fff;width:100%;margin:7px 0 12px}button{background:#d6f36a;color:#151a10;font-weight:700;cursor:pointer;margin:4px 5px 4px 0}button:disabled{opacity:.55;cursor:wait}button.secondary{background:#303b47;color:#fff}.grid{display:flex;flex-wrap:wrap;gap:3px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0b1015;padding:14px;border-radius:8px;min-height:100px;line-height:1.45}.result-image{display:block;width:100%;height:auto;border-radius:10px;margin:12px 0;border:1px solid #34404d}.download-link{display:inline-block;color:#d6f36a;word-break:break-word;margin:8px 0}.tag{display:inline-block;color:#aab7c4;font-size:13px;margin:5px 0}
</style></head><body><h1>MEXC Research Dashboard</h1>
<p class="muted">MEXC Futures research and historical paper simulations using completed candles only. Signal strategy: 1D → 12H → 4H → 1H. Charts: 1H, 4H, 12H and 1D only. Backtests report missing data and insufficient history explicitly; they never fabricate candles. Live orders are disabled.</p>
<section class="card"><label for="token">WEB_API_TOKEN</label><input id="token" type="password" autocomplete="off" placeholder="Paste the token configured in Render">
<label for="command">Command</label><input id="command" value="PRICE BTCUSDT" maxlength="160">
<div class="grid">
<button data-cmd="PRICE BTCUSDT">BTC price</button><button data-cmd="ANALYZE BTCUSDT">Analyze BTC</button><button data-cmd="CHART BTCUSDT 1H">Chart 1H</button><button data-cmd="CHART BTCUSDT 4H">Chart 4H</button><button data-cmd="CHART BTCUSDT 12H">Chart 12H</button><button data-cmd="CHART BTCUSDT 1D">Chart 1D</button>
<button data-cmd="SEARCH PEPE">Search PEPE</button><button data-cmd="PAPER">Paper wallet</button><button data-cmd="PAPER OPEN">Open paper positions</button><button data-cmd="PAPER HISTORY">Paper history</button>
<button data-cmd="SCAN">Scan</button><button data-cmd="BACKTEST 1D">Backtest 1D</button><button data-cmd="BACKTEST 7D">Backtest 7D control</button><button data-cmd="BACKTEST 7D TP=1.5R">TP 1.5R</button><button data-cmd="BACKTEST 7D TP=2.0R">TP 2.0R</button><button data-cmd="BACKTEST 7D TP=2.5R">TP 2.5R</button><button data-cmd="BACKTEST 30D">Backtest 30D</button><button data-cmd="BACKTEST 60D">Backtest 60D</button><button data-cmd="BACKTEST 90D">Backtest 90D</button><button data-cmd="BACKTEST 180D">Backtest 180D</button><button data-cmd="BACKTEST 365D">Backtest 365D</button><button data-cmd="STATUS">Status</button><button data-cmd="SIGNALS">Signals</button><button data-cmd="PERFORMANCE">Performance</button><button data-cmd="PNL">Paper P&amp;L</button><button data-cmd="RISK">Risk</button><button data-cmd="HELP" class="secondary">Help</button>
</div><button id="run">Run command</button><button id="clear" class="secondary">Clear token</button></section>
<section class="card"><strong id="status">Ready</strong><div id="mode" class="tag"></div><pre id="output">Results will appear here.</pre><div id="attachments"></div></section>
<script>
const token=document.getElementById('token'), command=document.getElementById('command'), output=document.getElementById('output'), status=document.getElementById('status'), mode=document.getElementById('mode'), runButton=document.getElementById('run'), attachmentBox=document.getElementById('attachments');
let running=false, artifactUrls=[];
function clearArtifacts(){for(const u of artifactUrls)URL.revokeObjectURL(u);artifactUrls=[];attachmentBox.replaceChildren();}
function setBusy(value){running=value;runButton.disabled=value;document.querySelectorAll('[data-cmd]').forEach(b=>b.disabled=value);}
async function showArtifacts(items, bearer){clearArtifacts();for(const a of items||[]){const response=await fetch(a.url,{headers:{'Authorization':'Bearer '+bearer}});if(!response.ok){const p=document.createElement('p');p.textContent='Could not load attachment '+(a.name||'file')+' (HTTP '+response.status+').';attachmentBox.appendChild(p);continue;}const objectUrl=URL.createObjectURL(await response.blob());artifactUrls.push(objectUrl);if(a.type==='image'){const img=document.createElement('img');img.className='result-image';img.alt=a.name||'Generated chart';img.src=objectUrl;attachmentBox.appendChild(img);}else{const link=document.createElement('a');link.className='download-link';link.href=objectUrl;link.download=a.name||'research-report.txt';link.textContent='Download report: '+(a.name||'research-report.txt');attachmentBox.appendChild(link);}}}
async function runCommand(preset){if(running)return;if(typeof preset==='string')command.value=preset;const requested=command.value.trim();if(!requested){status.textContent='Command required';output.textContent='Choose a command or type one above.';command.focus();return;}const bearer=token.value.trim();if(!bearer){status.textContent='Token required';output.textContent='Paste WEB_API_TOKEN from Render, then run the command.';token.focus();return;}clearArtifacts();setBusy(true);status.textContent='Running '+requested+'…';mode.textContent='';output.textContent='Waiting for server response…';try{const r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+bearer},body:JSON.stringify({command:requested})});const raw=await r.text();let d={};try{d=raw?JSON.parse(raw):{};}catch(_){d={detail:raw||'The server returned an empty or invalid response.'};}if(!r.ok)throw new Error(d.detail||('Request failed (HTTP '+r.status+')'));status.textContent=(d.command||requested)+' · '+(d.mode||'completed');mode.textContent='Server-generated result · live orders disabled';output.textContent=typeof d.result==='string'?d.result:JSON.stringify(d.result??d,null,2);await showArtifacts(d.attachments||[],bearer);}catch(e){status.textContent='Request failed';output.textContent=String(e.message||e);}finally{setBusy(false);}}
document.querySelectorAll('[data-cmd]').forEach(b=>b.addEventListener('click',()=>runCommand(b.dataset.cmd)));runButton.addEventListener('click',()=>runCommand());command.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();runCommand();}});document.getElementById('clear').addEventListener('click',()=>{token.value='';status.textContent='Token cleared';mode.textContent='';output.textContent='Paste the token again before running a command.';clearArtifacts();token.focus();});
</script></body></html>"""


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(cfg.database_url or cfg.database_path)
        mexc = MexcClient(base_url=cfg.mexc_rest_base_url, timeout_seconds=cfg.http_timeout_seconds,
                          max_retries=cfg.http_max_retries)
        whatsapp = WhatsAppCloud(cfg)
        processor = CommandProcessor(cfg, store, mexc, whatsapp)
        scheduler = ResearchScheduler(cfg, store, mexc, whatsapp)
        app.state.settings = cfg
        app.state.store = store
        app.state.mexc = mexc
        app.state.whatsapp = whatsapp
        app.state.processor = processor
        app.state.scheduler = scheduler
        app.state.scheduler_task = None
        app.state.background_tasks = set()
        app.state.artifacts = {}
        if cfg.scheduler_enabled:
            app.state.scheduler_task = asyncio.create_task(scheduler.run_forever(), name="research-scheduler")
        else:
            store.set_state("scheduler_status", "disabled")
        logger.info(
            "APPLICATION STARTUP COMPLETE | policy_gate=%s allowlist_configured=%s live_execution=false",
            "open" if cfg.whatsapp_policy_gate_open else "closed",
            bool(cfg.whatsapp_allowed_senders),
        )
        # Recover pending rows for allowlisted senders and dispatch all commands normally.
        if cfg.whatsapp_allowed_senders:
            allowed_recovery_senders = {sender_key(x) for x in cfg.whatsapp_allowed_senders}
            for row in store.pending_inbound(limit=50):
                try:
                    payload = json.loads(row["payload_json"])
                    msgs = extract_incoming_messages(payload)
                    msg = next((m for m in msgs if m["message_id"] == row["message_id"]), None)
                    if not msg:
                        continue
                    if sender_key(msg.get("from", "")) not in allowed_recovery_senders:
                        store.mark_inbound_processed(row["message_id"], "unauthorized_sender_dropped_on_recovery")
                    else:
                        _route_allowed_message(app, msg, row["message_id"])
                except Exception:
                    logger.warning("Could not requeue one stored inbound webhook row")
        yield
        scheduler.stop()
        scheduler_task = app.state.scheduler_task
        if scheduler_task is not None:
            scheduler_task.cancel()
            await asyncio.gather(scheduler_task, return_exceptions=True)
        tasks = list(app.state.background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await mexc.aclose()
        await whatsapp.aclose()

    app = FastAPI(title="MEXC Futures Research Bot", version="0.3.0", lifespan=lifespan)

    @app.api_route("/", methods=["GET", "HEAD"])
    async def root() -> dict[str, Any]:
        return {
            "service": "MEXC Futures Research Bot",
            "mode": "research/paper",
            "live_execution": "disabled",
            "health": "/healthz",
            "health_legacy": "/health",
            "whatsapp_webhook": "/webhook/whatsapp",
            "whatsapp_webhook_legacy": "/webhook",
        }

    @app.get("/dashboard", response_class=HTMLResponse)
    async def research_dashboard() -> HTMLResponse:
        # Token is entered by the operator and held only in page memory, not browser storage.
        return HTMLResponse(content=_RESEARCH_DASHBOARD_HTML)

    @app.post("/api/command")
    async def web_research_command(request: Request) -> dict[str, Any]:
        settings: Settings = app.state.settings
        configured_token = settings.web_api_token
        _authorize_dashboard_request(request, configured_token)
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("command"), str):
            raise HTTPException(status_code=422, detail="Provide a command string")
        command_text = payload["command"].strip()
        if len(command_text) > 160:
            raise HTTPException(status_code=422, detail="Command is too long")
        try:
            parsed = parse_command(command_text)
        except CommandError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        allowed_web_commands = {
            "HELP", "STATUS", "PRICE", "MARKET", "CHART", "ANALYZE", "SCAN", "BACKTEST",
            "SIGNALS", "PERFORMANCE", "PNL", "RISK", "LIVE_STATUS",
            "OPEN_POSITIONS", "POSITIONS", "PAPER", "SEARCH",
        }
        if parsed.name not in allowed_web_commands:
            raise HTTPException(status_code=403, detail="This command is not available in the read-only web interface")
        try:
            result = await app.state.processor.process_text(
                "authenticated-web-dashboard", command_text, enforce_whatsapp_policy=False
            )
        except CommandError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Web research command failed | command=%s", parsed.name)
            raise HTTPException(status_code=502, detail="Command failed. Check service logs for details.") from exc
        attachment_items: list[dict[str, str]] = []
        report_root = settings.reports_dir.resolve()
        # Expire short-lived artifact links and bound the in-memory registry.
        for key, item in list(app.state.artifacts.items()):
            if item["expires_at"] < time.time():
                app.state.artifacts.pop(key, None)
        for file_path, media_type in result.attachments:
            path = Path(file_path).resolve()
            try:
                path.relative_to(report_root)
            except ValueError:
                logger.warning("Refusing artifact outside reports directory: %s", path.name)
                continue
            if not path.is_file() or media_type not in {"image", "document", "json"}:
                continue
            artifact_id = secrets.token_urlsafe(18)
            app.state.artifacts[artifact_id] = {
                "path": str(path), "type": media_type, "name": path.name,
                "expires_at": time.time() + 3600,
            }
            attachment_items.append({
                "id": artifact_id, "type": media_type, "name": path.name,
                "url": f"/api/artifacts/{artifact_id}",
            })
        # Keep only the most recent 100 expiring handles.
        if len(app.state.artifacts) > 100:
            oldest = sorted(app.state.artifacts, key=lambda k: app.state.artifacts[k]["expires_at"])
            for key in oldest[:-100]:
                app.state.artifacts.pop(key, None)
        return {
            "command": parsed.name, "result": result.text,
            "mode": "research/paper; live execution disabled", "attachments": attachment_items,
        }

    @app.get("/api/artifacts/{artifact_id}")
    async def web_research_artifact(artifact_id: str, request: Request) -> FileResponse:
        settings: Settings = app.state.settings
        _authorize_dashboard_request(request, settings.web_api_token)
        item = app.state.artifacts.get(artifact_id)
        if not item or item["expires_at"] < time.time():
            app.state.artifacts.pop(artifact_id, None)
            raise HTTPException(status_code=404, detail="Artifact expired or not found; run the command again.")
        path = Path(item["path"]).resolve()
        try:
            path.relative_to(settings.reports_dir.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Artifact file is missing")
        media_type = "image/png" if item["type"] == "image" else (mimetypes.guess_type(path.name)[0] or "text/plain")
        return FileResponse(path, media_type=media_type, filename=item["name"], headers={"Cache-Control": "no-store"})

    @app.api_route("/healthz", methods=["GET", "HEAD"])
    @app.api_route("/health", methods=["GET", "HEAD"])
    async def healthz() -> dict[str, Any]:
        store: Store = app.state.store
        try:
            store.recent_events(1)
            db = "ok"
        except Exception:
            db = "error"
        settings: Settings = app.state.settings
        return {
            "status": "ok" if db == "ok" else "degraded",
            "database": db,
            "scheduler_enabled": settings.scheduler_enabled,
            "scheduler_worker": store.get_state("scheduler_status", "not_started"),
            "scheduler_last_success": store.get_state("scheduler_last_success"),
            "paper_scheduler_gate": store.get_state("paper_scheduler_gate", "not_evaluated"),
            "paper_trading_enabled": settings.paper_trading_enabled,
            "data_alignment_probe_approved": settings.data_alignment_probe_approved,
            "auto_trading_enabled": settings.auto_trading_enabled,
            "live_execution_enabled": False,
            "live_execution_build_released": settings.live_execution_build_released,
            "whatsapp_policy_gate": "open" if settings.whatsapp_policy_gate_open else "closed",
        }

    @app.get("/webhook", response_class=PlainTextResponse)
    @app.get("/webhook/whatsapp", response_class=PlainTextResponse)
    async def whatsapp_verify(
        hub_mode: str | None = Query(None, alias="hub.mode"),
        hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
        hub_challenge: str | None = Query(None, alias="hub.challenge"),
    ) -> PlainTextResponse:
        settings: Settings = app.state.settings
        if hub_mode == "subscribe" and settings.meta_verify_token and hub_verify_token and hmac.compare_digest(
            settings.meta_verify_token, hub_verify_token
        ) and hub_challenge is not None:
            return PlainTextResponse(hub_challenge, status_code=200)
        raise HTTPException(status_code=403, detail="Webhook verification failed")

    @app.post("/webhook")
    @app.post("/webhook/whatsapp")
    async def whatsapp_inbound(request: Request) -> dict[str, str]:
        settings: Settings = app.state.settings
        raw_body = await request.body()
        if len(raw_body) > 1_000_000:
            raise HTTPException(status_code=413, detail="Payload too large")
        signature = request.headers.get("X-Hub-Signature-256")
        if not verify_webhook_signature(raw_body, signature, settings.meta_app_secret):
            raise HTTPException(status_code=403, detail="Webhook signature invalid")
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Malformed JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be an object")
        messages = extract_incoming_messages(payload)
        status_updates = extract_status_updates(payload)
        store: Store = app.state.store
        logger.info(
            "WHATSAPP WEBHOOK PARSED | inbound_messages=%d status_updates=%d object=%s",
            len(messages), len(status_updates), str(payload.get("object", "unknown"))[:80],
        )
        if not messages:
            logger.info(
                "WHATSAPP WEBHOOK HAS NO INBOUND MESSAGES | status_updates=%d",
                len(status_updates),
            )
        for update in status_updates:
            provider_id = update.get("provider_message_id", "")
            status = update.get("status", "")
            if provider_id and status:
                store.record_outbox_status(
                    provider_id, status, update.get("error"),
                    details={
                        "recipient_id": update.get("recipient_id"),
                        "timestamp": update.get("timestamp"),
                        "pricing": update.get("pricing", {}),
                    },
                )
        allowed = {sender_key(x) for x in settings.whatsapp_allowed_senders}
        allowed.discard("")
        for msg in messages:
            message_id = msg.get("message_id", "")
            sender = sender_key(msg.get("from", ""))

            if (settings.meta_phone_number_id and msg.get("phone_number_id")
                    and msg["phone_number_id"] != settings.meta_phone_number_id):
                store.append_event(
                    "WHATSAPP_WRONG_PHONE_NUMBER_ID",
                    {"message_id": message_id, "received_phone_number_id": msg["phone_number_id"]},
                    idempotency_key=f"wrong-phone-id:{message_id}",
                )
                logger.warning(
                    "WHATSAPP INBOUND IGNORED | reason=phone_number_id_mismatch message_id=%s",
                    message_id,
                )
                continue

            if not message_id or not sender:
                store.append_event("WHATSAPP_INVALID_INBOUND", {"reason": "missing id/sender"})
                logger.warning("WHATSAPP INBOUND IGNORED | reason=missing_message_id_or_sender")
                continue

            logger.info(
                "WHATSAPP INBOUND RECEIVED | message_id=%s sender_suffix=%s type=%s",
                message_id, sender[-4:], msg.get("type", "unknown"),
            )
            accepted = store.save_inbound(message_id, sender, msg.get("body", ""), payload)
            if not accepted:
                store.append_event(
                    "WHATSAPP_DUPLICATE_INBOUND", {"message_id": message_id},
                    idempotency_key=f"duplicate-inbound:{message_id}",
                )
                logger.info("WHATSAPP INBOUND IGNORED | reason=duplicate message_id=%s", message_id)
                continue

            # Always enforce the sender allowlist before any reply or command.
            if not allowed or sender not in allowed:
                store.mark_inbound_processed(message_id, "unauthorized_sender_dropped")
                store.append_event(
                    "WHATSAPP_UNAUTHORIZED_SENDER", {"message_id": message_id},
                    idempotency_key=f"unauthorized:{message_id}",
                )
                logger.warning(
                    "WHATSAPP INBOUND IGNORED | reason=sender_not_allowlisted message_id=%s sender_suffix=%s",
                    message_id, sender[-4:],
                )
                continue

            _route_allowed_message(app, msg, message_id)
        # Persist all useful information first and acknowledge quickly; no strategy work happens inline.
        return {"status": "accepted"}

    return app


def _authorize_dashboard_request(request: Request, configured_token: str) -> None:
    if not configured_token:
        raise HTTPException(status_code=503, detail="Web research API is not configured. Set WEB_API_TOKEN in Render.")
    authorization = request.headers.get("Authorization", "")
    scheme, _, supplied_token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not supplied_token or not hmac.compare_digest(configured_token, supplied_token):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _is_support_command(body: str) -> bool:
    try:
        return parse_command(body).name in WHATSAPP_SUPPORT_COMMANDS
    except CommandError:
        return False


def _spawn_task(app: FastAPI, coroutine, *, name: str) -> None:
    task = asyncio.create_task(coroutine, name=name)
    app.state.background_tasks.add(task)

    def finished(done: asyncio.Task) -> None:
        app.state.background_tasks.discard(done)
        if done.cancelled():
            return
        try:
            done.result()
        except Exception as exc:
            logger.error(
                "WHATSAPP BACKGROUND TASK FAILED | task=%s error_type=%s",
                name, type(exc).__name__,
            )

    task.add_done_callback(finished)


def _spawn_message_task(app: FastAPI, message: dict[str, str], message_id: str) -> None:
    _spawn_task(
        app,
        _process_one_message(app, message, message_id),
        name=f"whatsapp-command-{message_id}",
    )


def _spawn_policy_status_task(app: FastAPI, message: dict[str, str], message_id: str) -> None:
    _spawn_task(
        app,
        _send_policy_status_reply(app, message, message_id),
        name=f"whatsapp-status-{message_id}",
    )


def _spawn_support_command_task(app: FastAPI, message: dict[str, str], message_id: str) -> None:
    _spawn_task(
        app,
        _send_support_command_reply(app, message, message_id),
        name=f"whatsapp-support-{message_id}",
    )


def _route_allowed_message(app: FastAPI, message: dict[str, str], message_id: str) -> None:
    """Route commands according to the explicit WhatsApp policy-clearance gate."""
    settings: Settings = app.state.settings
    if settings.whatsapp_policy_gate_open:
        _spawn_message_task(app, message, message_id)
    elif _is_support_command(message.get("body", "")):
        _spawn_support_command_task(app, message, message_id)
    else:
        app.state.store.append_event(
            "WHATSAPP_POLICY_GATE_BLOCKED",
            {"message_id": message_id, "command_token": (message.get("body", "").split() or [""])[0].upper()[:24]},
            idempotency_key=f"wa-policy-blocked:{message_id}",
        )
        _spawn_policy_status_task(app, message, message_id)


async def _send_support_command_reply(app: FastAPI, message: dict[str, str], message_id: str) -> None:
    """Serve HELP/STATUS only while the crypto-research messaging gate is closed."""
    store: Store = app.state.store
    sender = sender_key(message.get("from", ""))
    outbox_id: int | None = None
    try:
        command = parse_command(message.get("body", ""))
        if command.name not in WHATSAPP_SUPPORT_COMMANDS:
            raise CommandError("Only support commands are permitted while the policy gate is closed")
        if command.name == "HELP":
            reply = WHATSAPP_SUPPORT_HELP_TEXT
        else:
            result = await app.state.processor.execute(command)
            reply = result.text
        idempotency_key = f"wa-support:{message_id}"
        outbox_id = store.enqueue_outbox(
            sender, reply, category="service", idempotency_key=idempotency_key
        )
        provider_result = await app.state.whatsapp.send_support_text(
            sender, reply, command=command.name
        )
        provider_messages = provider_result.get("messages", [])
        provider_id = (
            provider_messages[0].get("id")
            if provider_messages and isinstance(provider_messages[0], dict)
            else None
        )
        store.record_outbox_attempt(outbox_id, accepted=True, provider_message_id=provider_id)
        store.append_event(
            "WHATSAPP_SUPPORT_MESSAGE_ACCEPTED_BY_META",
            {"message_id": message_id, "command": command.name,
             "provider_message_id": provider_id, "outbox_id": outbox_id,
             "delivery_confirmed": False},
            idempotency_key=f"wa-support-accepted:{message_id}",
        )
        store.mark_inbound_processed(message_id, "support_reply_accepted_by_meta")
    except Exception as exc:
        if outbox_id is not None:
            try:
                store.mark_outbox_unknown(outbox_id, type(exc).__name__)
            except Exception:
                logger.warning("WHATSAPP SUPPORT OUTBOX UPDATE FAILED | message_id=%s", message_id)
        store.append_event(
            "WHATSAPP_SUPPORT_MESSAGE_FAILED",
            {"message_id": message_id, "error_type": type(exc).__name__},
            idempotency_key=f"wa-support-failed:{message_id}",
        )
        store.mark_inbound_processed(message_id, f"support_reply_failed:{type(exc).__name__}")
        logger.warning(
            "WHATSAPP SUPPORT REPLY FAILED | message_id=%s error_type=%s",
            message_id, type(exc).__name__,
        )


async def _send_policy_status_reply(app: FastAPI, message: dict[str, str], message_id: str) -> None:
    """Send the policy-status response; never execute the blocked user command."""
    store: Store = app.state.store
    sender = sender_key(message.get("from", ""))
    outbox_id = None
    generic_reply = WHATSAPP_POLICY_BLOCKED_REPLY
    response_key = f"wa-policy-status:{message_id}"
    try:
        outbox_id = store.enqueue_outbox(
            sender, generic_reply, category="service", idempotency_key=response_key
        )
        result = await app.state.whatsapp.send_service_status(sender)
        provider_messages = result.get("messages", [])
        provider_id = (
            provider_messages[0].get("id")
            if provider_messages and isinstance(provider_messages[0], dict)
            else None
        )
        store.record_outbox_attempt(
            outbox_id, accepted=True, provider_message_id=provider_id
        )
        store.append_event(
            "WHATSAPP_SERVICE_STATUS_ACCEPTED_BY_META",
            {"message_id": message_id, "provider_message_id": provider_id,
             "outbox_id": outbox_id, "delivery_confirmed": False},
            idempotency_key=f"wa-service-status-accepted:{message_id}",
        )
        store.mark_inbound_processed(
            message_id, "policy_gate_closed_service_status_sent"
        )
        logger.info(
            "WHATSAPP SERVICE REPLY ACCEPTED BY META | message_id=%s provider_id_present=%s",
            message_id, bool(provider_id),
        )
    except Exception as exc:
        if outbox_id is not None:
            try:
                store.mark_outbox_unknown(outbox_id, type(exc).__name__)
            except Exception:
                logger.warning("WHATSAPP OUTBOX UPDATE FAILED | message_id=%s", message_id)
        store.append_event(
            "WHATSAPP_SERVICE_STATUS_FAILED",
            {"message_id": message_id, "error_type": type(exc).__name__},
            idempotency_key=f"wa-service-status-failed:{message_id}",
        )
        store.mark_inbound_processed(
            message_id, f"service_status_failed:{type(exc).__name__}"
        )
        logger.warning(
            "WHATSAPP SERVICE REPLY FAILED | message_id=%s error_type=%s",
            message_id, type(exc).__name__,
        )


async def _process_one_message(app: FastAPI, message: dict[str, str], message_id: str) -> None:
    store: Store = app.state.store
    settings: Settings = app.state.settings
    processor: CommandProcessor = app.state.processor
    sender = message.get("from", "")
    body = message.get("body", "")
    try:
        if message.get("type") != "text" or not body.strip():
            store.mark_inbound_processed(message_id, "unsupported_message_type")
            return
        timestamp_raw = message.get("timestamp", "")
        try:
            message_time = int(timestamp_raw)
        except (TypeError, ValueError):
            message_time = None
        logger.info(
            "WHATSAPP COMMAND STARTED | message_id=%s command_token=%s",
            message_id, (body.split()[0].upper()[:24] if body.split() else "EMPTY"),
        )
        result = await processor.process_text(sender, body, received_timestamp=message_time, enforce_whatsapp_policy=True)
        logger.info(
            "WHATSAPP COMMAND PROCESSED | message_id=%s response_chars=%d attachments=%d",
            message_id, len(result.text), len(result.attachments),
        )
        response_key = f"wa-reply:{message_id}:text"
        outbox_id = store.enqueue_outbox(sender, result.text, category="service", idempotency_key=response_key)
        try:
            provider_result = await app.state.whatsapp.send_text(sender, result.text)
            provider_messages = provider_result.get("messages", [])
            provider_id = provider_messages[0].get("id") if provider_messages and isinstance(provider_messages[0], dict) else None
            store.record_outbox_attempt(outbox_id, accepted=True, provider_message_id=provider_id)
            store.append_event("WHATSAPP_MESSAGE_ACCEPTED_BY_META", {"recipient": sender, "provider_message_id": provider_id,
                "outbox_id": outbox_id, "category": "service", "delivery_confirmed": False},
                idempotency_key=f"wa-accepted:{response_key}")
            logger.info(
                "WHATSAPP REPLY ACCEPTED BY META | message_id=%s provider_id_present=%s",
                message_id, bool(provider_id),
            )
        except Exception as exc:
            # A timeout may have happened after Meta accepted the message; mark unknown and do not blind retry.
            store.mark_outbox_unknown(outbox_id, type(exc).__name__)
            store.append_event("WHATSAPP_SEND_UNKNOWN", {"outbox_id": outbox_id, "error_type": type(exc).__name__},
                               idempotency_key=f"wa-send-unknown:{response_key}")
            store.mark_inbound_processed(message_id, "reply_outcome_unknown_no_retry")
            return
        for path, media_type in result.attachments:
            if media_type not in {"image", "document"}:
                continue
            media_key = f"wa-reply:{message_id}:{media_type}:{path}"
            media_outbox_id = store.enqueue_outbox(sender, f"[{media_type.upper()}] {Path(path).name}",
                category="service", idempotency_key=media_key)
            try:
                media_result = await app.state.whatsapp.send_media(sender, path, media_type=media_type)
                media_messages = media_result.get("messages", [])
                media_provider_id = media_messages[0].get("id") if media_messages and isinstance(media_messages[0], dict) else None
                store.record_outbox_attempt(media_outbox_id, accepted=True, provider_message_id=media_provider_id)
                store.append_event("WHATSAPP_MEDIA_ACCEPTED_BY_META", {"message_id": message_id,
                    "media_type": media_type, "provider_message_id": media_provider_id, "outbox_id": media_outbox_id,
                    "delivery_confirmed": False}, idempotency_key=f"wa-media-accepted:{message_id}:{media_type}:{path}")
            except Exception as exc:
                store.mark_outbox_unknown(media_outbox_id, type(exc).__name__)
                store.append_event("WHATSAPP_MEDIA_SEND_UNKNOWN", {"message_id": message_id, "media_type": media_type,
                    "error_type": type(exc).__name__}, idempotency_key=f"wa-media-unknown:{message_id}:{path}")
        store.mark_inbound_processed(message_id, "completed")
    except WhatsAppPolicyBlocked:
        store.mark_inbound_processed(message_id, "crypto_policy_gate_closed")
    except Exception as exc:
        store.append_event("WHATSAPP_COMMAND_FAILED", {"message_id": message_id, "error_type": type(exc).__name__},
                           idempotency_key=f"wa-command-failed:{message_id}")
        store.mark_inbound_processed(message_id, f"failed:{type(exc).__name__}")
        logger.warning(
            "WHATSAPP COMMAND FAILED | message_id=%s error_type=%s",
            message_id, type(exc).__name__,
        )


app = create_app()
