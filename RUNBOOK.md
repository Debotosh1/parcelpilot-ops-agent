# End-to-End Runbook (Windows-first)

Everything from "I have the zip" to "the submission form is filled in". Roughly 30 minutes, most of it
the video.

---

## Step 1 — Get a Groq API key (2 min, free)

1. Go to <https://console.groq.com> and sign in with Google.
2. **API Keys** → **Create API Key** → name it `parcelpilot` → copy the value (starts with `gsk_`).
   It is shown once; paste it somewhere safe now.

Free tier is plenty — a full demo run is a few thousand tokens.

## Step 2 — Run it locally

Open **Command Prompt** (or Anaconda Prompt) and unzip the project somewhere simple, e.g.
`C:\Users\debot\parcelpilot-ops-agent`.

```bat
cd C:\Users\debot\parcelpilot-ops-agent

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt

copy .env.example .env
notepad .env
```

In Notepad, replace `gsk_your_key_here` with your real key, save, close. Then:

```bat
uvicorn app.main:app --reload
```

Open <http://localhost:8000>. You should see the header showing
`snapshot 2026-08-16 11:00 Asia/Kolkata` and `groq · llama-3.3-70b-versatile`, with no orange warning
bar. An orange bar means the key was not picked up — check `.env` is in the project root and restart.

Stop the server with `Ctrl+C`. To start it again later: `cd` in, `.venv\Scripts\activate`, `uvicorn app.main:app --reload`.

**If PowerShell blocks activation:** run `Set-ExecutionPolicy -Scope Process RemoteSigned` first, or
just use Command Prompt.
**If port 8000 is busy:** `uvicorn app.main:app --port 8001`.

## Step 3 — Prove it works (5 min)

Run the tests first — they need no key and no network:

```bat
pytest -q
```

Expect `79 passed`.

Then walk the app in this order (this is also the demo order):

| # | As | Ask / do | What proves what |
| --- | --- | --- | --- |
| 1 | Rohit | *Can Northstar cancel ORD-1001 without a cancellation fee? Explain why.* | multi-step tools; contract beats SOP; citations |
| 2 | Rohit | *A pickup is three hours late because of carrier fault. Should I get a service credit?* | default SOP path, flags that a contract could change it |
| 3 | Rohit | *What if that customer is LumenWorks?* | 4-hour contract threshold → not eligible |
| 4 | Rohit | *What's happening with LumenWorks order ORD-2001?* | red `denied` chip — access control |
| 5 | Maya | same question again | she owns ACCT-002, so she gets the answer |
| 6 | Rohit | *What should I do about TKT-505?* → click **Confirm & execute** | P1 + confirmation gate |
| 7 | Priya | open **Ops signals**, then **Audit log** | proactive detection + full audit trail |

If an answer ever looks wrong, open the **tool trace** under it — the raw evaluator output is there, and
that is the number the answer must match.

## Step 4 — Push to GitHub

The zip already contains a git repo with one commit, so:

```bat
git remote add origin https://github.com/<your-username>/parcelpilot-ops-agent.git
git branch -M main
git push -u origin main
```

Create the empty repo on GitHub first (**public**, no README/licence — the repo already has them).
`.env` is git-ignored, so your key cannot leak.

## Step 5 — Deploy (10 min, free)

**Render** (matches the included `render.yaml`):

1. <https://render.com> → sign in with GitHub.
2. **New** → **Blueprint** → pick the repo → Apply.
3. It asks for `GROQ_API_KEY` — paste it. Everything else is preconfigured.
4. Wait for the build, then open the `.onrender.com` URL and re-run one question from Step 3.

Free instances sleep after ~15 minutes idle, so the first hit takes ~30s. **Open the URL a minute
before you record or before a reviewer looks at it.** Put that URL in the README's first line and in the
submission form.

*Alternative:* Railway (New → Deploy from GitHub → add `GROQ_API_KEY`; it detects the Dockerfile) or any
host that runs `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

## Step 6 — Record the 5-minute video

Follow `DEMO_SCRIPT.md` — it is timed section by section with the exact prompts and the lines to say.

- Record with the **hosted** URL if it is up, otherwise localhost is fine.
- Windows: Xbox Game Bar (`Win+G`) records the browser window; OBS or Loom also work.
- Browser zoom ~90% so the tool trace is readable; close other tabs.
- Do one silent rehearsal so the model responses are warm and you know the timings.
- Upload to Google Drive or YouTube (unlisted) and **set sharing to "anyone with the link"** — this is
  the single most common submission mistake.

## Step 7 — Submit

Fill in <https://forms.gle/hLGBrDrNRmK7UAbv6> with:

- **Repository:** your public GitHub URL
- **Hosted app:** the Render URL
- **Demo video:** the link (check it in an incognito window first)
- **Notes:** point at `ARCHITECTURE.md`, `PRODUCT_NOTE.md` and `AI_TOOL_USAGE.md` in the repo root

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Orange bar: "GROQ_API_KEY is not set" | `.env` missing/misplaced, or server not restarted after editing it |
| Chat replies "I could not reach the language model" | Bad or revoked key, no internet, or Groq rate limit — the trace shows the error |
| `ModuleNotFoundError: fastapi` | Virtualenv not activated (`.venv\Scripts\activate`) or `pip install -r requirements.txt` not run |
| `429` from Groq | Free-tier rate limit; wait a minute, or set `GROQ_MODEL=llama-3.1-8b-instant` in `.env` |
| Answers look stale after editing a CSV | `--reload` picks up code, not data — restart the server |
| Render build fails | Check `PYTHON_VERSION` is `3.11.9` in the service's env vars |

## Resetting between demo takes

State (escalations, confirmed actions, audit log) is in memory, so **restarting the server resets
everything** to a clean snapshot. Locally: `Ctrl+C`, then `uvicorn app.main:app --reload`. On Render:
**Manual Deploy** → **Restart service**.
