# Architecture Note

## 1. Shape of the system

```
Browser (single-page chat + ops console)
   │  POST /api/chat            POST /api/actions/{id}/confirm       GET /api/signals
   ▼
FastAPI (app/main.py)
   │
   ├── OpsAgent (app/agent.py) ──► Groq chat.completions + tool schemas
   │        plan → call tool → observe → answer, max 8 steps
   │
   └── ToolRegistry (app/tools/registry.py)   ← the ONLY path to data or state
            │  enforce role + account scope → redact → execute → audit
            ├── DocumentIndex   (retrieval.py)   authority-weighted BM25
            ├── PolicyEngine    (policy_engine.py) deterministic decisions
            ├── SignalEngine    (signals.py)     proactive detection
            └── OpsStore        (store.py)       pending actions, escalations, audit log
                                    ▲
                            confirm endpoint only
```

The central decision: **the model orchestrates, the code decides.** Anything with a correct answer —
a fee, a credit, a deadline, a precedence conflict — is computed in Python and handed to the model as a
result to narrate. The model's job is language and routing.

## 2. Agent design

- **Loop.** Tool-calling loop over Groq's OpenAI-compatible API, capped at `MAX_AGENT_STEPS` (8).
  Hitting the cap returns an honest "I stopped without a confident answer" plus the trace, rather than
  an improvised answer.
- **Model.** `openai/gpt-oss-20b` — reliable parallel tool calling, fast enough for a live support
  console, and cheap. Swappable via `GROQ_MODEL`; nothing in the code is model-specific.
- **Temperature 0.1.** Support answers should be boring and repeatable.
- **System prompt** carries only what the model must know at runtime: who the user is, their account
  scope, the snapshot time that means "now", the precedence order, the ban on doing its own arithmetic,
  the confirmation rule, and the escalation triggers. Every rule in it is *also* enforced in code, so
  the prompt is guidance, not the security boundary.
- **Injection stance.** Ticket text, notes and documents reach the model as tool results and are
  explicitly framed as data. Because tools authorise per call, injected instructions cannot widen scope
  or execute an action.
- **Failure modes are explicit.** LLM unreachable, step limit exhausted, tool denied, evaluator
  `unknown` — each has a defined response, and none of them ends in a guess.

## 3. Tool design

Thirteen tools in three families (document retrieval / structured lookup & calculation / state change).
Principles:

1. **Decision tools, not data dumps.** `evaluate_cancellation` returns a decision, the facts behind it,
   an ordered source chain, assumptions, conflicts, verification needs, `requires_human` and a
   confidence band. The model cannot reach a wrong conclusion from a right result without contradicting
   text placed directly in front of it.
2. **Enforcement inside the tool.** `require_account` / `require` run before any read. Denials return a
   structured `access_denied` object that tells the model to stop, not to retry another route.
3. **Actions cannot execute.** `propose_*` writes a pending action and returns a preview. Execution
   lives in `execute_confirmed_action`, reachable only from the confirm endpoint, which re-checks role,
   scope, proposer identity, credit authority and expiry. This is a server-side gate, not a UI
   convention — the model has no code path to it.
4. **Same tools, hypothetical or real.** `evaluate_service_credit` takes an `order_id` *or* a scenario
   (hours late, fault flags, fee), so "a pickup is three hours late…" is answered by the same rules that
   answer a live order — and the answer states that a contract could change it.
5. **Every call is audited**, including denials and cancellations, with actor, role, arguments and
   outcome.

## 4. Document handling

- Six documents → markdown with front matter (`doc_id`, `doc_type`, `status`, `effective_date`,
  `superseded_by`, `applies_to`, `confidential`, `authority_tier`). The PDFs ship in
  `data/source_pack/` and `scripts/ingest_pack.py --documents` verifies that every figure in the
  markdown still exists in its PDF.
- **Chunking is clause-level** (markdown headings), so a citation is "SOP-CANCEL-CREDIT-V4 §2. Failed-
  pickup service credits", not "page 1".
- **Retrieval is BM25 × authority weight** (tier 1 contract ×1.35 … tier 5 deprecated ×0.35), with
  deprecated documents excluded unless explicitly requested and confidential contracts filtered by the
  caller's account scope.
- **Why not embeddings?** Six short policy documents, queries that share vocabulary with the clauses,
  and a hard requirement for deterministic, testable behaviour. An embedding index adds a model
  dependency, a network hop and a threshold to tune, and would still need the same authority and
  permission layer on top. The interface (`DocumentIndex.search`) is where a hybrid retriever would
  drop in when the corpus grows past a few hundred clauses.

