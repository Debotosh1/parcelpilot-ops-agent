"""FastAPI surface: chat, confirmation, signals, audit."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import settings
from .rbac import AccessDenied
from .services import Services
from .tools.registry import ToolRegistry, execute_confirmed_action

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("parcelpilot")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title=settings.app_name, version="1.0.0")
services = Services.build()


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: str
    session_id: str = "default"


class ConfirmRequest(BaseModel):
    user_id: str
    session_id: str = "default"
    decision: str = Field(default="confirm", pattern="^(confirm|cancel)$")


class ToolRequest(BaseModel):
    user_id: str
    session_id: str = "default"
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "llm_configured": bool(services.llm),
        "model": settings.groq_model,
        "documents": len(services.pack.documents),
        "accounts": len(services.pack.accounts),
        "orders": len(services.pack.orders),
        "tickets": len(services.store.tickets),
    }


@app.get("/api/bootstrap")
def bootstrap() -> dict[str, Any]:
    return services.bootstrap()


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict[str, Any]:
    try:
        ctx = services.context(req.user_id, req.session_id)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=exc.to_dict()) from exc

    if services.llm is None:
        return {
            "answer": (
                "The chat agent needs a Groq API key. Set GROQ_API_KEY and restart; "
                "the Ops Signals tab works without it because detection is deterministic."
            ),
            "steps": [],
            "citations": [],
            "pending_action": None,
            "error": "llm_not_configured",
            "principal": ctx.principal.to_dict(),
        }

    services.store.audit(
        actor=ctx.principal.user_id,
        role=ctx.principal.role,
        event="chat_message",
        arguments={"message": req.message[:500]},
        session_id=req.session_id,
    )
    result = services.agent().run(req.message, ctx, history=services.history(req.session_id))
    services.remember(req.session_id, req.message, result.answer)
    payload = result.to_dict()
    payload["principal"] = ctx.principal.to_dict()
    return payload


@app.post("/api/actions/{action_id}/confirm")
def confirm_action(action_id: str, req: ConfirmRequest) -> dict[str, Any]:
    try:
        ctx = services.context(req.user_id, req.session_id)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=exc.to_dict()) from exc

    action = services.store.get_pending_action(action_id)
    if not action:
        raise HTTPException(status_code=404, detail={"message": f"Unknown action {action_id}"})

    if req.decision == "cancel":
        if action.status == "pending":
            action.status = "cancelled"
        services.store.audit(
            actor=ctx.principal.user_id, role=ctx.principal.role, event="action_cancelled",
            tool=action.tool_name, arguments=action.arguments, outcome="cancelled",
            detail=action_id, session_id=req.session_id,
        )
        return {"status": "cancelled", "action": action.to_dict()}

    if action.status == "expired":
        raise HTTPException(
            status_code=410,
            detail={"message": f"Action {action_id} expired; ask the agent to prepare it again."},
        )

    try:
        result = execute_confirmed_action(ctx, action_id)
    except AccessDenied as exc:
        services.store.audit(
            actor=ctx.principal.user_id, role=ctx.principal.role, event="action_execute_denied",
            tool=action.tool_name, arguments=action.arguments, outcome="access_denied",
            detail=str(exc), session_id=req.session_id,
        )
        raise HTTPException(status_code=403, detail=exc.to_dict()) from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc

    return {"status": "executed", "result": result, "action": action.to_dict()}


@app.get("/api/actions")
def list_actions(session_id: str = "default") -> dict[str, Any]:
    return {
        "pending": [a.to_dict() for a in services.store.open_actions_for_session(session_id)],
        "all": [a.to_dict() for a in services.store.pending_actions.values()],
    }


@app.get("/api/signals")
def signals(user_id: str) -> dict[str, Any]:
    try:
        principal = services.principal(user_id)
        found = services.signals.detect(principal)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=exc.to_dict()) from exc
    services.store.audit(actor=user_id, role=principal.role, event="signals_viewed", outcome="ok")
    counts: dict[str, int] = {}
    for sig in found:
        counts[sig["severity"]] = counts.get(sig["severity"], 0) + 1
    return {
        "generated_at": services.engine.snapshot.strftime("%Y-%m-%d %H:%M"),
        "scope": sorted(principal.allowed_accounts(services.pack)),
        "counts": counts,
        "signals": found,
    }


@app.post("/api/tool")
def call_tool(req: ToolRequest) -> dict[str, Any]:
    """Direct tool invocation - used by tests and by the UI's 'run tool' shortcuts."""
    try:
        ctx = services.context(req.user_id, req.session_id)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=exc.to_dict()) from exc
    return {"tool": req.tool, "result": ToolRegistry(ctx).call(req.tool, req.arguments)}


@app.get("/api/audit")
def audit(user_id: str, limit: int = 100) -> dict[str, Any]:
    principal = services.principal(user_id)
    entries = services.store.audit_log[-limit:][::-1]
    if not principal.can("audit.read"):
        entries = [e for e in entries if e["actor"] == principal.user_id]
    return {"count": len(entries), "entries": entries, "full_access": principal.can("audit.read")}


@app.get("/api/state")
def state() -> dict[str, Any]:
    return {
        "escalations": services.store.escalations,
        "tasks": services.store.tasks,
        "tickets": list(services.store.tickets.values()),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(AccessDenied)
def access_denied_handler(_request, exc: AccessDenied) -> JSONResponse:  # pragma: no cover
    return JSONResponse(status_code=403, content=exc.to_dict())
