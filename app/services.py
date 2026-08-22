"""Composition root: build every service once and hand out request-scoped contexts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .agent import OpsAgent
from .config import settings
from .llm import LLMClient, build_default_client
from .loaders import DataPack, load_data_pack
from .policy_engine import PolicyEngine
from .rbac import Directory, Principal
from .retrieval import DocumentIndex
from .signals import SignalEngine
from .store import OpsStore
from .tools.registry import ToolContext


@dataclass
class Services:
    pack: DataPack
    engine: PolicyEngine
    index: DocumentIndex
    store: OpsStore
    signals: SignalEngine
    directory: Directory
    llm: LLMClient | None = None
    sessions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @classmethod
    def build(cls, llm: LLMClient | None = None) -> "Services":
        pack = load_data_pack()
        engine = PolicyEngine(pack)
        store = OpsStore(pack)
        return cls(
            pack=pack,
            engine=engine,
            index=DocumentIndex(pack.documents),
            store=store,
            signals=SignalEngine(pack, engine, store),
            directory=Directory(pack),
            llm=llm if llm is not None else build_default_client(),
        )

    # -- helpers -----------------------------------------------------------
    def principal(self, user_id: str) -> Principal:
        return self.directory.get(user_id)

    def context(self, user_id: str, session_id: str) -> ToolContext:
        return ToolContext(
            pack=self.pack,
            engine=self.engine,
            index=self.index,
            store=self.store,
            signals=self.signals,
            principal=self.principal(user_id),
            session_id=session_id,
        )

    def agent(self) -> OpsAgent:
        if self.llm is None:
            raise RuntimeError(
                "No LLM client configured. Set GROQ_API_KEY (see .env.example) and restart."
            )
        return OpsAgent(self.llm)

    def history(self, session_id: str) -> list[dict[str, Any]]:
        return self.sessions.setdefault(session_id, [])

    def remember(self, session_id: str, user_message: str, answer: str, keep: int = 8) -> None:
        history = self.history(session_id)
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": answer})
        del history[:-keep]

    def bootstrap(self) -> dict[str, Any]:
        return {
            "app_name": settings.app_name,
            "model": settings.groq_model,
            "llm_configured": bool(self.llm),
            "snapshot": self.engine.snapshot.strftime("%Y-%m-%d %H:%M"),
            "timezone": self.pack.meta.get("timezone"),
            "currency": self.pack.meta.get("currency"),
            "users": self.directory.list_users(),
            "roles": self.directory.roles,
            "accounts": [
                {"account_id": a["account_id"], "account_name": a["account_name"], "plan": a["plan"]}
                for a in self.pack.accounts.values()
            ],
            "documents": [d.to_meta() for d in self.pack.documents],
            "business_calendar": self.pack.policy_rules.get("business_calendar", {}),
        }
