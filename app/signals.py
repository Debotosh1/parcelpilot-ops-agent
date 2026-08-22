"""Proactive issue detection (Client Problem 1).

A reactive chatbot only helps once someone asks. This module sweeps the whole
support surface at the dataset snapshot and emits ranked, evidence-backed
signals: SLA breaches, open P1s, known-issue clusters, cross-customer patterns,
unactioned order states, volume spikes, and past answers that today's rules
contradict.

Every signal is deterministic and carries its evidence (ticket/order ids) plus
the recommended next action, so the LLM narrates and the humans verify - the
detection itself never depends on a model.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .business_time import humanize_minutes
from .loaders import DataPack, parse_ts
from .policy_engine import PolicyEngine
from .rbac import Principal, redact_record

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "info": 3}


@dataclass
class Signal:
    signal_id: str
    type: str
    severity: str
    title: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    recommended_action: str = ""
    accounts: list[str] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    confidence: str = "high"

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "type": self.type,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "evidence": self.evidence,
            "recommended_action": self.recommended_action,
            "accounts": self.accounts,
            "citations": self.citations,
            "confidence": self.confidence,
        }


def _redact_deep(value: Any, principal: Principal) -> Any:
    """Signal payloads nest tickets inside lists and dicts; redaction has to
    follow the whole tree or customer text leaks through the evidence blob."""
    if isinstance(value, dict):
        return {k: _redact_deep(v, principal) for k, v in redact_record(value, principal).items()}
    if isinstance(value, list):
        return [_redact_deep(v, principal) for v in value]
    return value


def _tokens(text: str) -> set[str]:
    stop = {"the", "a", "for", "and", "is", "of", "to", "in", "on", "after", "still", "how", "do", "we"}
    return {w.strip(",.?").lower() for w in text.split() if len(w) > 3 and w.lower() not in stop}


class SignalEngine:
    def __init__(self, pack: DataPack, engine: PolicyEngine, store):
        self.pack = pack
        self.engine = engine
        self.store = store

    # ------------------------------------------------------------------
    def detect(self, principal: Principal, at: datetime | None = None) -> list[dict[str, Any]]:
        now = at or self.engine.snapshot
        allowed = principal.allowed_accounts(self.pack)
        tickets = [t for t in self.store.tickets.values() if t["account_id"] in allowed]
        orders = [o for o in self.pack.orders.values() if o["account_id"] in allowed]

        signals: list[Signal] = []
        signals += self._sla_signals(tickets, now)
        signals += self._known_issue_clusters(tickets)
        signals += self._recurring_themes(tickets, now)
        signals += self._order_anomalies(orders, now)
        signals += self._volume_spike(tickets, now)
        signals += self._stale_guidance(tickets)
        signals += self._carrier_concentration(tickets, orders, now)

        signals.sort(key=lambda s: (SEVERITY_RANK.get(s.severity, 9), s.signal_id))
        out = []
        for sig in signals:
            data = sig.to_dict()
            if principal.redact_customer_content:
                data = _redact_deep(data, principal)
            out.append(data)
        return out

    # ------------------------------------------------------------------
    def _sla_signals(self, tickets: list[dict[str, Any]], now: datetime) -> list[Signal]:
        out: list[Signal] = []
        for ticket in tickets:
            if ticket.get("status") != "open":
                continue
            sla = self.engine.ticket_sla_status(ticket, at=now)
            severity = sla.facts["severity"]
            account = self.pack.accounts[ticket["account_id"]]
            if sla.decision == "breached":
                out.append(
                    Signal(
                        signal_id=f"SIG-SLA-{ticket['ticket_id']}",
                        type="sla_breach",
                        severity="critical" if severity == "P1" else "high",
                        title=f"{ticket['ticket_id']} ({account['account_name']}) {severity} first response BREACHED",
                        detail=(
                            f"{sla.summary} Ticket opened {ticket['created_at']}, still open at snapshot "
                            f"{now:%Y-%m-%d %H:%M}."
                        ),
                        evidence={"ticket": ticket, "sla": sla.facts},
                        recommended_action="Escalate now and state the breach openly to the customer "
                        "(Support Policy v3 §4).",
                        accounts=[ticket["account_id"]],
                        citations=sla.citations,
                        confidence=sla.facts.get("severity_confidence", "high"),
                    )
                )
            elif sla.decision == "at_risk":
                out.append(
                    Signal(
                        signal_id=f"SIG-SLA-{ticket['ticket_id']}",
                        type="sla_at_risk",
                        severity="medium",
                        title=f"{ticket['ticket_id']} ({account['account_name']}) {severity} response due soon",
                        detail=sla.summary,
                        evidence={"ticket": ticket, "sla": sla.facts},
                        recommended_action="Respond before the target elapses or hand over with context.",
                        accounts=[ticket["account_id"]],
                        citations=sla.citations,
                    )
                )
            if severity == "P1":
                out.append(
                    Signal(
                        signal_id=f"SIG-P1-{ticket['ticket_id']}",
                        type="open_p1",
                        severity="critical",
                        title=f"Open P1: {ticket['subject']} ({account['account_name']})",
                        detail=f"{sla.facts['severity_rationale']} P1 incidents must be escalated immediately.",
                        evidence={"ticket": ticket, "sla": sla.facts},
                        recommended_action="Escalate immediately; for suspected credential exposure also start "
                        "key rotation with the customer.",
                        accounts=[ticket["account_id"]],
                        citations=sla.citations,
                    )
                )
        return out

    # ------------------------------------------------------------------
    def _known_issue_clusters(self, tickets: list[dict[str, Any]]) -> list[Signal]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ticket in tickets:
            carrier = None
            for order in self.pack.orders.values():
                if order["account_id"] == ticket["account_id"]:
                    carrier = order["carrier"]
                    break
            hits = self.engine.match_known_issues(
                f"{ticket.get('subject','')} {ticket.get('description','')}", carrier=None
            )
            for hit in hits:
                grouped[hit["id"]].append(ticket)

        out: list[Signal] = []
        for issue_id, matched in grouped.items():
            issue = self.engine._known_issue(issue_id)
            accounts = sorted({t["account_id"] for t in matched})
            open_count = sum(1 for t in matched if t.get("status") == "open")
            severity = "high" if len(accounts) > 1 or open_count > 1 else "medium"
            out.append(
                Signal(
                    signal_id=f"SIG-KI-{issue_id}",
                    type="known_issue_cluster",
                    severity=severity,
                    title=f"{issue_id} {issue['title']} - {len(matched)} ticket(s) across {len(accounts)} account(s)",
                    detail=(
                        f"Status {issue['status']}, opened {issue['opened']}. "
                        f"Matching tickets: {', '.join(t['ticket_id'] for t in matched)}. "
                        f"Workaround: {issue['workaround']}"
                    ),
                    evidence={"tickets": matched, "known_issue": issue},
                    recommended_action=(
                        "Link these tickets to the known issue, give every affected customer the same "
                        "workaround, and feed the ticket count back to engineering."
                    ),
                    accounts=accounts,
                    citations=[
                        {
                            "doc_id": "DOC-PRODUCT-OPS",
                            "clause": f"2. Current known issues / {issue_id}",
                            "authority_tier": 3,
                        }
                    ],
                )
            )
        return out

    # ------------------------------------------------------------------
    def _recurring_themes(self, tickets: list[dict[str, Any]], now: datetime) -> list[Signal]:
        """Cheap lexical theme detection.

        Tickets are grouped by the *terms they actually share*, not by transitive
        similarity - chaining A-B-C through different term pairs produced one
        useless mega-cluster. Terms common to most tickets ("shipment") are
        dropped first, and a theme only surfaces if it is either strongly shared
        (3+ terms) or recurring over time within one account.
        """
        out: list[Signal] = []
        by_id = {t["ticket_id"]: t for t in tickets}
        token_map = {t["ticket_id"]: _tokens(f"{t.get('subject','')} {t.get('description','')}") for t in tickets}
        doc_freq = Counter(tok for toks in token_map.values() for tok in set(toks))
        cutoff = max(2, int(0.4 * len(tickets)))
        token_map = {tid: {t for t in toks if doc_freq[t] <= cutoff} for tid, toks in token_map.items()}

        # pair -> shared salient terms
        themes: list[tuple[set[str], set[str]]] = []  # (shared terms, ticket ids)
        ids = list(by_id)
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                shared_terms = token_map[a] & token_map[b]
                if len(shared_terms) < 2:
                    continue
                for terms, members in themes:
                    if len(terms & shared_terms) >= 2:  # same theme, extend it
                        terms &= shared_terms | terms
                        members.update({a, b})
                        break
                else:
                    themes.append((set(shared_terms), {a, b}))

        for shared_terms, members in themes:
            cluster = [by_id[m] for m in sorted(members)]
            key = frozenset(members)
            accounts = sorted({t["account_id"] for t in cluster})
            dates = sorted(parse_ts(t["created_at"]) for t in cluster)
            span_days = (dates[-1] - dates[0]).days
            shared = sorted(shared_terms)
            strong = len(shared) >= 3
            recurring_same_account = len(accounts) == 1 and span_days >= 1
            if not (strong or recurring_same_account):
                continue
            out.append(
                Signal(
                    signal_id=f"SIG-THEME-{'-'.join(sorted(key))[:40]}",
                    type="recurring_theme",
                    severity="high" if len(accounts) > 1 else "medium",
                    title=(
                        f"Recurring theme across {len(cluster)} tickets"
                        + (f" and {len(accounts)} accounts" if len(accounts) > 1 else "")
                        + f": {', '.join(shared[:4])}"
                    ),
                    detail=(
                        f"Tickets {', '.join(sorted(key))} share the terms {', '.join(shared[:6])} "
                        f"over {span_days} day(s). Repeat contacts on the same theme usually mean the "
                        "underlying defect or the customer-facing explanation is unresolved."
                    ),
                    evidence={"tickets": cluster, "shared_terms": shared},
                    recommended_action="Confirm whether one root cause explains all of them before answering "
                    "each ticket separately.",
                    accounts=accounts,
                    confidence="medium",
                )
            )
        return out

    # ------------------------------------------------------------------
    def _order_anomalies(self, orders: list[dict[str, Any]], now: datetime) -> list[Signal]:
        out: list[Signal] = []
        for order in orders:
            account = self.pack.accounts[order["account_id"]]
            window_end = parse_ts(order["pickup_window_end"])
            actual = parse_ts(order.get("pickup_actual_at"))
            status = order["status"].upper()

            if status == "BOOKED" and not actual and window_end and now > window_end:
                late_h = (now - window_end).total_seconds() / 3600.0
                credit = self.engine.evaluate_service_credit(order_id=order["order_id"], at=now)
                out.append(
                    Signal(
                        signal_id=f"SIG-PICKUP-{order['order_id']}",
                        type="overdue_pickup",
                        severity="high" if credit.decision == "eligible" else "medium",
                        title=(
                            f"{order['order_id']} ({account['account_name']}) pickup {late_h:.1f}h overdue "
                            f"and still not collected"
                        ),
                        detail=(
                            f"Carrier {order['carrier']}, window ended {order['pickup_window_end']}. "
                            f"Credit evaluation: {credit.summary}"
                        ),
                        evidence={"order": order, "credit": credit.to_dict()},
                        recommended_action=(
                            f"Proactively offer the INR {credit.facts.get('credit_amount_inr', 0):.0f} credit and "
                            "chase the carrier."
                            if credit.decision == "eligible"
                            else "Chase the carrier and confirm fault before promising anything."
                        ),
                        accounts=[order["account_id"]],
                        citations=credit.citations,
                    )
                )

            requested = parse_ts(order.get("cancellation_requested_at"))
            if requested and status in {"BOOKED", "PICKED_UP"}:
                cancel = self.engine.evaluate_cancellation(order["order_id"], at=now)
                out.append(
                    Signal(
                        signal_id=f"SIG-CANCEL-{order['order_id']}",
                        type="unactioned_cancellation_request",
                        severity="medium",
                        title=(
                            f"{order['order_id']} ({account['account_name']}) cancellation requested "
                            + (
                                "just now"
                                if (now - requested).total_seconds() < 300
                                else f"{humanize_minutes((now - requested).total_seconds() / 60)} ago"
                            )
                            + f", order still {status}"
                        ),
                        detail=cancel.summary,
                        evidence={"order": order, "cancellation": cancel.to_dict()},
                        recommended_action=(
                            "Action the cancellation per the decision above; the fee window keeps running."
                            if status == "BOOKED"
                            else "Order is already picked up - switch the customer to return-to-origin."
                        ),
                        accounts=[order["account_id"]],
                        citations=cancel.citations,
                    )
                )
        return out

    # ------------------------------------------------------------------
    def _volume_spike(self, tickets: list[dict[str, Any]], now: datetime) -> list[Signal]:
        created = [parse_ts(t["created_at"]) for t in tickets if parse_ts(t["created_at"])]
        if len(created) < 3:
            return []
        window_start = now - timedelta(hours=6)
        recent = [d for d in created if d >= window_start]
        older = [d for d in created if d < window_start]
        if not recent:
            return []
        baseline_days = max(1.0, ((now - min(created)).total_seconds() / 86400.0))
        baseline_per_6h = (len(older) / baseline_days) / 4.0 if older else 0.0
        if len(recent) >= 3 and len(recent) >= max(2.0, baseline_per_6h * 2):
            recent_tickets = [t for t in tickets if parse_ts(t["created_at"]) and parse_ts(t["created_at"]) >= window_start]
            accounts = sorted({t["account_id"] for t in recent_tickets})
            return [
                Signal(
                    signal_id="SIG-SPIKE-6H",
                    type="volume_spike",
                    severity="medium",
                    title=f"{len(recent)} tickets in the last 6 hours vs a {baseline_per_6h:.1f} baseline",
                    detail=(
                        f"Tickets in window: {', '.join(sorted(t['ticket_id'] for t in recent_tickets))}, "
                        f"across {len(accounts)} account(s). Baseline is the trailing average over "
                        f"{baseline_days:.1f} days of data in the pack."
                    ),
                    evidence={"tickets": recent_tickets, "baseline_per_6h": round(baseline_per_6h, 2)},
                    recommended_action="Check for a shared root cause before triaging individually; "
                    "staff the queue for the next few hours.",
                    accounts=accounts,
                    confidence="medium",
                )
            ]
        return []

    # ------------------------------------------------------------------
    def _stale_guidance(self, tickets: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        for ticket in tickets:
            if not ticket.get("historical_resolution"):
                continue
            audit = self.engine.check_historical_guidance(ticket)
            if audit.decision != "contradicts_current_policy":
                continue
            account = self.pack.accounts[ticket["account_id"]]
            out.append(
                Signal(
                    signal_id=f"SIG-STALE-{ticket['ticket_id']}",
                    type="incorrect_past_guidance",
                    severity="high",
                    title=f"{ticket['ticket_id']} ({account['account_name']}): past answer conflicts with current rules",
                    detail=" ".join(audit.conflicts),
                    evidence={"ticket": ticket, "audit": audit.to_dict()},
                    recommended_action=(
                        "Correct the record with the customer if it still affects them, and make sure the "
                        "wrong answer is not reused as precedent."
                    ),
                    accounts=[ticket["account_id"]],
                    citations=audit.citations,
                )
            )
        return out

    # ------------------------------------------------------------------
    def _carrier_concentration(
        self, tickets: list[dict[str, Any]], orders: list[dict[str, Any]], now: datetime | None = None
    ) -> list[Signal]:
        now = now or self.engine.snapshot
        counts = Counter()
        for order in orders:
            window_end = parse_ts(order["pickup_window_end"])
            late = bool(window_end and window_end < now and not parse_ts(order.get("pickup_actual_at")))
            if late and order["status"].upper() == "BOOKED":
                counts[order["carrier"]] += 1
        mentions = Counter()
        for ticket in tickets:
            blob = f"{ticket.get('subject','')} {ticket.get('description','')}".lower()
            for carrier in {o["carrier"] for o in self.pack.orders.values()}:
                if carrier.lower() in blob:
                    mentions[carrier] += 1
        out = []
        for carrier, count in (counts + mentions).items():
            if count >= 2:
                affected = sorted(
                    {o["account_id"] for o in orders if o["carrier"] == carrier}
                    | {t["account_id"] for t in tickets if carrier.lower() in f"{t.get('subject','')} {t.get('description','')}".lower()}
                )
                out.append(
                    Signal(
                        signal_id=f"SIG-CARRIER-{carrier.upper().replace(' ', '-')}",
                        type="carrier_concentration",
                        severity="medium",
                        title=f"{carrier}: {count} overdue pickups / mentions across {len(affected)} account(s)",
                        detail="Multiple live problems point at one carrier. Worth a carrier-level check "
                        "rather than per-ticket handling.",
                        evidence={
                            "carrier": carrier,
                            "overdue_orders": [o["order_id"] for o in orders if o["carrier"] == carrier],
                        },
                        recommended_action="Raise with carrier ops; check whether a known integration issue "
                        "(e.g. KI-211 webhook delay) explains part of it.",
                        accounts=affected,
                        confidence="medium",
                    )
                )
        return out
