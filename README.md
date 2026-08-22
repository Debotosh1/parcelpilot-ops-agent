# ParcelPilot — Internal Support / Operations Copilot

An internal chatbot + proactive ops console for ParcelPilot's customer-operations team, built on the
supplied assessment data pack. Authorised ParcelPilot staff ask questions in plain English; the agent
retrieves the right clause, computes the answer deterministically, cites its sources, refuses to guess,
and prepares — but never silently performs — state-changing actions.

**Chosen user context:** internal support / operations staff (not customer-facing).
**LLM:** Groq (`openai/gpt-oss-20b` by default) for planning, tool selection and narration only.
**Additional client problems addressed:** both — proactive issue detection (Problem 1) and
trust/reliability (Problem 2).

---

## Quick start

```bash
git clone <your-repo-url> && cd parcelpilot-ops-agent
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env        # then put your Groq key in it (https://console.groq.com/keys)
uvicorn app.main:app --reload
# open http://localhost:8000
```

Run the test suite (79 tests, no API key or network needed — the LLM is scripted):

```bash
pytest -q
```

Docker:

```bash
docker build -t parcelpilot-copilot .
docker run -p 8000:8000 -e GROQ_API_KEY=gsk_... parcelpilot-copilot
```

Deploy: `render.yaml` is a ready Render blueprint (free tier). New → Blueprint → select the repo →
paste `GROQ_API_KEY`. Any container host works; the app is a single stateless process.

> Without `GROQ_API_KEY` the app still boots: the Ops Signals dashboard, the audit log and every
> deterministic evaluator work, and the chat pane says the key is missing. Nothing is mocked when the
> key *is* present.

---

## What it does

### 1. Chat with tools (minimum requirements 1, 3, 5)

Thirteen tools in three families; the tool trace under every answer shows which fired, with what
arguments, and what came back.

| Family | Tools |
| --- | --- |
| **Document retrieval** | `search_policy_documents` — authority-ranked BM25 over policies, SOPs, product docs and signed agreements |
| **Structured lookup & calculation** | `get_account`, `get_order`, `get_ticket`, `search_tickets`, `evaluate_cancellation`, `evaluate_service_credit`, `compute_sla_status`, `audit_historical_guidance`, `detect_operational_signals` |
| **State-changing actions** | `propose_escalation`, `propose_ticket_update`, `propose_followup_task` |

A question like *"Can Northstar cancel ORD-1001 without a cancellation fee?"* runs order lookup →
account lookup → contract clause → SOP clause → elapsed-time calculation → decision, and answers with
the numbers and the clause each one came from.

### 2. Access control enforced in the data layer (minimum requirement 2)

Four mocked internal users across three roles. Scope is checked inside the tool layer before any data
is touched, so a jailbroken prompt changes nothing:

| User | Role | Sees | Can do |
| --- | --- | --- | --- |
| Rohit Sharma | Support Agent | ACCT-001, ACCT-003, ACCT-004 | propose + confirm actions, credits ≤ INR 1,000 |
| Maya Iyer | Support Agent | ACCT-001, ACCT-002 | same, for her accounts |
| Priya Mehta | Ops Manager | all accounts | everything, including credit approval and the full audit log |
| Sam Verma | Ops Analyst (read-only) | all accounts, **customer free-text redacted** | nothing state-changing |

Try it: as Rohit, ask *"What's happening with LumenWorks order ORD-2001?"* — the tool returns
`access_denied`, the trace shows a red `denied` chip, and the agent says so instead of answering.
Another customer's signed agreement is also filtered out of retrieval for out-of-scope agents.

### 3. Confirmation before any action (minimum requirement 4)

`propose_*` tools cannot execute. They persist a pending action and return a preview; the UI renders a
confirmation card; only `POST /api/actions/{id}/confirm` executes, and it re-checks role, account scope,
proposer identity, credit authority and a 15-minute expiry. Ask *"escalate TKT-501"* and watch nothing
happen until you click **Confirm & execute**.

### 4. Proactive issue detection — Client Problem 1

The **Ops signals** tab sweeps everything at the dataset snapshot and ranks what deserves attention,
without anyone asking. Seven detectors: SLA breach / at-risk (contract-aware, business-hours aware),
open P1s, known-issue clusters (KI-208, KI-211), recurring themes across customers and time, overdue
pickups with their credit evaluation attached, unactioned cancellation requests, ticket volume spikes,
carrier concentration, and **past guidance that today's rules contradict**. Every signal carries its
evidence, a recommended action and a confidence level. Detection is pure Python — no model in the loop.