## 5. Structured-data handling

- Workbook → three CSVs plus `dataset_meta.json` (snapshot, timezone, currency), loaded into plain
  dictionaries at boot. Six orders and seven tickets do not need a database; the loader interface is
  the seam where a real API or warehouse would replace them.
- **Rules registry.** `policy_rules.json` and `contract_overrides.json` are a machine-usable projection
  of the current documents: SLA matrix, cancellation states, credit rules, approval thresholds,
  severity signals, known issues, and per-account contract overrides — each with the clause and quote it
  came from. Prose is for citation; the registry is for computation.
- **Why a registry rather than LLM extraction at query time?** Extraction is where hallucination is
  most expensive, and re-extracting the same clause on every request is both slow and non-reproducible.
  Extract once, review, commit, test against the source — and let retrieval serve the prose alongside.
  Adding a customer means adding a contract entry, not touching code.
- **The dataset snapshot is "now"** everywhere, so every time-based answer is reproducible.

## 6. Source reliability and conflict handling

| Tier | Source | Treatment |
| --- | --- | --- |
| 1 | Signed customer agreement | Wins outright; cited first; the overridden default is still shown |
| 2 | Current support policy / SOP | Default rules |
| 3 | Current product documentation | Capabilities, known issues, workarounds |
| 4 | Internal notes | Context only |
| 5 | Deprecated / superseded documents | Excluded from retrieval by default; labelled if forced |

Concretely, on the planted traps in the pack:

- **ORD-1001** — SOP says INR 250 (two hours after booking); Northstar's agreement waives it. Answer:
  no fee, with the conflict stated.
- **ORD-2002** — SOP would pay 10% of INR 2,400 above a 2h delay; LumenWorks' agreement replaces both
  with a flat INR 300 above 4h. Answer: INR 300.
- **"Three hours late"** — eligible under the default SOP, *not* eligible for LumenWorks. The tool
  answers the default and says which fact would change it.
- **Support Policy v2** — never surfaces as current; its Enterprise "1 hour" P1 cannot leak into the
  registry (a provenance test pins v3's 30 minutes).
- **TKT-450 / TKT-451** — both historical resolutions are wrong today; `audit_historical_guidance`
  detects them generically (a fee claim against a contract waiver, a row-limit claim against the product
  doc plus KI-208) and the ops dashboard raises them without being asked.
- **Unknown fault or timing** — `decision: "unknown"`, `requires_human: true`, no promise.
- **KI-211** — a BOOKED SwiftShip order may already be collected, so cancellation answers carry a
  "verify with the carrier first" note before a state-changing step.

## 7. Trade-offs taken

| Decision | Why | Cost / when to revisit |
| --- | --- | --- |
| Deterministic engine over model reasoning | Correctness, testability, auditability | More code per rule; every new rule needs a registry entry |
| Rules registry extracted once | Reproducible, reviewable, cheap | Needs a refresh step when documents change (guarded by provenance tests) |
| Lexical BM25 retrieval | Deterministic, no extra services | Weak on paraphrase; revisit at a few hundred clauses |
| In-memory store | Inspectable transaction path for a demo | Nothing survives restart; swap `OpsStore` for Postgres |
| Mocked identity | The brief allows it; shape matches an IdP | Wire to SSO + the real account-assignment service |
| Server-rendered single-file UI | Zero build step, one deployable, easy to review | No component tests or streaming tokens |
| Blocking (non-streaming) responses | Simpler correctness story for tool loops | Perceived latency on multi-step answers |
| Business calendar assumed | The pack never defines it | Must be confirmed with ParcelPilot; it is configuration, and surfaced in answers |

## 8. Testing

79 tests, no network:

- `test_policy_engine.py` — the golden scenarios a reviewer checks by hand (contract vs SOP, credit
  thresholds and caps, unknown-fault refusal, SLA breach maths, the weekend business-hours case,
  historical-guidance audits, severity classification).
- `test_access_control.py` — cross-account denial, contract filtering in retrieval, redaction for the
  read-only role, propose-changes-nothing, confirm-executes-once, wrong-user confirm, credit-authority
  block, expiry, audit coverage.
- `test_retrieval_and_signals.py` — deprecated exclusion and demotion, contract outranking the SOP,
  clause-level citations, each detector.
- `test_agent_and_api.py` — the agent loop against a scripted LLM: tool chaining, denial handling,
  proposal-only actions, step limit, LLM failure, and the HTTP surface end to end.
- `test_provenance.py` — the registry cannot drift from the documents it claims to project.
