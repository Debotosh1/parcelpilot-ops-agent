# 5-Minute Demo Video — Run Sheet

Record at 1080p with the browser at ~90% zoom so the tool trace is readable. Have the app running with
a valid `GROQ_API_KEY`, signed in as **Rohit Sharma (Support Agent)**, Ops Signals tab visible.

Rehearse once: every prompt below is copy-paste from the app's example chips.

---

### 0:00–0:35 — What it is

> "This is ParcelPilot's internal support and operations copilot — for ParcelPilot's own agents, not
> customers. It answers from the supplied pack only, computes decisions deterministically, cites the
> clause, and never performs an action without a human clicking Confirm."

Show the header: snapshot `2026-08-16 11:00 Asia/Kolkata` is treated as "now", model is Groq
`openai/gpt-oss-20b`, the signed-in user and their account scope.

### 0:35–1:25 — Architecture (one slide or the README diagram)

> "FastAPI serves a chat UI. Groq plans and narrates; it never does arithmetic. Every data access goes
> through one tool layer that enforces role and account scope, redacts, and audits. Behind it: an
> authority-ranked document index, a deterministic policy engine, a proactive signal engine, and a
> store where state-changing actions wait for confirmation."

Name the three tool families and the authority order: **contract > current policy/SOP > product doc >
historical tickets**.

### 1:25–2:20 — Multi-step answer with a contract conflict

Ask: **"Can Northstar cancel ORD-1001 without a cancellation fee? Explain why."**

Point at, in the trace: `get_order` → `evaluate_cancellation`, and in the answer:

- No fee — the SOP would charge INR 250 two hours after booking, the signed agreement waives it.
- The citation chips: contract tier 1 first, SOP tier 2 second.
- The KI-211 note: a BOOKED SwiftShip order may already be collected — verify before cancelling.

> "The model didn't decide this. The engine did, and reported the conflict rather than smoothing it
> over."

### 2:20–3:00 — The same question, two different right answers

Ask: **"A pickup is three hours late because of carrier fault. Should I get a service credit?"**
→ eligible under the default SOP, INR 500 cap / 10% of fee, and it flags that a signed agreement could
change the answer.

Then: **"What if that customer is LumenWorks?"**
→ not eligible: their agreement raises the threshold to 4 hours.

> "Nothing about these is hard-coded. Add a contract to the registry and the same code answers for it."

### 3:00–3:35 — Access control is real

Still as Rohit, ask: **"What's happening with LumenWorks order ORD-2001?"**

Show the red `denied` chip in the trace and the agent declining to answer. Switch to **Maya Iyer**, ask
again → she gets the answer, because ACCT-002 is hers.

> "That's the tool layer refusing before it touches data — not a prompt instruction the model could
> talk itself out of."

### 3:35–4:15 — Confirmation before action

Ask: **"What should I do about TKT-505?"** (the API-key exposure)

Show: classified P1 per the current policy, first response already breached at the snapshot, escalation
**prepared** with a preview card — and the Ops Signals tab still shows no escalation. Click **Confirm &
execute**, then show the audit log entry and the ticket now marked escalated.

> "Everything up to that click was reversible. The confirm endpoint re-checks role, scope and authority
> server-side — the model has no path to it."

### 4:15–4:50 — Proactive detection and trust (Problems 1 and 2)

Switch to **Priya Mehta (Ops Manager)** and open **Ops signals**:

- Two breached P1s at the top, one a suspected credential exposure.
- A LumenWorks pickup 4.5 hours late that is already owed INR 300 — nobody has asked about it.
- **"Past answer conflicts with current rules"** on TKT-450 and TKT-451 — the two mistakes planted in
  the pack, found without anyone asking.

> "Detection is deterministic Python. And the deprecated Support Policy v2 never appears as current —
> it's excluded from retrieval by construction, not by asking the model nicely."

### 4:50–5:00 — Close

> "79 tests cover the policy decisions, the access control, the confirmation gate and provenance between
> the rules and the source documents. The architecture and product notes cover the trade-offs and what
> I'd build next — starting with feedback-driven evals and a document-change pipeline."

---

**If time is short, cut:** the second half of section 2:20–3:00 (the LumenWorks variant) and the
architecture slide detail. **Never cut:** the confirmation gate and the access-denial moment — they are
explicit requirements.
