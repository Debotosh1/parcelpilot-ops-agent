# Product Note

## Which user context I built, and why

I built the **internal support / operations copilot**, not the customer-facing bot.

The pack's own contents point there. Half the value in the data is in things you would never say
directly to a customer: which past answers were wrong, which tickets have already breached their SLA,
which known defect explains a complaint, whether a credit needs manager approval. A customer-facing bot
has to hide most of that; an internal one is *made of* it. Internally, a wrong answer is also caught by
the human in front of it, which is the right place to start when the source base is deliberately
imperfect — agents build the calibration the product needs before it ever speaks to a customer.

It is also the shorter path to the customer-facing product: the same tools, the same rules registry and
the same enforcement layer sit behind a narrower role with tighter output rules.

## Which additional client problem I chose

**Both**, because they are the same problem seen from two sides — knowing what is true, and knowing
what is happening.

**Problem 1 — proactive issue detection.** The Ops Signals console runs seven deterministic detectors
over the whole support surface at the dataset snapshot and ranks the result: SLA breaches and at-risk
tickets (contract-aware, business-hours aware), open P1s, known-issue clusters, themes recurring across
customers and time, overdue pickups with the credit already computed, unactioned cancellation requests,
volume spikes, carrier concentration, and past guidance that current rules contradict. Every signal
carries evidence, a recommended action and a confidence level; the agent can query the same detectors in
chat ("what needs attention right now?"). On the supplied data it surfaces, unprompted: two breached
P1s (one a suspected credential exposure), a LumenWorks pickup 4.5 hours late that is already owed a
INR 300 credit, both planted incorrect past answers, and the KI-208 bulk-upload cluster spanning two
tickets five days apart.

Detection is pure Python. If a spike detector only fires when a model feels like it, nobody trusts the
dashboard at 2am.

**Problem 2 — trust and reliability.** Covered in ARCHITECTURE.md §6; the product-level stance is:
never be confidently wrong, and make the reasoning inspectable. Authority tiers decide conflicts,
conflicts are stated rather than smoothed over, unknown facts block promises, assumptions (the business
calendar) are declared in the answer, deprecated documents are excluded by construction, historical
answers are audited before reuse, and every tool call is in an audit log the ops manager can read.

## What I would build next, in priority order

1. **Human feedback on every answer, and an eval set built from it.** One thumb-down with a reason is
   worth more than any amount of prompt tuning. Log answer + trace + verdict, promote disagreements into
   the golden-scenario suite, and gate deploys on it. Without this, quality drifts silently the first
   time a policy changes.
2. **A document-change pipeline.** Today a new policy version means editing the rules registry by hand.
   It should be: drop the PDF in, LLM extracts candidate rule deltas, a human approves a diff
   ("Enterprise P1: 30 min → 20 min, effective 1 Oct"), the registry versions itself and the provenance
   tests re-run. Policy freshness is the single biggest source of confidently wrong answers.
3. **Draft customer replies, not just internal answers.** The agent already knows the decision, the
   citation and the caveats; the highest-leverage next step is turning that into a reply the agent edits
   and sends — with the internal-only parts (known-issue IDs, fault attribution, past mistakes) stripped
   by construction rather than by instruction.
4. **Real write-back integrations.** Zendesk/Freshdesk, PagerDuty for P1, Slack for the signal feed,
   with the same propose-confirm-audit gate. Actions that stop at a local store are a demo, not a
   workflow.
5. **A credits ledger.** LumenWorks' and Northstar's caps (INR 5,000 monthly aggregate) cannot be
   enforced without one — today the agent correctly says "check the month-to-date total first". That
   check should be a tool call.
6. **Then the customer-facing bot**, launched behind agent review for the first weeks: same tools,
   customer role, hard rule that anything `requires_human` never reaches the customer unedited.
7. **Business-hours and holiday calendar as real configuration**, per region, replacing the assumption.

## What I intentionally left out

- **A customer-facing chatbot.** In scope for the brief as an option; excluded to do one context
  properly rather than two halfway.
- **Embeddings / a vector store.** Six documents. Lexical retrieval with authority ranking is more
  accurate here and fully testable; the seam for a hybrid retriever is one class.
- **A database.** In-memory state keeps the transaction path visible for review. `OpsStore` is the only
  file that changes.
- **Real authentication.** Mocked users and roles, as the brief permits. The enforcement layer is real;
  only identity is stubbed, so wiring SSO does not move the security boundary.
- **Streaming responses.** Multi-step tool loops are easier to reason about (and to show in a trace)
  when they resolve atomically. Cost: a few seconds of visible "working…".
- **Automatic execution of anything.** Even for a P1, the copilot prepares and asks. The failure cost of
  a wrong autonomous escalation on a strategic account is much higher than one click.
- **Multilingual, voice, mobile layout, i18n of currency.** Not what this assessment is testing.

## One metric

**Percentage of answered requests that a human accepts without correction — "clean-answer rate" —
measured per source-authority path (contract-overridden vs default policy).**

It is the only number that moves for the right reason: it goes up when retrieval finds the right clause,
when the engine computes the right number, and when the agent refuses instead of guessing; it goes down
the moment a policy changes and the registry lags. Splitting it by path matters because contract-
overridden answers are where the money and the strategic accounts are, and where a plausible-looking
default answer is most damaging.

Supporting instruments, not headline metrics: escalation precision (share of prepared escalations a
human confirms), time-to-first-response on tickets the signal console flagged versus those it did not,
and the count of answers blocked as `unknown` — which should be non-zero. A copilot that never says "I
need to verify that" is not being careful.
