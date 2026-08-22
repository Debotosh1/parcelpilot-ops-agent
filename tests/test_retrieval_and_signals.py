"""Retrieval precedence + proactive detection."""

from __future__ import annotations

from app.retrieval import DocumentIndex


def test_deprecated_policy_is_excluded_by_default(services):
    hits = services.index.search("Enterprise P1 first response target")
    assert all(h["doc_id"] != "POL-SUPPORT-V2" for h in hits)


def test_deprecated_policy_is_labelled_when_explicitly_requested(services):
    hits = services.index.search("Enterprise P1 first response target", include_deprecated=True, limit=20)
    assert [h["doc_id"] for h in hits].index("POL-SUPPORT-V2") > 3  # demoted well below current sources
    v2 = [h for h in hits if h["doc_id"] == "POL-SUPPORT-V2"]
    assert v2 and "DEPRECATED" in v2[0]["warning"]


def test_contract_outranks_the_general_sop_for_a_contract_question(services):
    hits = services.index.search("Northstar cancellation fee BOOKED shipment")
    assert hits[0]["doc_id"] == "CONTRACT-ACCT-001"
    assert hits[0]["authority_tier"] == 1


def test_sop_is_returned_for_a_generic_cancellation_question(services):
    hits = services.index.search("cancellation fee 30 minutes after booking")
    assert any(h["doc_id"] == "SOP-CANCEL-CREDIT-V4" for h in hits)


def test_chunks_are_clause_level_so_citations_are_precise(services):
    hits = services.index.search("failed pickup service credit 10% of the shipment fee")
    assert hits[0]["clause"].startswith("2. Failed-pickup")


def test_index_survives_a_new_document_without_code_changes(services):
    index = DocumentIndex(services.pack.documents)
    assert len(index.chunks) > len(services.pack.documents)


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------
def signal_types(sigs):
    return {s["type"] for s in sigs}


def test_open_p1s_and_breaches_are_detected(services):
    sigs = services.signals.detect(services.principal("priya"))
    assert {"open_p1", "sla_breach"} <= signal_types(sigs)
    breached = {s["title"].split()[0] for s in sigs if s["type"] == "sla_breach"}
    assert breached == {"TKT-501", "TKT-505"}


def test_overdue_pickup_is_linked_to_its_credit_evaluation(services):
    sigs = [s for s in services.signals.detect(services.principal("priya")) if s["type"] == "overdue_pickup"]
    assert len(sigs) == 1
    assert sigs[0]["evidence"]["credit"]["decision"] == "eligible"
    assert sigs[0]["evidence"]["credit"]["facts"]["credit_amount_inr"] == 300


def test_known_issue_cluster_groups_the_bulk_upload_tickets(services):
    sigs = {s["signal_id"]: s for s in services.signals.detect(services.principal("priya"))}
    ki208 = sigs["SIG-KI-KI-208"]
    ids = {t["ticket_id"] for t in ki208["evidence"]["tickets"]}
    assert ids == {"TKT-502", "TKT-451"}


def test_incorrect_past_guidance_is_surfaced_proactively(services):
    sigs = [s for s in services.signals.detect(services.principal("priya")) if s["type"] == "incorrect_past_guidance"]
    assert {s["evidence"]["ticket"]["ticket_id"] for s in sigs} == {"TKT-450", "TKT-451"}


def test_signals_are_ranked_worst_first(services):
    sigs = services.signals.detect(services.principal("priya"))
    order = [s["severity"] for s in sigs]
    rank = {"critical": 0, "high": 1, "medium": 2, "info": 3}
    assert order == sorted(order, key=lambda s: rank[s])


def test_analyst_signals_carry_no_customer_free_text(services):
    sigs = services.signals.detect(services.principal("sam"))
    blob = str(sigs)
    assert "HTTP 500" not in blob
    assert "redacted" in blob
