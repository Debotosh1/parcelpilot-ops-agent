"""Access control is enforced in the tool layer, not by prompt instruction."""

from __future__ import annotations

import pytest

from app.rbac import AccessDenied
from app.tools.registry import ToolRegistry, execute_confirmed_action


def reg(services, user, session="s1"):
    return ToolRegistry(services.context(user, session))


# --------------------------------------------------------------------------
# account scoping
# --------------------------------------------------------------------------
def test_agent_cannot_read_an_order_outside_assigned_accounts(services):
    out = reg(services, "rohit").call("get_order", {"order_id": "ORD-2001"})  # ACCT-002
    assert out["error"] == "access_denied"
    assert "ACCT-002" in out["message"]


def test_manager_can_read_every_account(services):
    assert reg(services, "priya").call("get_order", {"order_id": "ORD-2001"})["account_id"] == "ACCT-002"


def test_ticket_search_only_returns_in_scope_accounts(services):
    out = reg(services, "rohit").call("search_tickets", {})
    assert {t["account_id"] for t in out["tickets"]} <= {"ACCT-001", "ACCT-003", "ACCT-004"}
    assert out["scope"] == ["ACCT-001", "ACCT-003", "ACCT-004"]


def test_other_customers_contract_is_not_retrievable(services):
    hits = reg(services, "rohit").call(
        "search_policy_documents", {"query": "LumenWorks failed pickup credit fixed INR 300"}
    )["results"]
    assert all(h["doc_id"] != "CONTRACT-ACCT-002" for h in hits)


def test_the_same_query_returns_the_contract_for_an_assigned_agent(services):
    hits = reg(services, "maya").call(
        "search_policy_documents", {"query": "LumenWorks failed pickup credit fixed INR 300"}
    )["results"]
    assert any(h["doc_id"] == "CONTRACT-ACCT-002" for h in hits)


def test_evaluators_refuse_out_of_scope_records(services):
    assert reg(services, "rohit").call("evaluate_cancellation", {"order_id": "ORD-2001"})["error"] == "access_denied"
    assert reg(services, "rohit").call("compute_sla_status", {"ticket_id": "TKT-502"})["error"] == "access_denied"


# --------------------------------------------------------------------------
# roles
# --------------------------------------------------------------------------
def test_readonly_analyst_sees_redacted_customer_content(services):
    ticket = reg(services, "sam").call("get_ticket", {"ticket_id": "TKT-501"})
    assert "redacted" in ticket["description"]
    assert ticket["account_id"] == "ACCT-001"  # metadata still usable for ops work


def test_readonly_analyst_cannot_propose_actions(services):
    out = reg(services, "sam").call("propose_escalation", {"ticket_id": "TKT-501", "severity": "P1", "reason": "x"})
    assert out["error"] == "access_denied"
    assert out["permission"] == "action.propose"


def test_signals_are_scoped_per_role(services):
    rohit = services.signals.detect(services.principal("rohit"))
    priya = services.signals.detect(services.principal("priya"))
    assert all("ACCT-002" not in s["accounts"] for s in rohit)
    assert any("ACCT-002" in s["accounts"] for s in priya)


# --------------------------------------------------------------------------
# confirmation gate
# --------------------------------------------------------------------------
def test_proposing_an_action_changes_nothing(services):
    out = reg(services, "rohit").call(
        "propose_escalation", {"ticket_id": "TKT-501", "severity": "P1", "reason": "P1 outage"}
    )
    assert out["status"] == "pending_confirmation"
    assert services.store.escalations == []
    assert services.store.get_pending_action(out["action_id"]).status == "pending"


def test_confirmation_executes_exactly_once(services):
    ctx = services.context("rohit", "s1")
    out = ToolRegistry(ctx).call(
        "propose_escalation", {"ticket_id": "TKT-501", "severity": "P1", "reason": "P1 outage"}
    )
    execute_confirmed_action(ctx, out["action_id"])
    assert len(services.store.escalations) == 1
    with pytest.raises(ValueError):
        execute_confirmed_action(ctx, out["action_id"])


def test_another_user_cannot_confirm_someone_elses_action(services):
    out = ToolRegistry(services.context("rohit", "s1")).call(
        "propose_ticket_update", {"ticket_id": "TKT-501", "status": "pending"}
    )
    with pytest.raises(AccessDenied):
        execute_confirmed_action(services.context("maya", "s2"), out["action_id"])


def test_credit_above_agent_authority_needs_a_manager(services):
    ctx = services.context("rohit", "s1")
    out = ToolRegistry(ctx).call(
        "propose_escalation",
        {"ticket_id": "TKT-501", "severity": "P1", "reason": "goodwill credit", "proposed_credit_inr": 2500},
    )
    assert out["blocked_reason"]
    with pytest.raises(AccessDenied):
        execute_confirmed_action(ctx, out["action_id"])


def test_expired_actions_cannot_execute(services, monkeypatch):
    ctx = services.context("rohit", "s1")
    out = ToolRegistry(ctx).call("propose_followup_task", {"title": "chase carrier", "details": "RoadRunner"})
    action = services.store.get_pending_action(out["action_id"])
    action.expires_at = action.created_at  # force expiry
    assert services.store.get_pending_action(out["action_id"]).status == "expired"
    with pytest.raises(ValueError):
        execute_confirmed_action(ctx, out["action_id"])


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------
def test_every_call_including_denials_is_audited(services):
    before = len(services.store.audit_log)
    reg(services, "rohit").call("get_order", {"order_id": "ORD-2001"})
    reg(services, "rohit").call("get_order", {"order_id": "ORD-1001"})
    entries = services.store.audit_log[before:]
    assert [e["outcome"] for e in entries] == ["access_denied", "ok"]
    assert all(e["actor"] == "rohit" for e in entries)
