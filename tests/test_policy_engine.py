"""Golden scenarios: the answers a reviewer will check by hand."""

from __future__ import annotations

from datetime import datetime

import pytest


# --------------------------------------------------------------------------
# cancellation
# --------------------------------------------------------------------------
def test_northstar_booked_cancellation_is_free_despite_being_past_30_minutes(engine):
    """ORD-1001: 2h after booking. SOP would charge INR 250; the signed
    agreement waives it. Contract must win, and the conflict must be visible."""
    f = engine.evaluate_cancellation("ORD-1001")
    assert f.decision == "cancellable_no_fee"
    assert f.facts["fee_inr"] == 0
    assert f.facts["policy_default_fee_inr"] == 250
    assert f.facts["contract_waiver_applied"] is True
    assert f.facts["minutes_since_booking"] == 120.0
    assert any("waives" in c for c in f.conflicts)
    assert f.citations[0]["doc_id"] == "CONTRACT-ACCT-001"  # contract cited first


def test_lumenworks_pays_the_default_fee_after_the_free_window(engine):
    f = engine.evaluate_cancellation("ORD-2001")
    assert f.decision == "cancellable_with_fee"
    assert f.facts["fee_inr"] == 250
    assert f.facts["minutes_since_booking"] == 75.0


def test_cancellation_inside_free_window_is_free_without_any_contract(engine):
    f = engine.evaluate_cancellation("ORD-3001")  # Beacon Retail, 15 minutes
    assert f.decision == "cancellable_no_fee"
    assert f.facts["fee_inr"] == 0


def test_picked_up_order_routes_to_return_to_origin(engine):
    f = engine.evaluate_cancellation("ORD-1002")
    assert f.decision == "not_cancellable"
    assert f.facts["alternative_workflow"] == "return-to-origin"


def test_delivered_order_cannot_be_cancelled(engine):
    assert engine.evaluate_cancellation("ORD-4001").decision == "not_cancellable"


def test_swiftship_booked_order_carries_the_ki_211_verification_warning(engine):
    f = engine.evaluate_cancellation("ORD-1001")  # SwiftShip
    assert any("KI-211" in v for v in f.verification_needed)


# --------------------------------------------------------------------------
# service credits
# --------------------------------------------------------------------------
def test_lumenworks_credit_uses_contract_threshold_and_flat_amount(engine):
    """ORD-2002: 4.5h late, carrier fault. Default SOP would pay 10% of 2400 =
    INR 240 above a 2h threshold; the agreement replaces both."""
    f = engine.evaluate_service_credit(order_id="ORD-2002")
    assert f.decision == "eligible"
    assert f.facts["credit_amount_inr"] == 300
    assert f.facts["threshold_hours"] == 4.0
    assert f.facts["amount_basis"] == "contract_flat"
    assert f.facts["hours_late"] == pytest.approx(4.5)


def test_hypothetical_three_hours_late_is_eligible_under_the_default_sop(engine):
    f = engine.evaluate_service_credit(
        hours_late=3, carrier_fault=True, customer_fault=False, shipment_fee_inr=2400
    )
    assert f.decision == "eligible"
    assert f.facts["credit_amount_inr"] == 240  # min(500, 10% of 2400)
    assert any("account" in a.lower() for a in f.assumptions)


def test_same_scenario_is_not_eligible_for_lumenworks(engine):
    f = engine.evaluate_service_credit(
        account_id="ACCT-002", hours_late=3, carrier_fault=True, customer_fault=False, shipment_fee_inr=2400
    )
    assert f.decision == "not_eligible"
    assert f.facts["threshold_hours"] == 4.0


def test_flat_cap_applies_to_expensive_shipments(engine):
    f = engine.evaluate_service_credit(
        hours_late=5, carrier_fault=True, customer_fault=False, shipment_fee_inr=20000
    )
    assert f.facts["credit_amount_inr"] == 500  # capped, not 2000


def test_unknown_fault_blocks_a_promise(engine):
    f = engine.evaluate_service_credit(hours_late=6, shipment_fee_inr=1000)
    assert f.decision == "unknown"
    assert f.requires_human is True
    assert "carrier_fault" in f.facts["unknown_inputs"]


def test_northstar_monthly_cap_is_surfaced(engine):
    f = engine.evaluate_service_credit(
        account_id="ACCT-001", hours_late=3, carrier_fault=True, customer_fault=False, shipment_fee_inr=4200
    )
    assert f.decision == "eligible"
    assert f.facts["monthly_aggregate_cap_inr"] == 5000
    assert any("5000" in v or "5,000" in v for v in f.verification_needed)


