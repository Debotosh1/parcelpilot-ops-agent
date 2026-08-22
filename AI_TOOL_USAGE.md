# AI Tool Usage

**Tools used:** Claude (Anthropic) in an agentic coding session, with file, shell and test-execution
access. No other AI coding assistant was used. Groq is a runtime dependency of the product, not a
development tool.

**How it was used**

- *Source ingestion.* Extracting the six PDFs and the workbook into the repo's markdown corpus and CSVs,
  then writing `scripts/ingest_pack.py` so the conversion is repeatable and verifiable rather than a
  one-off copy-paste.
- *Implementation.* Drafting the FastAPI service, the tool layer, the retrieval index, the business-hours
  arithmetic, the signal detectors and the single-page UI, then iterating against the test suite.
- *Test-first on the parts that matter.* The golden scenarios (contract vs SOP, credit thresholds, SLA
  breach maths, the weekend business-hours case) were written as expected values first and used to catch
  real bugs — the closed-ticket SLA state, the over-eager theme clustering, and incomplete redaction of
  nested evidence payloads were all found this way.
- *Documentation.* First drafts of the README, architecture note and product note.

**What was decided by hand, not delegated**

The product decisions and the architecture: choosing the internal ops context over the customer-facing
bot; keeping every policy decision in a deterministic engine rather than in the model; the authority-tier
model and the rules-registry-with-provenance approach; propose/confirm as a server-side gate rather than
a UI convention; the role and scope matrix; which signals are worth surfacing; and what to leave out.
Every generated fact was checked against the source pack — the LumenWorks agreement was re-read from the
PDF specifically to remove a section that an earlier draft had inferred rather than read.
