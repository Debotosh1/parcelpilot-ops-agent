"""Identity, roles and enforcement.

Access control lives here and is called by the *tool layer*, not by the prompt.
A model that decides to ignore its instructions still cannot read an account it
has no scope for, because the tool raises before touching data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .loaders import DataPack

REDACTED = "[redacted: customer content not visible to this role]"
REDACTED_FIELDS = ("description", "notes", "historical_resolution", "subject")


class AccessDenied(PermissionError):
    """Raised by the tool layer when a principal may not perform an operation."""

    def __init__(self, message: str, *, permission: str | None = None, resource: str | None = None):
        super().__init__(message)
        self.permission = permission
        self.resource = resource

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "access_denied",
            "message": str(self),
            "permission": self.permission,
            "resource": self.resource,
        }


@dataclass
class Principal:
    user_id: str
    name: str
    role: str
    role_label: str
    permissions: frozenset[str]
    account_scope: str  # "all" | "assigned"
    assigned_accounts: tuple[str, ...] = ()
    redact_customer_content: bool = False
    limits: dict[str, Any] = field(default_factory=dict)

    # -- capability checks -------------------------------------------------
    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise AccessDenied(
                f"Role '{self.role_label}' does not have permission '{permission}'.",
                permission=permission,
            )

    def allowed_accounts(self, pack: DataPack) -> set[str]:
        if self.account_scope == "all":
            return set(pack.accounts.keys())
        return set(self.assigned_accounts)

    def require_account(self, pack: DataPack, account_id: str | None) -> None:
        if account_id is None:
            return
        if account_id not in pack.accounts:
            raise AccessDenied(f"Unknown account {account_id}.", resource=account_id)
        if account_id not in self.allowed_accounts(pack):
            raise AccessDenied(
                f"{self.name} ({self.role_label}) is not assigned to {account_id} "
                f"({pack.accounts[account_id]['account_name']}). Assigned accounts: "
                f"{', '.join(self.assigned_accounts) or 'none'}.",
                permission="data.read_scoped",
                resource=account_id,
            )

    def max_credit_without_approval(self) -> float:
        return float(self.limits.get("max_credit_inr_without_approval", 0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "role": self.role,
            "role_label": self.role_label,
            "permissions": sorted(self.permissions),
            "account_scope": self.account_scope,
            "assigned_accounts": list(self.assigned_accounts),
            "redact_customer_content": self.redact_customer_content,
            "limits": self.limits,
        }


class Directory:
    def __init__(self, pack: DataPack):
        self._roles = pack.users["roles"]
        self._users = {u["user_id"]: u for u in pack.users["users"]}

    @property
    def roles(self) -> dict[str, Any]:
        return self._roles

    def list_users(self) -> list[dict[str, Any]]:
        return [self.get(uid).to_dict() for uid in self._users]

    def get(self, user_id: str) -> Principal:
        user = self._users.get(user_id)
        if not user:
            raise AccessDenied(f"Unknown user '{user_id}'. Sign in again.", resource=user_id)
        role = self._roles[user["role"]]
        return Principal(
            user_id=user["user_id"],
            name=user["name"],
            role=user["role"],
            role_label=role.get("label", user["role"]),
            permissions=frozenset(role.get("permissions", [])),
            account_scope=role.get("account_scope", "assigned"),
            assigned_accounts=tuple(user.get("assigned_accounts", [])),
            redact_customer_content=bool(role.get("redact_customer_content", False)),
            limits=role.get("limits", {}),
        )


def redact_record(record: dict[str, Any], principal: Principal) -> dict[str, Any]:
    """Strip free-text customer content for roles that may not read it."""
    if not principal.redact_customer_content:
        return record
    cleaned = dict(record)
    for key in REDACTED_FIELDS:
        if cleaned.get(key):
            cleaned[key] = REDACTED
    return cleaned


def redact_records(records: Iterable[dict[str, Any]], principal: Principal) -> list[dict[str, Any]]:
    return [redact_record(r, principal) for r in records]
