"""Agent loop and HTTP surface, driven by a scripted LLM (no network)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.agent import OpsAgent
from app.llm import LLMError
from tests.conftest import FakeLLM


# --------------------------------------------------------------------------
# agent loop
# --------------------------------------------------------------------------
def test_agent_chains_tools_and_reports_the_engine_numbers(services):
    llm = FakeLLM(
        [
            {"tool": "get_order", "arguments": {"order_id": "ORD-1001"}},
            {"tool": "evaluate_cancellation", "arguments": {"order_id": "ORD-1001"}},
            {"content": "No fee - the signed agreement waives it."},
        ]
    )
    result = OpsAgent(llm).run("Can Northstar cancel ORD-1001 for free?", services.context("maya", "s1"))
    assert [s["tool"] for s in result.steps] == ["get_order", "evaluate_cancellation"]
    assert [s["family"] for s in result.steps] == ["structured", "calculation"]
    assert result.steps[1]["result"]["facts"]["fee_inr"] == 0
    assert any(c["doc_id"] == "CONTRACT-ACCT-001" for c in result.citations)


def test_agent_surfaces_denials_instead_of_data(services):
    llm = FakeLLM(
        [
            {"tool": "get_order", "arguments": {"order_id": "ORD-2001"}},
            {"content": "That account is outside your scope."},
        ]
    )
    result = OpsAgent(llm).run("What's on ORD-2001?", services.context("rohit", "s1"))
    assert result.steps[0]["denied"] is True
    assert "LumenWorks" not in str(result.steps[0]["result"].get("carrier", ""))


def test_agent_action_is_only_ever_a_proposal(services):
    llm = FakeLLM(
        [
            {"tool": "propose_escalation", "arguments": {"ticket_id": "TKT-501", "severity": "P1", "reason": "P1"}},
            {"content": "Ready to escalate - confirm?"},
        ]
    )
    result = OpsAgent(llm).run("Escalate TKT-501", services.context("rohit", "s1"))
    assert result.pending_action["status"] == "pending_confirmation"
    assert services.store.escalations == []


def test_agent_stops_at_the_step_limit(services):
    llm = FakeLLM([{"tool": "get_order", "arguments": {"order_id": "ORD-1001"}}] * 10)
    result = OpsAgent(llm, max_steps=3).run("loop", services.context("maya", "s1"))
    assert result.error == "max_steps_exhausted"
    assert len(result.steps) == 3


def test_llm_failure_degrades_honestly(services):
    class Broken:
        def chat(self, messages, tools=None):
            raise LLMError("rate limited")

    result = OpsAgent(Broken()).run("anything", services.context("maya", "s1"))
    assert result.error == "rate limited"
    assert "could not reach" in result.answer.lower()


def test_system_prompt_pins_scope_and_snapshot(services):
    prompt = OpsAgent(FakeLLM([])).build_system_prompt(services.context("rohit", "s1"))
    assert "2026-08-16 11:00" in prompt
    assert "ACCT-001, ACCT-003, ACCT-004" in prompt
    assert "signed customer agreement >" in prompt


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------
def client_with(script):
    from app import main

    main.services = main.Services.build(llm=FakeLLM(script))
    return TestClient(main.app), main


def test_health_and_bootstrap():
    client, _ = client_with([])
    assert client.get("/api/health").json()["status"] == "ok"
    boot = client.get("/api/bootstrap").json()
    assert {u["user_id"] for u in boot["users"]} == {"rohit", "maya", "priya", "sam"}
    assert boot["snapshot"] == "2026-08-16 11:00"


def test_chat_then_confirm_executes_the_action():
    client, main = client_with(
        [
            {"tool": "propose_escalation", "arguments": {"ticket_id": "TKT-501", "severity": "P1", "reason": "P1 outage"}},
            {"content": "Prepared - confirm to escalate."},
        ]
    )
    chat = client.post("/api/chat", json={"message": "escalate TKT-501", "user_id": "rohit", "session_id": "s1"}).json()
    action_id = chat["pending_action"]["action_id"]
    assert main.services.store.escalations == []

    done = client.post(f"/api/actions/{action_id}/confirm", json={"user_id": "rohit", "session_id": "s1"}).json()
    assert done["status"] == "executed"
    assert main.services.store.escalations[0]["ticket_id"] == "TKT-501"


def test_cancelling_an_action_changes_nothing():
    client, main = client_with(
        [
            {"tool": "propose_ticket_update", "arguments": {"ticket_id": "TKT-503", "status": "closed"}},
            {"content": "Confirm?"},
        ]
    )
    chat = client.post("/api/chat", json={"message": "close TKT-503", "user_id": "rohit", "session_id": "s1"}).json()
    action_id = chat["pending_action"]["action_id"]
    out = client.post(
        f"/api/actions/{action_id}/confirm", json={"user_id": "rohit", "session_id": "s1", "decision": "cancel"}
    ).json()
    assert out["status"] == "cancelled"
    assert main.services.store.tickets["TKT-503"]["status"] == "open"


def test_confirm_is_rejected_for_a_different_user():
    client, _ = client_with(
        [
            {"tool": "propose_ticket_update", "arguments": {"ticket_id": "TKT-501", "status": "pending"}},
            {"content": "Confirm?"},
        ]
    )
    chat = client.post("/api/chat", json={"message": "update", "user_id": "rohit", "session_id": "s1"}).json()
    res = client.post(
        f"/api/actions/{chat['pending_action']['action_id']}/confirm",
        json={"user_id": "maya", "session_id": "s1"},
    )
    assert res.status_code == 403


def test_signals_endpoint_is_role_scoped():
    client, _ = client_with([])
    rohit = client.get("/api/signals", params={"user_id": "rohit"}).json()
    priya = client.get("/api/signals", params={"user_id": "priya"}).json()
    assert rohit["scope"] == ["ACCT-001", "ACCT-003", "ACCT-004"]
    assert len(priya["signals"]) > len(rohit["signals"])


def test_audit_endpoint_hides_other_users_from_non_managers():
    client, _ = client_with([])
    client.post("/api/tool", json={"user_id": "maya", "tool": "get_order", "arguments": {"order_id": "ORD-2001"}})
    client.post("/api/tool", json={"user_id": "rohit", "tool": "get_order", "arguments": {"order_id": "ORD-1001"}})
    rohit_view = client.get("/api/audit", params={"user_id": "rohit"}).json()
    priya_view = client.get("/api/audit", params={"user_id": "priya"}).json()
    assert {e["actor"] for e in rohit_view["entries"]} == {"rohit"}
    assert {"rohit", "maya"} <= {e["actor"] for e in priya_view["entries"]}
    assert priya_view["full_access"] is True


def test_unknown_user_is_rejected():
    client, _ = client_with([])
    res = client.post("/api/chat", json={"message": "hi", "user_id": "mallory", "session_id": "s1"})
    assert res.status_code == 403
