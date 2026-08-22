"""The tool layer: the only way the agent touches data or changes state.

Three families, as required:

1. **Document retrieval** - `search_policy_documents`, authority-ranked.
2. **Structured lookup / calculation** - accounts, orders, tickets, plus the
   deterministic evaluators (`evaluate_cancellation`, `evaluate_service_credit`,
   `compute_sla_status`, `audit_historical_guidance`, `detect_operational_signals`).
3. **State-changing actions** - `propose_escalation`, `propose_ticket_update`,
   `propose_followup_task`. These *cannot* execute. They return a preview and a
   pending action id; execution happens only through the confirm endpoint after
   a human clicks Confirm.

Every call passes through `ToolRegistry.call`, which enforces permissions and
account scope, redacts where the role requires it, and writes an audit entry -
including for denials.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from ..loaders import DataPack
from ..policy_engine import PolicyEngine
from ..rbac import AccessDenied, Principal, redact_record, redact_records
from ..retrieval import DocumentIndex
from ..signals import SignalEngine
from ..store import OpsStore

ACTION_TOOLS = {"propose_escalation", "propose_ticket_update", "propose_followup_task"}


class ToolContext:
    """Per-request binding of the shared services to one principal/session."""

    def __init__(
        self,
        *,
        pack: DataPack,
        engine: PolicyEngine,
        index: DocumentIndex,
        store: OpsStore,
        signals: SignalEngine,
        principal: Principal,
        session_id: str,
    ):
        self.pack = pack
        self.engine = engine
        self.index = index
        self.store = store
        self.signals = signals
        self.principal = principal
        self.session_id = session_id


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _resolve_account(ctx: ToolContext, account_id: str | None, account_name: str | None) -> str | None:
    if account_id:
        return account_id.strip().upper()
    if account_name:
        acct = ctx.pack.account_by_name(account_name)
        if acct:
            return acct["account_id"]
        raise KeyError(f"No account matches '{account_name}'.")
    return None


def _scoped_ticket(ctx: ToolContext, ticket_id: str) -> dict[str, Any]:
    ticket = ctx.store.tickets.get(ticket_id.strip().upper())
    if not ticket:
        raise KeyError(f"Unknown ticket {ticket_id}")
    ctx.principal.require_account(ctx.pack, ticket["account_id"])
    return ticket


def _scoped_order(ctx: ToolContext, order_id: str) -> dict[str, Any]:
    order = ctx.pack.orders.get(order_id.strip().upper())
    if not order:
        raise KeyError(f"Unknown order {order_id}")
    ctx.principal.require_account(ctx.pack, order["account_id"])
    return order


def _read_permission(ctx: ToolContext) -> None:
    if not (
        ctx.principal.can("data.read_all")
        or ctx.principal.can("data.read_scoped")
        or ctx.principal.can("data.read_redacted")
    ):
        raise AccessDenied("This role cannot read operational data.", permission="data.read")


# ---------------------------------------------------------------------------
# 1. document retrieval
# ---------------------------------------------------------------------------
def search_policy_documents(
    ctx: ToolContext,
    query: str,
    doc_types: list[str] | None = None,
    account_id: str | None = None,
    include_deprecated: bool = False,
    limit: int = 5,
) -> dict[str, Any]:
    ctx.principal.require("docs.read")
    account_id = _resolve_account(ctx, account_id, None)
    if account_id:
        ctx.principal.require_account(ctx.pack, account_id)

    allowed = ctx.principal.allowed_accounts(ctx.pack)
    hits = ctx.index.search(
        query,
        allowed_account_ids=allowed,
        allow_confidential=ctx.principal.can("docs.read_contract"),
        doc_types=doc_types,
        include_deprecated=include_deprecated,
        limit=limit,
    )
    return {
        "query": query,
        "results": hits,
        "precedence_reminder": (
            "Signed customer agreement > current support policy/SOP > current product documentation > "
            "historical tickets (context only)."
        ),
        "excluded": (
            "Deprecated documents were excluded from this search."
            if not include_deprecated
            else "Deprecated documents INCLUDED - they must never be quoted as current policy."
        ),
    }


# ---------------------------------------------------------------------------
# 2. structured lookup / calculation
# ---------------------------------------------------------------------------
def get_account(ctx: ToolContext, account_id: str | None = None, account_name: str | None = None) -> dict[str, Any]:
    _read_permission(ctx)
    resolved = _resolve_account(ctx, account_id, account_name)
    if not resolved:
        raise ValueError("Provide account_id or account_name.")
    ctx.principal.require_account(ctx.pack, resolved)
    account = dict(ctx.pack.accounts[resolved])
    contract = ctx.pack.contract_overrides.get("accounts", {}).get(resolved)
    account["has_signed_agreement_in_pack"] = bool(contract)
    account["contract_doc_id"] = contract["doc_id"] if contract else None
    account["orders"] = [o["order_id"] for o in ctx.pack.orders.values() if o["account_id"] == resolved]
    account["open_tickets"] = [
        t["ticket_id"] for t in ctx.store.tickets.values() if t["account_id"] == resolved and t.get("status") == "open"
    ]
    return redact_record(account, ctx.principal)


def get_order(ctx: ToolContext, order_id: str) -> dict[str, Any]:
    _read_permission(ctx)
    order = dict(_scoped_order(ctx, order_id))
    order["account_name"] = ctx.pack.accounts[order["account_id"]]["account_name"]
    return redact_record(order, ctx.principal)


def get_ticket(ctx: ToolContext, ticket_id: str) -> dict[str, Any]:
    _read_permission(ctx)
    ticket = dict(_scoped_ticket(ctx, ticket_id))
    ticket["account_name"] = ctx.pack.accounts[ticket["account_id"]]["account_name"]
    if ticket.get("historical_resolution"):
        ticket["historical_resolution_warning"] = (
            "Historical resolutions are tier-5 context and may be wrong. Run audit_historical_guidance "
            "before reusing this answer."
        )
    return redact_record(ticket, ctx.principal)


def search_tickets(
    ctx: ToolContext,
    account_id: str | None = None,
    account_name: str | None = None,
    status: str | None = None,
    text: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    _read_permission(ctx)
    resolved = _resolve_account(ctx, account_id, account_name)
    if resolved:
        ctx.principal.require_account(ctx.pack, resolved)
    allowed = ctx.principal.allowed_accounts(ctx.pack)

    rows = []
    for ticket in ctx.store.tickets.values():
        if ticket["account_id"] not in allowed:
            continue
        if resolved and ticket["account_id"] != resolved:
            continue
        if status and ticket.get("status") != status:
            continue
        if text and text.lower() not in f"{ticket.get('subject','')} {ticket.get('description','')}".lower():
            continue
        row = dict(ticket)
        row["account_name"] = ctx.pack.accounts[ticket["account_id"]]["account_name"]
        rows.append(row)

    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return {
        "count": len(rows),
        "scope": sorted(allowed),
        "tickets": redact_records(rows[:limit], ctx.principal),
    }


def evaluate_cancellation(ctx: ToolContext, order_id: str) -> dict[str, Any]:
    _read_permission(ctx)
    _scoped_order(ctx, order_id)
    return ctx.engine.evaluate_cancellation(order_id.strip().upper()).to_dict()


def evaluate_service_credit(
    ctx: ToolContext,
    order_id: str | None = None,
    account_id: str | None = None,
    account_name: str | None = None,
    hours_late: float | None = None,
    carrier_fault: bool | None = None,
    customer_fault: bool | None = None,
    shipment_fee_inr: float | None = None,
) -> dict[str, Any]:
    _read_permission(ctx)
    resolved = _resolve_account(ctx, account_id, account_name)
    if order_id:
        order = _scoped_order(ctx, order_id)
        resolved = order["account_id"]
    elif resolved:
        ctx.principal.require_account(ctx.pack, resolved)
    finding = ctx.engine.evaluate_service_credit(
        order_id=order_id.strip().upper() if order_id else None,
        account_id=resolved,
        hours_late=hours_late,
        carrier_fault=carrier_fault,
        customer_fault=customer_fault,
        shipment_fee_inr=shipment_fee_inr,
    ).to_dict()
    amount = float(finding["facts"].get("credit_amount_inr") or 0)
    limit = ctx.principal.max_credit_without_approval()
    if finding["decision"] == "eligible" and amount > limit:
        finding["role_limit_note"] = (
            f"{ctx.principal.name} ({ctx.principal.role_label}) may authorise up to INR {limit:.0f} without "
            f"approval; INR {amount:.0f} needs an Ops Manager."
        )
        finding["requires_human"] = True
    return finding


def compute_sla_status(
    ctx: ToolContext,
    ticket_id: str | None = None,
    account_id: str | None = None,
    account_name: str | None = None,
    severity: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    _read_permission(ctx)
    if ticket_id:
        ticket = _scoped_ticket(ctx, ticket_id)
        return ctx.engine.ticket_sla_status(ticket).to_dict()
    resolved = _resolve_account(ctx, account_id, account_name)
    if not resolved or not severity:
        raise ValueError("Provide ticket_id, or account plus severity (and optionally created_at).")
    ctx.principal.require_account(ctx.pack, resolved)
    if created_at:
        return ctx.engine.sla_status(resolved, severity, created_at).to_dict()
    return ctx.engine.resolve_sla_target(resolved, severity).to_dict()


def audit_historical_guidance(ctx: ToolContext, ticket_id: str) -> dict[str, Any]:
    _read_permission(ctx)
    ticket = _scoped_ticket(ctx, ticket_id)
    return ctx.engine.check_historical_guidance(ticket).to_dict()


def detect_operational_signals(
    ctx: ToolContext, severity: str | None = None, signal_type: str | None = None, limit: int = 20
) -> dict[str, Any]:
    if not (ctx.principal.can("signals.read_all") or ctx.principal.can("signals.read_scoped")):
        raise AccessDenied("This role cannot read operational signals.", permission="signals.read")
    signals = ctx.signals.detect(ctx.principal)
    if severity:
        signals = [s for s in signals if s["severity"] == severity]
    if signal_type:
        signals = [s for s in signals if s["type"] == signal_type]
    return {
        "generated_at": ctx.engine.snapshot.strftime("%Y-%m-%d %H:%M"),
        "scope": sorted(ctx.principal.allowed_accounts(ctx.pack)),
        "count": len(signals),
        "signals": signals[:limit],
    }


# ---------------------------------------------------------------------------
# 3. state-changing actions (propose only)
# ---------------------------------------------------------------------------
def _propose(
    ctx: ToolContext, tool_name: str, arguments: dict[str, Any], preview: dict[str, Any], blocked_reason: str | None
) -> dict[str, Any]:
    ctx.principal.require("action.propose")
    action = ctx.store.create_pending_action(
        tool_name=tool_name,
        arguments=arguments,
        preview=preview,
        created_by=ctx.principal.user_id,
        session_id=ctx.session_id,
        blocked_reason=blocked_reason,
    )
    ctx.store.audit(
        actor=ctx.principal.user_id,
        role=ctx.principal.role,
        event="action_proposed",
        tool=tool_name,
        arguments=arguments,
        outcome="pending_confirmation",
        session_id=ctx.session_id,
        detail=action.action_id,
    )
    return {
        "status": "pending_confirmation",
        "action_id": action.action_id,
        "tool_name": tool_name,
        "preview": preview,
        "blocked_reason": blocked_reason,
        "expires_at": action.expires_at.isoformat(timespec="seconds"),
        "instruction": (
            "NOTHING HAS BEEN CREATED YET. Show this preview to the user and ask them to confirm. "
            "Only the Confirm control in the UI can execute it."
        ),
    }


def propose_escalation(
    ctx: ToolContext,
    ticket_id: str,
    severity: str,
    reason: str,
    escalate_to: str = "Duty Manager",
    proposed_credit_inr: float | None = None,
) -> dict[str, Any]:
    ticket = _scoped_ticket(ctx, ticket_id)
    account = ctx.pack.accounts[ticket["account_id"]]
    sla = ctx.engine.ticket_sla_status(ticket)
    blocked = None
    if proposed_credit_inr and proposed_credit_inr > ctx.principal.max_credit_without_approval():
        blocked = (
            f"Proposed credit INR {proposed_credit_inr:.0f} exceeds {ctx.principal.role_label} authority "
            f"(INR {ctx.principal.max_credit_without_approval():.0f}). An Ops Manager must approve."
        )
    preview = {
        "type": "escalation",
        "ticket_id": ticket["ticket_id"],
        "account": f"{account['account_id']} - {account['account_name']} ({account['plan']})",
        "severity": severity.upper(),
        "escalate_to": escalate_to,
        "reason": reason,
        "sla_state": sla.decision,
        "sla_detail": sla.summary,
        "proposed_credit_inr": proposed_credit_inr,
        "requested_by": ctx.principal.name,
    }
    return _propose(
        ctx,
        "propose_escalation",
        {
            "ticket_id": ticket["ticket_id"],
            "severity": severity.upper(),
            "reason": reason,
            "escalate_to": escalate_to,
            "proposed_credit_inr": proposed_credit_inr,
        },
        preview,
        blocked,
    )


def propose_ticket_update(
    ctx: ToolContext,
    ticket_id: str,
    status: str | None = None,
    severity: str | None = None,
    assigned_to: str | None = None,
    internal_note: str | None = None,
) -> dict[str, Any]:
    ticket = _scoped_ticket(ctx, ticket_id)
    updates = {
        k: v
        for k, v in {
            "status": status,
            "severity": severity and severity.upper(),
            "assigned_to": assigned_to,
            "internal_note": internal_note,
        }.items()
        if v is not None
    }
    if not updates:
        raise ValueError("Nothing to update: provide at least one of status, severity, assigned_to, internal_note.")
    preview = {
        "type": "ticket_update",
        "ticket_id": ticket["ticket_id"],
        "account": ticket["account_id"],
        "current": {k: ticket.get(k) for k in updates},
        "proposed": updates,
        "requested_by": ctx.principal.name,
    }
    return _propose(ctx, "propose_ticket_update", {"ticket_id": ticket["ticket_id"], "updates": updates}, preview, None)


def propose_followup_task(
    ctx: ToolContext,
    title: str,
    details: str,
    owner: str = "unassigned",
    due_at: str | None = None,
    ticket_id: str | None = None,
    order_id: str | None = None,
) -> dict[str, Any]:
    account_id = None
    if ticket_id:
        account_id = _scoped_ticket(ctx, ticket_id)["account_id"]
    if order_id:
        account_id = _scoped_order(ctx, order_id)["account_id"]
    preview = {
        "type": "followup_task",
        "title": title,
        "details": details,
        "owner": owner,
        "due_at": due_at,
        "ticket_id": ticket_id,
        "order_id": order_id,
        "account_id": account_id,
        "requested_by": ctx.principal.name,
    }
    return _propose(
        ctx,
        "propose_followup_task",
        {
            "title": title,
            "details": details,
            "owner": owner,
            "due_at": due_at,
            "ticket_id": ticket_id,
            "order_id": order_id,
            "account_id": account_id,
        },
        preview,
        None,
    )


# ---------------------------------------------------------------------------
# registry + JSON schemas handed to Groq
# ---------------------------------------------------------------------------
HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "search_policy_documents": search_policy_documents,
    "get_account": get_account,
    "get_order": get_order,
    "get_ticket": get_ticket,
    "search_tickets": search_tickets,
    "evaluate_cancellation": evaluate_cancellation,
    "evaluate_service_credit": evaluate_service_credit,
    "compute_sla_status": compute_sla_status,
    "audit_historical_guidance": audit_historical_guidance,
    "detect_operational_signals": detect_operational_signals,
    "propose_escalation": propose_escalation,
    "propose_ticket_update": propose_ticket_update,
    "propose_followup_task": propose_followup_task,
}

TOOL_FAMILY = {
    "search_policy_documents": "documents",
    "get_account": "structured",
    "get_order": "structured",
    "get_ticket": "structured",
    "search_tickets": "structured",
    "evaluate_cancellation": "calculation",
    "evaluate_service_credit": "calculation",
    "compute_sla_status": "calculation",
    "audit_historical_guidance": "trust",
    "detect_operational_signals": "monitoring",
    "propose_escalation": "action",
    "propose_ticket_update": "action",
    "propose_followup_task": "action",
}


def tool_specs() -> list[dict[str, Any]]:
    """OpenAI/Groq-compatible function schemas."""
    def fn(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required or [],
                    "additionalProperties": False,
                },
            },
        }

    return [
        fn(
            "search_policy_documents",
            "Search ParcelPilot policies, SOPs, product documentation and signed customer agreements. "
            "Results are ranked with signed agreements above current policy above product docs, and "
            "deprecated documents are excluded unless explicitly requested. Use this for any question "
            "about what the rules say.",
            {
                "query": {"type": "string", "description": "Natural-language search over the document corpus."},
                "doc_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["contract", "policy", "sop", "product_doc"]},
                    "description": "Optional filter by document type.",
                },
                "account_id": {"type": "string", "description": "Restrict/include an account's signed agreement, e.g. ACCT-001."},
                "include_deprecated": {
                    "type": "boolean",
                    "description": "Only set true when the user explicitly asks what an old/superseded policy said.",
                },
                "limit": {"type": "integer", "description": "Max chunks to return (default 5)."},
            },
            ["query"],
        ),
        fn(
            "get_account",
            "Look up one account: plan, status, CSM, whether a signed agreement exists, its orders and open tickets.",
            {
                "account_id": {"type": "string", "description": "e.g. ACCT-002"},
                "account_name": {"type": "string", "description": "e.g. LumenWorks (used when no id is known)"},
            },
        ),
        fn(
            "get_order",
            "Look up one shipment order: status, carrier, booking time, pickup window, actual pickup, fee, fault flags.",
            {"order_id": {"type": "string", "description": "e.g. ORD-1001"}},
            ["order_id"],
        ),
        fn(
            "get_ticket",
            "Look up one support ticket including any historical resolution (which may be wrong).",
            {"ticket_id": {"type": "string", "description": "e.g. TKT-501"}},
            ["ticket_id"],
        ),
        fn(
            "search_tickets",
            "List/filter support tickets within the caller's account scope.",
            {
                "account_id": {"type": "string"},
                "account_name": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "closed"]},
                "text": {"type": "string", "description": "Substring match on subject/description."},
                "limit": {"type": "integer"},
            },
        ),
        fn(
            "evaluate_cancellation",
            "Decide whether a specific order can be cancelled and what fee applies. Applies the signed "
            "agreement first, then the current SOP, and flags known issues that make the status unreliable. "
            "Always use this instead of reasoning about fees yourself.",
            {"order_id": {"type": "string"}},
            ["order_id"],
        ),
        fn(
            "evaluate_service_credit",
            "Decide failed-pickup service-credit eligibility and amount. Either pass order_id for a real order, "
            "or pass a hypothetical scenario (hours_late, carrier_fault, customer_fault, shipment_fee_inr, "
            "optionally the account). Returns 'unknown' when fault or timing is unresolved - never promise a "
            "credit in that case.",
            {
                "order_id": {"type": "string"},
                "account_id": {"type": "string"},
                "account_name": {"type": "string"},
                "hours_late": {"type": "number", "description": "Hours past the END of the scheduled pickup window."},
                "carrier_fault": {"type": "boolean"},
                "customer_fault": {"type": "boolean"},
                "shipment_fee_inr": {"type": "number"},
            },
        ),
        fn(
            "compute_sla_status",
            "Resolve the applicable first-response target (contract-aware) and, for a ticket, whether it is "
            "on track, at risk or breached at the dataset snapshot. Handles business-hours vs 24x7 clocks.",
            {
                "ticket_id": {"type": "string"},
                "account_id": {"type": "string"},
                "account_name": {"type": "string"},
                "severity": {"type": "string", "enum": ["P1", "P2", "P3"]},
                "created_at": {"type": "string", "description": "YYYY-MM-DD HH:MM if evaluating a hypothetical."},
            },
        ),
        fn(
            "audit_historical_guidance",
            "Check whether a closed ticket's past resolution still matches current authoritative rules. Run this "
            "before reusing any previous answer as precedent.",
            {"ticket_id": {"type": "string"}},
            ["ticket_id"],
        ),
        fn(
            "detect_operational_signals",
            "Sweep the whole support surface for issues nobody has asked about yet: SLA breaches, open P1s, "
            "known-issue clusters, repeated themes across customers, overdue pickups, unactioned cancellations, "
            "ticket spikes and past guidance that is now wrong.",
            {
                "severity": {"type": "string", "enum": ["critical", "high", "medium", "info"]},
                "signal_type": {"type": "string"},
                "limit": {"type": "integer"},
            },
        ),
        fn(
            "propose_escalation",
            "Prepare an escalation for human confirmation. This does NOT create anything: it returns a preview "
            "and an action_id, and the user must press Confirm in the UI.",
            {
                "ticket_id": {"type": "string"},
                "severity": {"type": "string", "enum": ["P1", "P2", "P3"]},
                "reason": {"type": "string", "description": "Why escalation is warranted, citing the rule."},
                "escalate_to": {"type": "string", "description": "e.g. Duty Manager, Security On-call, Carrier Ops"},
                "proposed_credit_inr": {"type": "number", "description": "Include when a credit is part of the ask."},
            },
            ["ticket_id", "severity", "reason"],
        ),
        fn(
            "propose_ticket_update",
            "Prepare a ticket update (status, severity, owner, internal note) for human confirmation. Does NOT apply it.",
            {
                "ticket_id": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "pending", "closed"]},
                "severity": {"type": "string", "enum": ["P1", "P2", "P3"]},
                "assigned_to": {"type": "string"},
                "internal_note": {"type": "string"},
            },
            ["ticket_id"],
        ),
        fn(
            "propose_followup_task",
            "Prepare a follow-up task (e.g. chase carrier, verify fault, check credit cap) for human confirmation. "
            "Does NOT create it.",
            {
                "title": {"type": "string"},
                "details": {"type": "string"},
                "owner": {"type": "string"},
                "due_at": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
                "ticket_id": {"type": "string"},
                "order_id": {"type": "string"},
            },
            ["title", "details"],
        ),
    ]


class ToolRegistry:
    """Dispatch + enforcement + audit."""

    def __init__(self, ctx: ToolContext):
        self.ctx = ctx

    @staticmethod
    def specs() -> list[dict[str, Any]]:
        return tool_specs()

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = HANDLERS.get(name)
        ctx = self.ctx
        if not handler:
            ctx.store.audit(
                actor=ctx.principal.user_id, role=ctx.principal.role, event="tool_call", tool=name,
                arguments=arguments, outcome="unknown_tool", session_id=ctx.session_id,
            )
            return {"error": "unknown_tool", "message": f"No tool named {name}."}

        started = datetime.utcnow()
        try:
            result = handler(ctx, **(arguments or {}))
            if name not in ACTION_TOOLS:
                ctx.store.audit(
                    actor=ctx.principal.user_id, role=ctx.principal.role, event="tool_call", tool=name,
                    arguments=arguments, outcome="ok", session_id=ctx.session_id,
                    detail=f"{(datetime.utcnow() - started).total_seconds() * 1000:.0f}ms",
                )
            return result
        except AccessDenied as exc:
            ctx.store.audit(
                actor=ctx.principal.user_id, role=ctx.principal.role, event="tool_call", tool=name,
                arguments=arguments, outcome="access_denied", detail=str(exc), session_id=ctx.session_id,
            )
            payload = exc.to_dict()
            payload["message"] += (
                " Tell the user plainly that this is outside their access scope and offer to route it to "
                "someone who is assigned to that account. Do not attempt another tool to work around it."
            )
            return payload
        except (KeyError, ValueError) as exc:
            ctx.store.audit(
                actor=ctx.principal.user_id, role=ctx.principal.role, event="tool_call", tool=name,
                arguments=arguments, outcome="error", detail=str(exc), session_id=ctx.session_id,
            )
            return {"error": "invalid_request", "message": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            ctx.store.audit(
                actor=ctx.principal.user_id, role=ctx.principal.role, event="tool_call", tool=name,
                arguments=arguments, outcome="exception", detail=repr(exc), session_id=ctx.session_id,
            )
            return {"error": "tool_failure", "message": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# execution of a confirmed action (never reachable from the model)
# ---------------------------------------------------------------------------
def execute_confirmed_action(ctx: ToolContext, action_id: str) -> dict[str, Any]:
    action = ctx.store.get_pending_action(action_id)
    if not action:
        raise KeyError(f"Unknown action {action_id}")
    if action.status != "pending":
        raise ValueError(f"Action {action_id} is already {action.status}.")
    if action.created_by != ctx.principal.user_id:
        raise AccessDenied(
            f"{ctx.principal.name} cannot confirm an action proposed for {action.created_by}.",
            resource=action_id,
        )
    ctx.principal.require("action.execute")
    if action.blocked_reason and not ctx.principal.can("action.approve_credit"):
        raise AccessDenied(action.blocked_reason, permission="action.approve_credit", resource=action_id)

    args = action.arguments
    if action.tool_name == "propose_escalation":
        ctx.principal.require_account(ctx.pack, ctx.store.tickets[args["ticket_id"]]["account_id"])
        result = ctx.store.create_escalation(
            {
                "ticket_id": args["ticket_id"],
                "severity": args["severity"],
                "reason": args["reason"],
                "escalate_to": args.get("escalate_to", "Duty Manager"),
                "proposed_credit_inr": args.get("proposed_credit_inr"),
                "raised_by": ctx.principal.user_id,
                "approved_by": ctx.principal.user_id,
            }
        )
    elif action.tool_name == "propose_ticket_update":
        ctx.principal.require_account(ctx.pack, ctx.store.tickets[args["ticket_id"]]["account_id"])
        result = ctx.store.update_ticket(args["ticket_id"], args["updates"], ctx.principal.user_id)
    elif action.tool_name == "propose_followup_task":
        if args.get("account_id"):
            ctx.principal.require_account(ctx.pack, args["account_id"])
        result = ctx.store.create_task({**args, "created_by": ctx.principal.user_id})
    else:  # pragma: no cover - defensive
        raise ValueError(f"Action type {action.tool_name} cannot be executed.")

    action.status = "executed"
    action.result = result
    ctx.store.audit(
        actor=ctx.principal.user_id, role=ctx.principal.role, event="action_executed",
        tool=action.tool_name, arguments=args, outcome="ok", detail=action_id, session_id=ctx.session_id,
    )
    return result
