"""Loading of the supplied data pack.

Two families of source material, deliberately handled differently:

* **Documents** (policies, SOPs, contracts, product docs) -> markdown with a
  YAML front-matter block carrying authority metadata. Served through
  retrieval so the agent quotes prose.
* **Structured data** (accounts / orders / tickets) + the *rules registry*
  (a machine-usable projection of the CURRENT documents) -> parsed into
  dictionaries for deterministic computation.

Nothing here knows about the LLM: the whole data layer is testable on its own.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import settings

FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

# Authority tiers: 1 = signed contract, 2 = current policy/SOP,
# 3 = current product doc, 4 = internal note, 5 = deprecated/superseded.
DOC_TYPE_TIER = {"contract": 1, "policy": 2, "sop": 2, "product_doc": 3, "internal_note": 4}


def _coerce(value: str) -> Any:
    v = value.strip()
    if v.lower() in {"true", "yes"}:
        return True
    if v.lower() in {"false", "no"}:
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _parse_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    """Minimal front-matter parser (flat key: value only) - avoids a YAML dependency."""
    match = FRONT_MATTER_RE.match(raw)
    if not match:
        return {}, raw
    meta: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = _coerce(value)
    return meta, match.group(2)


@dataclass
class Document:
    doc_id: str
    title: str
    doc_type: str
    status: str
    text: str
    source_file: str = ""
    effective_date: str | None = None
    superseded_by: str | None = None
    supersedes: str | None = None
    applies_to: str = "ALL"
    confidential: bool = False
    authority_tier: int = 3
    path: str = ""

    @property
    def is_deprecated(self) -> bool:
        return self.status.upper() in {"DEPRECATED", "SUPERSEDED", "ARCHIVED"}

    @property
    def account_id(self) -> str | None:
        return None if self.applies_to == "ALL" else str(self.applies_to)

    def to_meta(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "doc_type": self.doc_type,
            "status": self.status,
            "effective_date": self.effective_date,
            "authority_tier": self.authority_tier,
            "applies_to": self.applies_to,
            "source_file": self.source_file,
        }


def load_documents(directory: Path | None = None) -> list[Document]:
    directory = directory or settings.documents_dir
    docs: list[Document] = []
    for path in sorted(directory.glob("*.md")):
        meta, body = _parse_front_matter(path.read_text(encoding="utf-8"))
        doc_type = str(meta.get("doc_type", "product_doc"))
        docs.append(
            Document(
                doc_id=str(meta.get("doc_id", path.stem)),
                title=str(meta.get("title", path.stem)),
                doc_type=doc_type,
                status=str(meta.get("status", "CURRENT")),
                text=body.strip(),
                source_file=str(meta.get("source_file", path.name)),
                effective_date=meta.get("effective_date") and str(meta.get("effective_date")),
                superseded_by=meta.get("superseded_by") and str(meta.get("superseded_by")),
                supersedes=meta.get("supersedes") and str(meta.get("supersedes")),
                applies_to=str(meta.get("applies_to", "ALL")),
                confidential=bool(meta.get("confidential", doc_type == "contract")),
                authority_tier=int(
                    meta.get(
                        "authority_tier",
                        5 if str(meta.get("status", "")).upper() == "DEPRECATED" else DOC_TYPE_TIER.get(doc_type, 3),
                    )
                ),
                path=str(path),
            )
        )
    if not docs:
        raise RuntimeError(f"No documents found in {directory}")
    return docs


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def _norm_bool(value: Any) -> bool | None:
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in {"true", "yes", "1"}:
        return True
    if v in {"false", "no", "0"}:
        return False
    return None


def parse_ts(value: Any) -> datetime | None:
    """Parse the pack's 'YYYY-MM-DD HH:MM' timestamps (naive, Asia/Kolkata)."""
    if value in (None, "", "nan"):
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


@dataclass
class DataPack:
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    tickets: dict[str, dict[str, Any]] = field(default_factory=dict)
    documents: list[Document] = field(default_factory=list)
    policy_rules: dict[str, Any] = field(default_factory=dict)
    contract_overrides: dict[str, Any] = field(default_factory=dict)
    users: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def snapshot_at(self) -> datetime:
        ts = parse_ts(self.meta.get("dataset_snapshot"))
        if ts is None:  # pragma: no cover - defensive
            raise RuntimeError("dataset_snapshot missing from dataset_meta.json")
        return ts

    def account_by_name(self, name: str) -> dict[str, Any] | None:
        needle = name.strip().lower()
        for acct in self.accounts.values():
            if acct["account_name"].lower() == needle:
                return acct
        for acct in self.accounts.values():  # loose contains match
            if needle and needle in acct["account_name"].lower():
                return acct
        return None


def load_data_pack(data_dir: Path | None = None) -> DataPack:
    data_dir = data_dir or settings.data_dir
    structured = data_dir / "structured"

    accounts = {}
    for row in _read_csv(structured / "accounts.csv"):
        row["premium_support"] = _norm_bool(row.get("premium_support"))
        accounts[row["account_id"]] = row

    orders = {}
    for row in _read_csv(structured / "orders.csv"):
        row["carrier_fault"] = _norm_bool(row.get("carrier_fault"))
        row["customer_fault"] = _norm_bool(row.get("customer_fault"))
        row["shipment_fee_inr"] = (
            float(row["shipment_fee_inr"]) if str(row.get("shipment_fee_inr", "")).strip() else None
        )
        orders[row["order_id"]] = row

    tickets = {row["ticket_id"]: row for row in _read_csv(structured / "tickets.csv")}

    return DataPack(
        accounts=accounts,
        orders=orders,
        tickets=tickets,
        documents=load_documents(data_dir / "documents"),
        policy_rules=json.loads((structured / "rules" / "policy_rules.json").read_text("utf-8")),
        contract_overrides=json.loads(
            (structured / "rules" / "contract_overrides.json").read_text("utf-8")
        ),
        users=json.loads((structured / "users.json").read_text("utf-8")),
        meta=json.loads((structured / "dataset_meta.json").read_text("utf-8")),
    )
