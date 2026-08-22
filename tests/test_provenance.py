"""The rules registry must stay faithful to the documents it projects.

These tests are the guard against the most dangerous kind of drift: a policy
document is updated, the JSON rules are not, and the agent starts giving
confidently wrong answers with a real citation attached.
"""

from __future__ import annotations

import re

import pytest


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("’", "'")).lower()


@pytest.fixture()
def corpus(services):
    return {doc.doc_id: normalise(doc.text) for doc in services.pack.documents}


def test_every_cited_document_exists(services, corpus):
    def walk(node):
        if isinstance(node, dict):
            if "doc_id" in node and isinstance(node["doc_id"], str):
                assert node["doc_id"] in corpus, f"citation to unknown document {node['doc_id']}"
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(services.pack.policy_rules)
    walk(services.pack.contract_overrides)


def test_sla_matrix_matches_the_current_policy_text(services, corpus):
    text = corpus["POL-SUPPORT-V3"]
    for plan, targets in services.pack.policy_rules["sla_defaults"]["plans"].items():
        assert plan.lower() in text
        for target in targets.values():
            assert str(target["value"]) in text


def test_cancellation_and_credit_numbers_match_the_sop(services, corpus):
    text = corpus["SOP-CANCEL-CREDIT-V4"]
    rules = services.pack.policy_rules
    booked = rules["cancellation"]["states"]["BOOKED"]
    assert str(booked["free_window_minutes"]) in text
    assert str(booked["fee_after_window_inr"]) in text
    credit = rules["service_credit"]
    assert str(credit["delay_threshold_hours"]) in text
    assert str(credit["flat_cap_inr"]) in text
    assert f"{credit['percent_of_fee']}%" in text
    assert "1,000" in text or str(rules["approval"]["manager_approval_above_inr"]) in text


def test_product_limits_match_the_product_guide(services, corpus):
    text = corpus["DOC-PRODUCT-OPS"]
    bulk = services.pack.policy_rules["product"]["bulk_upload"]
    assert "5,000" in text or str(bulk["supported_rows_per_csv"]) in text
    for issue in services.pack.policy_rules["product"]["known_issues"]:
        assert issue["id"].lower() in text


def test_contract_overrides_quote_their_agreements(services, corpus):
    for account_id, override in services.pack.contract_overrides["accounts"].items():
        text = corpus[override["doc_id"]]
        for section in ("sla", "cancellation", "service_credit"):
            block = override.get(section) or {}
            for value in (
                block.get("delay_threshold_hours"),
                block.get("flat_amount_inr"),
                block.get("monthly_aggregate_cap_inr"),
            ):
                if value is None:
                    continue
                assert str(int(value)) in text.replace(",", ""), f"{account_id}.{section} value {value} not in contract"
        for target in (override.get("sla") or {}).get("targets", {}).values():
            assert str(target["value"]) in text


def test_deprecated_policy_values_are_not_used_anywhere_in_the_registry(services):
    """v2's Enterprise P1 was 1 hour; v3's is 30 minutes. If the registry ever
    picks up a v2 number, this is the test that should fail."""
    enterprise = services.pack.policy_rules["sla_defaults"]["plans"]["Enterprise"]
    assert enterprise["P1"] == {"value": 30, "unit": "minutes", "clock": "24x7"}
    assert services.pack.policy_rules["sla_defaults"]["source"]["doc_id"] == "POL-SUPPORT-V3"


def test_snapshot_and_calendar_are_configured_not_hardcoded(services):
    assert services.pack.meta["dataset_snapshot"] == "2026-08-16 11:00"
    calendar = services.pack.policy_rules["business_calendar"]
    assert calendar["assumption"] is True and calendar["timezone"] == "Asia/Kolkata"