### 5. Trust and reliability — Client Problem 2

- **Authority tiers.** Signed agreement (1) → current policy/SOP (2) → current product doc (3) →
  internal note (4) → deprecated/superseded (5). Retrieval multiplies BM25 by tier weight, excludes
  deprecated documents by default, and labels them loudly if explicitly requested.
- **No model arithmetic.** Fees, credits, thresholds and deadlines come from `app/policy_engine.py`;
  the LLM only narrates. Conflicts are reported, not silently resolved ("SOP would charge INR 250,
  the signed agreement waives it, contract wins").
- **Unknowns block promises.** Missing fault or timing returns `decision: "unknown"` with a
  verification list, per SOP §3.
- **Historical answers are audited, not trusted.** `audit_historical_guidance` compares a closed
  ticket's resolution with today's rules and flags both planted mistakes in the pack (TKT-450's
  cancellation fee, TKT-451's "3,000-row plan limit").
- **Assumptions are surfaced.** The pack never defines working hours, so the business calendar is
  configuration, not a hidden constant, and every business-hours answer says so.
- **Everything is auditable.** Each tool call, denial, proposal and execution is logged and visible in
  the Audit tab.

---

## Repository layout

```
app/
  main.py            FastAPI routes (chat, confirm, signals, audit, tool, health)
  agent.py           Groq tool-calling loop + operating rules (system prompt)
  llm.py             Groq client wrapper
  tools/registry.py  the 13 tools: enforcement, redaction, audit, propose-only actions
  policy_engine.py   deterministic decisions: cancellation, credits, SLA, historical audit
  retrieval.py       clause-level chunking + authority-weighted BM25
  signals.py         proactive detection (Problem 1)
  rbac.py            principals, permissions, account scope, redaction
  store.py           mutable ops state, pending actions, audit log
  business_time.py   business-hours / business-days arithmetic
  static/index.html  chat UI, tool trace, confirmation cards, ops dashboard, audit view
data/
  source_pack/       the six PDFs + the workbook, exactly as supplied
  documents/         the same documents as markdown + authority front matter
  structured/        accounts / orders / tickets CSVs, snapshot metadata, mocked users
  structured/rules/  policy_rules.json + contract_overrides.json (the rules registry)
scripts/ingest_pack.py   re-ingest the workbook; verify markdown against the PDFs
tests/                   79 tests: policy correctness, access control, retrieval, signals, agent, API
```

`ARCHITECTURE.md` covers agent/tool design and trade-offs. `PRODUCT_NOTE.md` covers the product
decisions, what was left out, what comes next and the metric. `DEMO_SCRIPT.md` is the 5-minute video
run sheet. `AI_TOOL_USAGE.md` lists the AI tools used.
`RUNBOOK.md` is the end-to-end guide: key → local run → tests → GitHub → deploy → video → submit.

---

## Data handling

The document corpus is committed as markdown with editorial front matter (authority tier, status,
`superseded_by`, account scope) because a PDF does not carry that metadata. Rules the engine computes
with live in `data/structured/rules/*.json` — a machine-usable **projection** of the current documents,
where every rule points back at the clause it came from. The tests in `tests/test_provenance.py` fail if
a number in the registry stops matching the document text, and `scripts/ingest_pack.py --all` rebuilds
the CSVs from the workbook and verifies every figure in the markdown against the source PDF.

Nothing about the example questions is hard-coded. Add an account with no contract and it falls back to
the plan defaults; add a contract to `contract_overrides.json` and the same code answers for it.

## Example questions to try

| Question | What it exercises |
| --- | --- |
| Can Northstar cancel ORD-1001 without a cancellation fee? Explain why. | contract beats SOP; conflict stated openly; KI-211 verification note |
| A pickup is three hours late because of carrier fault. Should I get a service credit? | default SOP path; flags that a contract could change the answer |
| Same question, but for LumenWorks | contract raises the threshold to 4h → not eligible |
| Is ORD-2002 owed a credit, and how much? | contract flat INR 300, not the SOP's 10% |
| Which open tickets have breached first response? | contract SLA + 24x7 vs business-hours clocks |
| TKT-450 said an INR 250 fee applied. Is that still right? | historical-guidance audit: the past answer was wrong |
| What should I do about TKT-505? | P1 security → escalation prepared, awaiting confirmation |
| What's happening with ORD-2001? (as Rohit) | access denied at the tool layer |
| Show me what needs attention right now. | proactive signals inside chat |
