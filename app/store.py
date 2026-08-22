"""Mutable operational state + audit trail.

The supplied pack is read-only reference data. Anything the agent *changes*
(escalations, ticket updates, follow-up tasks) lands here, together with an
append-only audit log of every tool call, denial, proposal and execution.

In-memory on purpose: the assessment needs a demonstrable, inspectable
transaction path, not a database. Swapping this class for Postgres touches one
file (see ARCHITECTURE.md, "Trade-offs").
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import settings
from .loaders import DataPack


@dataclass
class PendingAction:
    action_id: str
    tool_name: str
    arguments: dict[str, Any]
    preview: dict[str, Any]
    created_by: str
    session_id: str
    created_at: datetime
    expires_at: datetime
    status: str = "pending"  # pending | executed | cancelled | expired
    blocked_reason: str | None = None
    result: dict[str, Any] | None = None

    @property
    def is_open(self) -> bool:
        return self.status == "pending" and datetime.utcnow() < self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "preview": self.preview,
            "created_by": self.created_by,
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "expires_at": self.expires_at.isoformat(timespec="seconds"),
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "result": self.result,
        }


class OpsStore:
    def __init__(self, pack: DataPack):
        self.pack = pack
        self._lock = threading.Lock()
        self._counters = {
            "escalation": itertools.count(1),
            "task": itertools.count(1),
            "action": itertools.count(1),
        }
        # Working copies so the pack on disk is never mutated.
        self.tickets: dict[str, dict[str, Any]] = {k: dict(v) for k, v in pack.tickets.items()}
        self.escalations: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.audit_log: list[dict[str, Any]] = []
        self.pending_actions: dict[str, PendingAction] = {}

    # -- ids ---------------------------------------------------------------
    def _next(self, kind: str, prefix: str) -> str:
        return f"{prefix}-{next(self._counters[kind]):04d}"

    # -- audit -------------------------------------------------------------
    def audit(
        self,
        *,
        actor: str,
        role: str,
        event: str,
        tool: str | None = None,
        arguments: dict[str, Any] | None = None,
        outcome: str = "ok",
        detail: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "seq": len(self.audit_log) + 1,
            "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "actor": actor,
            "role": role,
            "event": event,
            "tool": tool,
            "arguments": arguments or {},
            "outcome": outcome,
            "detail": detail,
            "session_id": session_id,
        }
        with self._lock:
            self.audit_log.append(entry)
        return entry

    # -- pending actions ---------------------------------------------------
    def create_pending_action(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        preview: dict[str, Any],
        created_by: str,
        session_id: str,
        blocked_reason: str | None = None,
    ) -> PendingAction:
        now = datetime.utcnow()
        action = PendingAction(
            action_id=self._next("action", "ACT"),
            tool_name=tool_name,
            arguments=arguments,
            preview=preview,
            created_by=created_by,
            session_id=session_id,
            created_at=now,
            expires_at=now + timedelta(seconds=settings.pending_action_ttl_s),
            blocked_reason=blocked_reason,
        )
        with self._lock:
            self.pending_actions[action.action_id] = action
        return action

    def get_pending_action(self, action_id: str) -> PendingAction | None:
        action = self.pending_actions.get(action_id)
        if action and action.status == "pending" and datetime.utcnow() >= action.expires_at:
            action.status = "expired"
        return action

    def open_actions_for_session(self, session_id: str) -> list[PendingAction]:
        return [a for a in self.pending_actions.values() if a.session_id == session_id and a.is_open]

    # -- state changes -----------------------------------------------------
    def create_escalation(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            record = {
                "escalation_id": self._next("escalation", "ESC"),
                "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                **payload,
            }
            self.escalations.append(record)
            ticket = self.tickets.get(payload.get("ticket_id", ""))
            if ticket:
                ticket["escalated"] = True
                ticket["escalation_id"] = record["escalation_id"]
                ticket["severity"] = payload.get("severity", ticket.get("severity"))
        return record

    def update_ticket(self, ticket_id: str, updates: dict[str, Any], actor: str) -> dict[str, Any]:
        with self._lock:
            ticket = self.tickets.get(ticket_id)
            if not ticket:
                raise KeyError(f"Unknown ticket {ticket_id}")
            before = {k: ticket.get(k) for k in updates}
            ticket.update(updates)
            ticket["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            ticket["updated_by"] = actor
        return {"ticket_id": ticket_id, "before": before, "after": updates, "ticket": ticket}

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            record = {
                "task_id": self._next("task", "TASK"),
                "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "status": "open",
                **payload,
            }
            self.tasks.append(record)
        return record
