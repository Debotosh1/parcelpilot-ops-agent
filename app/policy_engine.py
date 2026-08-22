"""Deterministic policy engine.

The LLM is not trusted with arithmetic, thresholds, or precedence. Every
answer that has a right answer is computed here, in Python, from the rules
registry (a projection of the CURRENT documents) plus the signed-contract
overrides. The engine returns:

* a decision,
* the numbers behind it,
* an ordered **source chain** (contract > policy/SOP > product doc),
* explicit **assumptions**, **conflicts** and **verification needs**,
* a confidence band and whether a human must be involved.

The agent's job is to route to these functions and narrate their output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .business_time import BusinessCalendar, humanize_minutes
from .loaders import DataPack, parse_ts

SEVERITIES = ("P1", "P2", "P3")


def _citation(doc_id: str, clause: str, quote: str = "", tier: int = 2) -> dict[str, Any]:
    return {"doc_id": doc_id, "clause": clause, "quote": quote, "authority_tier": tier}


@dataclass
class Finding:
    """Uniform envelope returned by every engine call."""

    decision: str
    summary: str
    facts: dict[str, Any] = field(default_factory=dict)
    citations: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    verification_needed: list[str] = field(default_factory=list)
    requires_human: bool = False
    confidence: str = "high"

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "summary": self.summary,
            "facts": self.facts,
            "citations": self.citations,
            "assumptions": self.assumptions,
            "conflicts": self.conflicts,
            "verification_needed": self.verification_needed,
            "requires_human": self.requires_human,
            "confidence": self.confidence,
        }


class PolicyEngine:
    def __init__(self, pack: DataPack):
        self.pack = pack
        self.rules = pack.policy_rules
        self.overrides = pack.contract_overrides.get("accounts", {})
        self.calendar = BusinessCalendar.from_rules(self.rules)
        self.snapshot = pack.snapshot_at

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _account(self, account_id: str) -> dict[str, Any]:
        acct = self.pack.accounts.get(account_id)
        if not acct:
            raise KeyError(f"Unknown account {account_id}")
        return acct

    def _override(self, account_id: str | None, section: str) -> dict[str, Any] | None:
        if not account_id:
            return None
        return (self.overrides.get(account_id) or {}).get(section)

    def _calendar_assumption(self) -> str:
        cal = self.rules.get("business_calendar", {})
        return (
            f"Business hours assumed {cal.get('start', '09:00')}-{cal.get('end', '18:00')} "
            f"Mon-Fri {cal.get('timezone', 'Asia/Kolkata')} "
            f"({cal.get('hours_per_business_day', 9)}h = 1 business day). "
            "The data pack does not define a working calendar; this is a configured assumption."
        )

    # ------------------------------------------------------------------
    # severity
    # ------------------------------------------------------------------
    def classify_severity(self, text: str, subject: str = "") -> Finding:
        blob = f"{subject} {text}".lower()
        defs = self.rules["severity_definitions"]
        for sev in SEVERITIES:
            signals = [s for s in defs[sev]["signals"] if s in blob]
            if signals:
                return Finding(
                    decision=sev,
                    summary=f"{sev} - {defs[sev]['label']}: {defs[sev]['definition']}",
                    facts={"matched_signals": signals},
                    citations=[
                        _citation(
                            defs[sev]["source"]["doc_id"],
                            defs[sev]["source"]["clause"],
                            defs[sev]["definition"],
                        )
                    ],
                    confidence="high" if len(signals) > 1 else "medium",
                )
        return Finding(
            decision="P3",
            summary="No severity signal matched; defaulted to P3 and flagged for human confirmation.",
            facts={"matched_signals": []},
            citations=[_citation("POL-SUPPORT-V3", "2. Severity definitions")],
            confidence="low",
            requires_human=True,
            verification_needed=["Severity could not be determined from the text; confirm manually."],
        )

    # ------------------------------------------------------------------
    # SLA
    # ------------------------------------------------------------------
    def resolve_sla_target(self, account_id: str, severity: str) -> Finding:
        severity = severity.upper()
        if severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}")
        acct = self._account(account_id)
        plan = acct["plan"]
        default = self.rules["sla_defaults"]["plans"][plan][severity]
        default_src = self.rules["sla_defaults"]["source"]
        citations = [
            _citation(
                default_src["doc_id"],
                default_src["clause"],
                f"{plan} {severity}: {default['value']} {default['unit'].replace('_', ' ')}",
                tier=2,
            )
        ]
        applied = dict(default)
        source_kind = "policy_default"
        conflicts: list[str] = []
        override = self._override(account_id, "sla")

        if override and override.get("replaces_defaults") and severity in override.get("targets", {}):
            applied = dict(override["targets"][severity])
            source_kind = "contract_override"
            citations.insert(
                0,
                _citation(
                    self.overrides[account_id]["doc_id"],
                    override["clause"],
                    override.get("quote", ""),
                    tier=1,
                ),
            )
            if (applied["value"], applied["unit"]) != (default["value"], default["unit"]):
                conflicts.append(
                    f"Signed agreement overrides the {plan} {severity} policy default "
                    f"({default['value']} {default['unit'].replace('_', ' ')} -> "
                    f"{applied['value']} {applied['unit'].replace('_', ' ')}). Contract wins."
                )

        assumptions = []
        if applied.get("clock") == "business":
            assumptions.append(self._calendar_assumption())
        if override and override.get("coverage") == "business_hours_only":
            assumptions.append(
                "Agreement states no weekend or after-hours coverage, so the response clock only "
                "runs during business hours."
            )

        return Finding(
            decision=f"{applied['value']} {applied['unit'].replace('_', ' ')}",
            summary=(
                f"{acct['account_name']} ({plan}) {severity} first-response target: "
                f"{applied['value']} {applied['unit'].replace('_', ' ')} "
                f"({'per signed agreement' if source_kind == 'contract_override' else 'per current support policy'})."
            ),
            facts={
                "account_id": account_id,
                "plan": plan,
                "severity": severity,
                "target": applied,
                "policy_default": default,
                "source_kind": source_kind,
            },
            citations=citations,
            assumptions=assumptions,
            conflicts=conflicts,
        )

    def sla_status(
        self,
        account_id: str,
        severity: str,
        created_at: str | datetime,
        first_response_at: str | datetime | None = None,
        at: datetime | None = None,
    ) -> Finding:
        now = at or self.snapshot
        created = created_at if isinstance(created_at, datetime) else parse_ts(created_at)
        if created is None:
            raise ValueError(f"Unparseable created_at: {created_at!r}")
        responded = (
            first_response_at
            if isinstance(first_response_at, datetime)
            else parse_ts(first_response_at)
        )

        target_finding = self.resolve_sla_target(account_id, severity)
        target = target_finding.facts["target"]
        clock = target.get("clock", "24x7")
        due_at = self.calendar.add(
            self.calendar.next_open(created) if clock == "business" else created,
            target["value"],
            target["unit"],
        )
        reference = responded or now
        elapsed = self.calendar.elapsed(created, reference, clock)
        target_minutes = self.calendar.elapsed(created, due_at, clock)
        remaining = target_minutes - elapsed
        breached = reference > due_at

        if responded:
            state = "met" if not breached else "breached"
        elif breached:
            state = "breached"
        elif remaining <= max(0.25 * target_minutes, 15):
            state = "at_risk"
        else:
            state = "on_track"

        summary = (
            f"{severity} target {target['value']} {target['unit'].replace('_', ' ')} "
            f"(due {due_at:%Y-%m-%d %H:%M}); "
            + (
                f"BREACHED by {humanize_minutes(-remaining)} of {clock.replace('24x7', '24x7 ')} clock."
                if state == "breached"
                else f"{humanize_minutes(remaining)} remaining."
            )
        )

        return Finding(
            decision=state,
            summary=summary,
            facts={
                "account_id": account_id,
                "severity": severity,
                "created_at": created.strftime("%Y-%m-%d %H:%M"),
                "evaluated_at": reference.strftime("%Y-%m-%d %H:%M"),
                "due_at": due_at.strftime("%Y-%m-%d %H:%M"),
                "clock": clock,
                "target": target,
                "elapsed_minutes": round(elapsed, 1),
                "minutes_remaining": round(remaining, 1),
                "breached": breached,
                "first_response_recorded": bool(responded),
            },
            citations=target_finding.citations,
            assumptions=target_finding.assumptions
            + (
                []
                if responded
                else [
                    "The data pack has no first-response timestamp, so an open ticket is treated as "
                    "not yet answered and the clock is measured to the dataset snapshot."
                ]
            ),
            conflicts=target_finding.conflicts,
            requires_human=state == "breached",
        )

    def ticket_sla_status(self, ticket: dict[str, Any], at: datetime | None = None) -> Finding:
        sev = self.classify_severity(ticket.get("description", ""), ticket.get("subject", ""))
        if str(ticket.get("status", "")).lower() == "closed":
            target = self.resolve_sla_target(ticket["account_id"], sev.decision)
            return Finding(
                decision="not_applicable_closed",
                summary=(
                    f"{ticket['ticket_id']} is closed. The pack records no first-response timestamp, so "
                    f"historical SLA attainment cannot be computed. Applicable target was {target.decision}."
                ),
                facts={
                    "ticket_id": ticket["ticket_id"],
                    "account_id": ticket["account_id"],
                    "severity": sev.decision,
                    "severity_rationale": sev.summary,
                    "severity_confidence": sev.confidence,
                    "target": target.facts["target"],
                    "breached": None,
                    "minutes_remaining": None,
                },
                citations=sev.citations + target.citations,
                assumptions=target.assumptions,
                conflicts=target.conflicts,
                confidence="medium",
            )
        finding = self.sla_status(
            ticket["account_id"], sev.decision, ticket["created_at"], at=at
        )
        finding.facts["ticket_id"] = ticket["ticket_id"]
        finding.facts["severity_rationale"] = sev.summary
        finding.facts["severity_confidence"] = sev.confidence
        finding.citations = sev.citations + finding.citations
        if sev.requires_human:
            finding.requires_human = True
            finding.verification_needed += sev.verification_needed
            finding.confidence = "low"
        return finding

    # ------------------------------------------------------------------
    # cancellation
    # ------------------------------------------------------------------
    def evaluate_cancellation(self, order_id: str, at: datetime | None = None) -> Finding:
        order = self.pack.orders.get(order_id)
        if not order:
            raise KeyError(f"Unknown order {order_id}")
        account_id = order["account_id"]
        acct = self._account(account_id)
        status = order["status"].upper()
        rules = self.rules["cancellation"]
        sop_src = rules["source"]
        state_rule = rules["states"].get(status)
        citations = [_citation(sop_src["doc_id"], sop_src["clause"], state_rule and state_rule["note"] or "", tier=2)]
        override = self._override(account_id, "cancellation")
        conflicts: list[str] = []
        verification: list[str] = []
        assumptions: list[str] = []

        if state_rule is None:
            return Finding(
                decision="unknown",
                summary=f"Order status {status} is not covered by the current SOP.",
                facts={"order_id": order_id, "status": status},
                citations=citations,
                requires_human=True,
                confidence="low",
                verification_needed=[f"SOP has no rule for status {status}; ask a manager."],
            )

        if status in {"PICKED_UP", "DELIVERED"}:
            decision = "not_cancellable"
            fee = None
            summary = (
                f"{order_id} is {status}: {state_rule['note']}"
            )
            if status == "PICKED_UP" and override and override.get("picked_up_defers_to_sop"):
                citations.insert(
                    0,
                    _citation(
                        self.overrides[account_id]["doc_id"],
                        override["clause"],
                        override.get("quote", ""),
                        tier=1,
                    ),
                )
            return Finding(
                decision=decision,
                summary=summary,
                facts={
                    "order_id": order_id,
                    "account_id": account_id,
                    "account_name": acct["account_name"],
                    "status": status,
                    "alternative_workflow": state_rule.get("alternative_workflow"),
                    "fee_inr": fee,
                },
                citations=citations,
                requires_human=status == "PICKED_UP",
            )

        if status == "DRAFT":
            return Finding(
                decision="cancellable_no_fee",
                summary=f"{order_id} is a DRAFT: may be cancelled with no fee.",
                facts={"order_id": order_id, "account_id": account_id, "status": status, "fee_inr": 0},
                citations=citations,
            )

        # --- BOOKED -------------------------------------------------------
        booked_at = parse_ts(order["booked_at"])
        requested_at = parse_ts(order.get("cancellation_requested_at")) or (at or self.snapshot)
        used_snapshot = not parse_ts(order.get("cancellation_requested_at"))
        elapsed_min = (requested_at - booked_at).total_seconds() / 60.0
        free_window = state_rule["free_window_minutes"]
        default_fee = 0 if elapsed_min <= free_window else state_rule["fee_after_window_inr"]
        fee = default_fee
        waiver_applied = False

        if override and override.get("booked_fee_waived"):
            waiver_applied = True
            fee = override.get("booked_fee_inr", 0)
            citations.insert(
                0,
                _citation(
                    self.overrides[account_id]["doc_id"],
                    override["clause"],
                    override.get("quote", ""),
                    tier=1,
                ),
            )
            if default_fee != fee:
                conflicts.append(
                    f"SOP default would charge INR {default_fee} ({humanize_minutes(elapsed_min)} after booking), "
                    f"but the signed agreement waives the BOOKED cancellation fee. Contract wins."
                )
        elif override and override.get("defer_to_sop"):
            citations.insert(
                0,
                _citation(
                    self.overrides[account_id]["doc_id"],
                    override["clause"],
                    override.get("quote", ""),
                    tier=1,
                ),
            )
        elif not acct.get("contract_file"):
            assumptions.append(
                f"No signed agreement for {acct['account_name']} in the supplied pack, so default SOP terms apply."
            )

        # KI-211: a BOOKED SwiftShip order may already have been collected.
        ki = self._known_issue("KI-211")
        if ki and order["carrier"] == ki.get("carrier"):
            verification.append(
                f"{ki['id']}: {ki['title']} - SwiftShip pickup webhooks can lag up to "
                f"{ki['delay_window_minutes']} minutes, so a BOOKED status may be stale. Verify with the "
                "carrier that the parcel has not already been collected before cancelling."
            )
            citations.append(
                _citation("DOC-PRODUCT-OPS", f"2. Current known issues / {ki['id']}", ki["workaround"], tier=3)
            )

        if used_snapshot:
            assumptions.append(
                "No cancellation_requested_at on this order; the dataset snapshot time was used as the request time."
            )

        decision = "cancellable_no_fee" if fee == 0 else "cancellable_with_fee"
        summary = (
            f"{order_id} ({acct['account_name']}) is BOOKED and not yet picked up, "
            f"{humanize_minutes(elapsed_min)} after booking. "
            + (
                "No cancellation fee: the signed agreement waives it regardless of elapsed time."
                if waiver_applied
                else (
                    f"No fee - inside the {free_window}-minute free window."
                    if fee == 0
                    else f"Cancellation fee INR {fee} applies (past the {free_window}-minute free window)."
                )
            )
        )

        return Finding(
            decision=decision,
            summary=summary,
            facts={
                "order_id": order_id,
                "account_id": account_id,
                "account_name": acct["account_name"],
                "status": status,
                "booked_at": booked_at.strftime("%Y-%m-%d %H:%M"),
                "cancellation_requested_at": requested_at.strftime("%Y-%m-%d %H:%M"),
                "minutes_since_booking": round(elapsed_min, 1),
                "free_window_minutes": free_window,
                "policy_default_fee_inr": default_fee,
                "fee_inr": fee,
                "contract_waiver_applied": waiver_applied,
            },
            citations=citations,
            assumptions=assumptions,
            conflicts=conflicts,
            verification_needed=verification,
            requires_human=False,
            confidence="high" if not verification else "medium",
        )

    # ------------------------------------------------------------------
    # service credits
    # ------------------------------------------------------------------
    def evaluate_service_credit(
        self,
        order_id: str | None = None,
        account_id: str | None = None,
        hours_late: float | None = None,
        carrier_fault: bool | None = None,
        customer_fault: bool | None = None,
        shipment_fee_inr: float | None = None,
        at: datetime | None = None,
    ) -> Finding:
        """Evaluate a failed-pickup credit either for a real order or for a
        hypothetical scenario ("a pickup is 3 hours late, carrier at fault")."""
        now = at or self.snapshot
        assumptions: list[str] = []
        verification: list[str] = []
        conflicts: list[str] = []
        facts: dict[str, Any] = {}
        pickup_state = None

        if order_id:
            order = self.pack.orders.get(order_id)
            if not order:
                raise KeyError(f"Unknown order {order_id}")
            account_id = order["account_id"]
            window_end = parse_ts(order["pickup_window_end"])
            actual = parse_ts(order.get("pickup_actual_at"))
            reference = actual or now
            hours_late = max(0.0, (reference - window_end).total_seconds() / 3600.0)
            carrier_fault = order["carrier_fault"]
            customer_fault = order["customer_fault"]
            shipment_fee_inr = order["shipment_fee_inr"]
            pickup_state = "picked_up" if actual else "not_picked_up"
            facts.update(
                {
                    "order_id": order_id,
                    "carrier": order["carrier"],
                    "order_status": order["status"],
                    "pickup_window_end": window_end.strftime("%Y-%m-%d %H:%M"),
                    "pickup_actual_at": actual.strftime("%Y-%m-%d %H:%M") if actual else None,
                    "measured_at": reference.strftime("%Y-%m-%d %H:%M"),
                }
            )
            if not actual:
                assumptions.append(
                    "Pickup has not happened yet; delay is measured from the end of the pickup window "
                    "to the dataset snapshot and keeps growing."
                )

        default = self.rules["service_credit"]
        sop_src = default["source"]
        citations = [
            _citation(
                sop_src["doc_id"],
                sop_src["clause"],
                f"Credit when pickup is more than {default['delay_threshold_hours']} hours past the pickup "
                f"window end, carrier at fault, no customer fault. Default credit = lower of INR "
                f"{default['flat_cap_inr']} or {default['percent_of_fee']}% of the shipment fee.",
                tier=2,
            )
        ]

        threshold = float(default["delay_threshold_hours"])
        amount_basis = "default"
        override = self._override(account_id, "service_credit")
        account_name = self._account(account_id)["account_name"] if account_id else None
        monthly_cap = None

        if override and override.get("replaces_default"):
            citations.insert(
                0,
                _citation(
                    self.overrides[account_id]["doc_id"],
                    override["clause"],
                    override.get("quote", ""),
                    tier=1,
                ),
            )
            if override.get("delay_threshold_hours") is not None:
                if float(override["delay_threshold_hours"]) != threshold:
                    conflicts.append(
                        f"Default SOP threshold is {threshold}h, but {account_name}'s signed agreement "
                        f"sets {override['delay_threshold_hours']}h. Contract wins."
                    )
                threshold = float(override["delay_threshold_hours"])
            amount_basis = "contract_flat"
        elif override:
            citations.insert(
                0,
                _citation(
                    self.overrides[account_id]["doc_id"],
                    override["clause"],
                    override.get("quote", ""),
                    tier=1,
                ),
            )
            monthly_cap = override.get("monthly_aggregate_cap_inr")
        if override and override.get("monthly_aggregate_cap_inr"):
            monthly_cap = override["monthly_aggregate_cap_inr"]

        if account_id is None:
            assumptions.append(
                "No account was supplied, so the default SOP terms are used. A signed agreement can "
                "change the delay threshold, the amount, or the cap - confirm the account before promising anything."
            )
            verification.append("Which account is this? Contract terms may override the default answer.")

        # --- eligibility --------------------------------------------------
        unknowns = []
        if carrier_fault is None:
            unknowns.append("carrier_fault")
        if customer_fault is None:
            unknowns.append("customer_fault")
        if hours_late is None:
            unknowns.append("pickup_timing")

        approval = self.rules["approval"]
        if unknowns:
            return Finding(
                decision="unknown",
                summary=(
                    "Cannot decide the credit: "
                    + ", ".join(unknowns)
                    + " is unknown. The SOP forbids promising a credit while these are unresolved."
                ),
                facts={**facts, "unknown_inputs": unknowns, "threshold_hours": threshold},
                citations=citations
                + [_citation(approval["source"]["doc_id"], approval["source"]["clause"], approval["conflict_rule"], 2)],
                assumptions=assumptions,
                conflicts=conflicts,
                verification_needed=verification + [f"Confirm {u}" for u in unknowns],
                requires_human=True,
                confidence="low",
            )

        eligible = bool(hours_late > threshold and carrier_fault and not customer_fault)
        amount = 0.0
        if eligible:
            if amount_basis == "contract_flat" and override.get("flat_amount_inr") is not None:
                amount = float(override["flat_amount_inr"])
            elif shipment_fee_inr is None:
                return Finding(
                    decision="unknown",
                    summary="Eligible on timing and fault, but the shipment fee is unknown so the "
                    "default 10%-of-fee calculation cannot be completed.",
                    facts={**facts, "threshold_hours": threshold, "hours_late": round(hours_late, 2)},
                    citations=citations,
                    assumptions=assumptions,
                    verification_needed=verification + ["Confirm shipment_fee_inr"],
                    requires_human=True,
                    confidence="low",
                )
            else:
                amount = min(
                    float(default["flat_cap_inr"]),
                    float(default["percent_of_fee"]) / 100.0 * float(shipment_fee_inr),
                )

        approval_threshold = float(approval["manager_approval_above_inr"])
        approval_required = amount > approval_threshold
        if approval_required:
            citations.append(
                _citation(
                    approval["source"]["doc_id"],
                    approval["source"]["clause"],
                    f"Any individual credit above INR {approval_threshold:.0f} requires manager approval.",
                    2,
                )
            )

        if eligible and monthly_cap:
            assumptions.append(
                f"{account_name}'s agreement caps monthly aggregate credits at INR {monthly_cap}. "
                "The pack has no ledger of credits already issued this month, so the remaining headroom "
                "must be checked before issuing."
            )
            verification.append(f"Check month-to-date credits against the INR {monthly_cap} aggregate cap.")

        if eligible:
            summary = (
                f"Eligible: pickup is {hours_late:.1f}h past the window end (threshold {threshold}h), "
                f"carrier at fault, no customer fault. Credit INR {amount:.0f}"
                + (" (contract flat amount)." if amount_basis == "contract_flat" else
                   f" = lower of INR {default['flat_cap_inr']} and {default['percent_of_fee']}% of INR {shipment_fee_inr:.0f}.")
            )
            decision = "eligible"
        else:
            reasons = []
            if hours_late <= threshold:
                reasons.append(f"delay {hours_late:.1f}h does not exceed the {threshold}h threshold")
            if not carrier_fault:
                reasons.append("carrier fault is not recorded")
            if customer_fault:
                reasons.append("a customer-caused issue is recorded")
            summary = "Not eligible: " + "; ".join(reasons) + "."
            decision = "not_eligible"

        return Finding(
            decision=decision,
            summary=summary,
            facts={
                **facts,
                "account_id": account_id,
                "account_name": account_name,
                "hours_late": round(float(hours_late), 2),
                "threshold_hours": threshold,
                "carrier_fault": carrier_fault,
                "customer_fault": customer_fault,
                "shipment_fee_inr": shipment_fee_inr,
                "credit_amount_inr": round(amount, 2),
                "amount_basis": amount_basis,
                "monthly_aggregate_cap_inr": monthly_cap,
                "manager_approval_required": approval_required,
                "pickup_state": pickup_state,
            },
            citations=citations,
            assumptions=assumptions,
            conflicts=conflicts,
            verification_needed=verification,
            requires_human=approval_required or bool(verification),
            confidence="high" if not verification else "medium",
        )

    # ------------------------------------------------------------------
    # product / known issues
    # ------------------------------------------------------------------
    def _known_issue(self, issue_id: str) -> dict[str, Any] | None:
        for ki in self.rules["product"]["known_issues"]:
            if ki["id"] == issue_id:
                return ki
        return None

    def match_known_issues(self, text: str, carrier: str | None = None) -> list[dict[str, Any]]:
        blob = (text or "").lower()
        hits = []
        for ki in self.rules["product"]["known_issues"]:
            matched = [s for s in ki.get("match_signals", []) if s in blob]
            if carrier and ki.get("carrier") and carrier.lower() == ki["carrier"].lower():
                matched.append(f"carrier={carrier}")
            if matched:
                hits.append({**ki, "matched_signals": matched})
        return hits

    # ------------------------------------------------------------------
    # trust: historical guidance auditing
    # ------------------------------------------------------------------
    def check_historical_guidance(self, ticket: dict[str, Any]) -> Finding:
        """Compare a past resolution against today's authoritative rules.

        Historical tickets are tier-5 context. This routine looks for the
        specific ways past guidance can be wrong today: a fee that a contract
        waives, a plan limit that contradicts the product doc, or an SLA number
        that no longer matches.
        """
        resolution = (ticket.get("historical_resolution") or "").strip()
        account_id = ticket.get("account_id")
        findings: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []

        if not resolution:
            return Finding(
                decision="no_historical_guidance",
                summary=f"{ticket.get('ticket_id')} has no recorded historical resolution.",
                facts={"ticket_id": ticket.get("ticket_id")},
            )

        low = resolution.lower()

        # 1) Cancellation-fee claims vs contract waiver / current SOP amount.
        fee_match = re.search(r"inr\s*([\d,]+)\s*cancellation fee|cancellation fee[^.]*?inr\s*([\d,]+)", low)
        if "cancellation fee" in low:
            sop_fee = self.rules["cancellation"]["states"]["BOOKED"]["fee_after_window_inr"]
            claimed = None
            if fee_match:
                raw = fee_match.group(1) or fee_match.group(2)
                claimed = float(raw.replace(",", ""))
            override = self._override(account_id, "cancellation")
            if override and override.get("booked_fee_waived"):
                findings.append(
                    {
                        "type": "contradicts_contract",
                        "claim": resolution,
                        "current_position": (
                            f"{self._account(account_id)['account_name']}'s signed agreement waives the BOOKED "
                            "cancellation fee entirely, regardless of elapsed time. The past answer was wrong."
                        ),
                        "severity": "high",
                    }
                )
                citations.append(
                    _citation(
                        self.overrides[account_id]["doc_id"], override["clause"], override.get("quote", ""), 1
                    )
                )
            elif claimed is not None and claimed != float(sop_fee):
                findings.append(
                    {
                        "type": "contradicts_current_sop",
                        "claim": resolution,
                        "current_position": f"The current SOP fee is INR {sop_fee}, not INR {claimed:.0f}.",
                        "severity": "medium",
                    }
                )
                citations.append(
                    _citation("SOP-CANCEL-CREDIT-V4", "1. Order cancellation", "", 2)
                )

        # 2) Row-limit claims vs the product doc + KI-208.
        rows_match = re.search(r"([\d,]{3,})\s*rows", low)
        if rows_match and ("upload" in low or "csv" in low or "plan" in low or "supports" in low):
            claimed_rows = int(rows_match.group(1).replace(",", ""))
            supported = int(self.rules["product"]["bulk_upload"]["supported_rows_per_csv"])
            ki = self._known_issue("KI-208")
            if claimed_rows != supported:
                findings.append(
                    {
                        "type": "contradicts_product_doc",
                        "claim": resolution,
                        "current_position": (
                            f"The supported limit is {supported:,} rows per CSV on Growth and Enterprise. "
                            f"The {claimed_rows:,}-row figure is not a plan limit - it matches "
                            f"{ki['id']} ({ki['title']}, {ki['status']}), an open defect above ~{ki['threshold_rows']:,} rows. "
                            "Describing it as a plan limit sets the wrong expectation."
                        ),
                        "severity": "high",
                    }
                )
                citations.append(_citation("DOC-PRODUCT-OPS", "1. Plan capabilities", "", 3))
                citations.append(
                    _citation("DOC-PRODUCT-OPS", f"2. Current known issues / {ki['id']}", ki["workaround"], 3)
                )

        # 3) SLA numbers quoted in past guidance vs today's targets.
        sla_match = re.search(r"\b(p[123])\b[^.]*?(\d+)\s*(minutes?|hours?|business hours?|business days?)", low)
        if sla_match and account_id in self.pack.accounts:
            sev = sla_match.group(1).upper()
            claimed_value = float(sla_match.group(2))
            claimed_unit = sla_match.group(3).replace(" ", "_").rstrip("s")
            target = self.resolve_sla_target(account_id, sev).facts["target"]
            if (claimed_value, claimed_unit.rstrip("s")) != (
                float(target["value"]),
                target["unit"].rstrip("s"),
            ):
                findings.append(
                    {
                        "type": "contradicts_current_sla",
                        "claim": resolution,
                        "current_position": (
                            f"Current {sev} target for this account is {target['value']} "
                            f"{target['unit'].replace('_', ' ')}."
                        ),
                        "severity": "medium",
                    }
                )

        if not findings:
            return Finding(
                decision="consistent",
                summary="Past resolution does not contradict any current authoritative rule "
                "(still tier-5 context, never a basis for a promise).",
                facts={"ticket_id": ticket.get("ticket_id"), "historical_resolution": resolution},
            )

        return Finding(
            decision="contradicts_current_policy",
            summary=(
                f"{ticket.get('ticket_id')}: past guidance conflicts with current authoritative sources "
                f"({len(findings)} issue(s)). Do not reuse it."
            ),
            facts={
                "ticket_id": ticket.get("ticket_id"),
                "historical_resolution": resolution,
                "findings": findings,
            },
            citations=citations,
            conflicts=[f["current_position"] for f in findings],
            requires_human=any(f["severity"] == "high" for f in findings),
            confidence="high",
        )

    # ------------------------------------------------------------------
    def escalation_recommendation(self, ticket: dict[str, Any]) -> dict[str, Any]:
        sev = self.classify_severity(ticket.get("description", ""), ticket.get("subject", ""))
        sla = self.ticket_sla_status(ticket)
        reasons: list[str] = []
        if sev.decision == "P1":
            reasons.append("P1 incidents must be escalated immediately (Support Policy v3 §4).")
        if sla.facts.get("breached"):
            reasons.append(
                f"First-response target already breached by {humanize_minutes(-sla.facts['minutes_remaining'])}."
            )
        elif sla.decision == "at_risk":
            reasons.append("First-response target is close to breach.")
        return {
            "ticket_id": ticket.get("ticket_id"),
            "severity": sev.decision,
            "sla_state": sla.decision,
            "should_escalate": bool(reasons),
            "reasons": reasons,
        }