# --------------------------------------------------------------------------
# SLA
# --------------------------------------------------------------------------
def test_contract_sla_replaces_plan_default(engine):
    f = engine.resolve_sla_target("ACCT-001", "P1")
    assert f.facts["target"] == {"value": 15, "unit": "minutes", "clock": "24x7"}
    assert f.facts["policy_default"]["value"] == 30
    assert f.facts["source_kind"] == "contract_override"


def test_account_without_contract_uses_the_current_policy_table(engine):
    f = engine.resolve_sla_target("ACCT-004", "P1")  # Axis Labs, Enterprise
    assert f.facts["target"]["value"] == 30
    assert f.facts["source_kind"] == "policy_default"
    assert all(c["doc_id"] != "POL-SUPPORT-V2" for c in f.citations)  # never the deprecated table


def test_p1_breach_is_detected_for_northstar(services, engine):
    f = engine.ticket_sla_status(services.store.tickets["TKT-501"])
    assert f.facts["severity"] == "P1"
    assert f.decision == "breached"
    assert f.facts["due_at"] == "2026-08-16 10:45"
    assert f.facts["minutes_remaining"] == -15.0


def test_security_exposure_is_p1_and_breached(services, engine):
    f = engine.ticket_sla_status(services.store.tickets["TKT-505"])
    assert f.facts["severity"] == "P1"
    assert f.decision == "breached"
    assert f.facts["due_at"] == "2026-08-16 09:00"  # Enterprise 30 min, 24x7


def test_business_hours_clock_pauses_over_the_weekend(services, engine):
    """The snapshot is a Sunday. LumenWorks' P2 target is 4 business hours with
    no weekend coverage, so the deadline lands on Monday - not the same day."""
    f = engine.ticket_sla_status(services.store.tickets["TKT-502"])
    assert f.facts["severity"] == "P2"
    assert f.facts["due_at"] == "2026-08-17 13:00"
    assert f.decision == "on_track"
    assert any("Business hours assumed" in a for a in f.assumptions)


def test_closed_tickets_are_not_reported_as_breached(services, engine):
    f = engine.ticket_sla_status(services.store.tickets["TKT-450"])
    assert f.decision == "not_applicable_closed"


# --------------------------------------------------------------------------
# trust: historical guidance
# --------------------------------------------------------------------------
def test_past_cancellation_answer_is_flagged_as_wrong(services, engine):
    f = engine.check_historical_guidance(services.store.tickets["TKT-450"])
    assert f.decision == "contradicts_current_policy"
    assert f.facts["findings"][0]["type"] == "contradicts_contract"


def test_past_row_limit_answer_is_corrected_with_the_known_issue(services, engine):
    f = engine.check_historical_guidance(services.store.tickets["TKT-451"])
    assert f.decision == "contradicts_current_policy"
    assert "KI-208" in " ".join(f.conflicts)
    assert "5,000" in " ".join(f.conflicts)


def test_ticket_without_history_is_neutral(services, engine):
    f = engine.check_historical_guidance(services.store.tickets["TKT-501"])
    assert f.decision == "no_historical_guidance"


# --------------------------------------------------------------------------
# severity classification
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Every user gets HTTP 500 when creating any shipment", "P1"),
        ("An employee posted a production API key in a public channel", "P1"),
        ("Bulk upload fails for a 4,200-row CSV, one-by-one still works", "P2"),
        ("How do we change the billing contact?", "P3"),
    ],
)
def test_severity_signals(engine, text, expected):
    assert engine.classify_severity(text).decision == expected


def test_unmatched_text_defaults_to_p3_with_low_confidence(engine):
    f = engine.classify_severity("zxqv unmatched gibberish")
    assert f.decision == "P3"
    assert f.confidence == "low"
    assert f.requires_human is True


# --------------------------------------------------------------------------
# business calendar
# --------------------------------------------------------------------------
def test_business_minutes_skip_nights_and_weekends(engine):
    cal = engine.calendar
    friday_5pm = datetime(2026, 8, 14, 17, 0)
    assert cal.add(friday_5pm, 2, "business_hours") == datetime(2026, 8, 17, 10, 0)
    assert cal.business_minutes_between(friday_5pm, datetime(2026, 8, 17, 10, 0)) == 120
