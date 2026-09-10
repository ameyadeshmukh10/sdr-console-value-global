# USAGE — SDR Pipeline & Analysis Commands (Value Global)

Quick reference for running everything yourself from the terminal. All commands assume you are
in the project root.

Everything is pure Python standard library — **no installs needed**. Secrets/config live in `.env`.

---

## 0. One-time config (`.env`)

| Variable | What it is | Status |
|---|---|---|
| `EMAILBISON_API_KEY` / `EMAILBISON_BASE_URL` | Bison token + instance (`send.everworker.ai`) | ✅ set |
| `BISON_CAMPAIGN_ID` | The Value Global Bison campaign (single-campaign routing) | ⬜ set once the VG campaign exists |
| `ENROLL_MONTHLY_CAP` | Monthly enrollment guardrail (client program: 1,500-3,000/month) | default `3000` |
| `HUBSPOT_*` | Dormant — the client has no CRM for this engagement | ⬜ unset |
| `HEYREACH_API_KEY` / `HEYREACH_BASE_URL` | HeyReach (LinkedIn) | ✅ set |
| `HEYREACH_CAMPAIGN_ID` / `HEYREACH_LINKEDIN_ACCOUNT_ID` | LinkedIn campaign + sender | ⬜ blank (LinkedIn deferred until the client's profiles are ready) |

**Personas** (`erp-owner` / `dba` / `data-governance` / `it-leadership`) share one uniform
message set; enrollment falls through to `BISON_CAMPAIGN_ID`. Load the client's do-not-contact
list (`suppression.py load`) before any send.

---

## 1. THE MAIN FLOW — batch outbound (recommended)

This batches your HubSpot contacts (25/batch), generates copy with parallel sub-agents, and enrolls
into Bison. State lives in SQLite (`data/outreach/pipeline.db`), so it's resumable.

### Easiest: the slash command (inside Claude Code)
```
/sdr-batches 2            # process 2 batches (50 contacts), DRY-RUN enroll (no writes)
/sdr-batches 2 enroll     # process 2 batches and LIVE-enroll into Bison
/sdr-batches all enroll   # process every pending batch and live-enroll
```
The slash command runs init, dispatches the `sdr-batch-runner` agents in parallel, then enrolls.

### Manual, step by step (terminal)
```bash
P=.claude/skills/sdr-pipeline/scripts

# (a) Refresh contacts from HubSpot list 2198  ->  data/outreach/contacts.jsonl
python3 $P/hubspot_pull.py

# (b) Load contacts into the batch DB (idempotent); makes 25-contact batches
python3 $P/sdr_batches.py init

# (c) See where things stand
python3 $P/sdr_batches.py status
python3 $P/sdr_batches.py pending-batches            # list pending batch ids

# (d) GENERATION happens via Claude sub-agents — use the slash command for this part,
#     or inspect a batch yourself:
python3 $P/sdr_batches.py get-batch 1                 # the 25 contacts in batch 1
#     (a sub-agent writes data/outreach/generated/<contact_id>.json, then:)
python3 $P/sdr_batches.py ingest 1                    # lint files + mark generated/failed
#     (ingest also mirrors signal + email 1 to the HubSpot contact property
#      sdr_signal_notes — best-effort; SDR_NOTES_HUBSPOT_WRITEBACK=0 disables)

# (e) Enroll everything marked "generated" into Bison (per-persona campaigns)
python3 $P/sdr_batches.py enroll --dry-run            # preview payloads, no writes
python3 $P/sdr_batches.py enroll                      # LIVE: create leads + attach to 10/11/12/13

# Fix-ups
python3 $P/sdr_batches.py reset-batch 7               # set batch 7 + its contacts back to pending
python3 $P/sdr_batches.py notes-backfill --dry-run    # preview signal+email-1 notes -> HubSpot contacts
python3 $P/sdr_batches.py notes-backfill              # LIVE one-time backfill of sdr_signal_notes
```

**Contact status:** `pending → generated → enrolled` (or `failed` with the lint reason).
Re-running only picks up unfinished work.

### The gated approval flow (console pulls + CSV uploads)

Pulls started from the **console** (Use view list pull, CSV upload) insert contacts
**gated** (`sdr_batches.py init --gated` / `csv_audience.py ingest --gated`) and stop at
two human gates — SLA-sourced pulls skip both and stay fully autonomous:

1. **Segment gate** — signal intelligence (tech + hiring + the five ERP news triggers)
   researches the pulled ACCOUNTS first; the Pipeline view shows them grouped by the
   signals found, and only approved segments continue. Approval batches the contacts
   (`assign_batches` skips gated contacts until then) and auto-starts generation —
   trigger segments write **trigger-anchored ERP Data Retirement copy** (the
   `erp-trigger` path in `generate_batch.py`: no web search, the stored verdict is the
   research).
2. **Outreach gate** — generated copy is reviewed on the Outreach tab (editable per
   touch; a human edit always wins and re-promotes a lint-failed contact) and must be
   approved before enrollment. `sdr_batches.py enroll` only touches approved or
   autonomous contacts and prints how many are still held at the gate.

There is no CLI for the approvals themselves (they are console actions:
`POST /api/segments/approve`, `POST /api/outreach/approve`); "Approve all" in the UI is
the escape hatch when a backlog should flow through un-reviewed.

**CSV audiences** — instead of (a), a contact CSV can feed the batch DB directly (the
console's Use view does this via `POST /api/audiences/upload`):
```bash
python3 $P/csv_audience.py ingest --file contacts.csv --name "Q3 event leads" --dry-run
python3 $P/csv_audience.py ingest --file contacts.csv --name "Q3 event leads"   # LIVE
python3 $P/csv_audience.py --self-test                # offline parser/mapping checks
```
Contacts get synthetic `csv-…` ids (not HubSpot ids — HubSpot write-backs skip them);
see CLAUDE.md "CSV audiences".

---

## 2. Single-shot / file-based outbound (no DB)

For a one-off or small set without the batch DB.
```bash
P=.claude/skills/sdr-pipeline/scripts

python3 $P/hubspot_pull.py            # pull list -> data/outreach/contacts.jsonl (US/tech ICP only)
# (generate copy per contact via the persona agents -> data/outreach/generated/<id>.json)
python3 $P/enroll.py --dry-run        # preview Bison payloads (per-persona routing)
python3 $P/enroll.py                  # live enroll; idempotent via data/outreach/enroll_state.json
```

**Lint any sequence markdown** (the guardrail check):
```bash
python3 .claude/skills/ai-sdr/scripts/lint_sequence.py .claude/skills/ai-sdr/examples/icp-email-sequence.md
```
Checks: 70–110 words, paragraph breaks, no sign-off, no em dashes, value-anchored **meeting** CTA,
step-4 breakup, a metric, no pricing, no undeliverable gives.

**Classify a job title** (ICP gate + persona routing):
```bash
echo "VP of Sales" | python3 .claude/skills/ai-sdr/scripts/buyer_group.py
```

---

## 3. Email Bison — pull data

```bash
B=.claude/skills/email-bison/scripts

python3 $B/fetch_interested_replies.py   # all "Interested" replies -> data/interested-replies/
python3 $B/fetch_campaign_stats.py       # campaign + per-step stats -> data/campaign-stats/
```

---

## 4. Analysis (reads the pulled data)

```bash
T=.claude/skills/interested-trends/scripts

python3 $T/analyze_interested.py     # descriptive features -> analysis/ (summary.json + CSVs)
python3 $T/analyze_conversion.py     # TRUE conversion rates by campaign/offer/geo/step
python3 $T/analyze_cohorts.py        # cohort (Marketing/Sales/CEO-Founder/Other) + qualitative prep
python3 $T/analyze_icp_cta.py        # ICP buyer-group filter + value-first-vs-time-ask CTA baseline
python3 $T/cohort_deepdive.py Sales  # verbatim evidence book for one cohort (Sales|Marketing|"CEO/Founder"|Other)
```
Outputs land in `data/interested-replies/analysis/` (reports: `trends-report.md`,
`conversion-report.md`, `cohort-playbook.md`, `sales-cohort-deepdive.md`, `icp-cta-report.md`).

---

## 5. AI SDR deal attribution (nightly HubSpot -> MongoDB sync)

Runs automatically at midnight US Eastern inside the deployed web server (needs
`MONGO_URL` — the Railway MongoDB service). Manual runs, from the console's Analytics
page ("Sync attribution") or the CLI:

```bash
P=.claude/skills/sdr-pipeline/scripts

python3 $P/aisdr_attribution_sync.py --json              # incremental (watermark) sync
python3 $P/aisdr_attribution_sync.py --json --dry-run    # compute, but no HubSpot writes
python3 $P/aisdr_attribution_sync.py --json --full       # re-scan all emails from scratch
```

Pulls every email engagement sent by `HUBSPOT_AISDR_FROM_EMAIL`, joins email -> contact ->
deals, snapshots into MongoDB (db `aisdr`: `emails` / `contacts` / `deals` / `sync_state`),
and sets `ai_sdr_deal_created=true` on deals created after the contact's first AI SDR email
(and on those contacts). Requires the `sales-email-read` scope on the HubSpot token.
Results feed the "Deals created by AI SDR" / "Total pipeline" tiles on the Analytics page
(`GET /api/analytics/aisdr`). See `CLAUDE.md` for the full design and gotchas.

---

## 6. Technographic signals (which tech an account runs)

Deterministic website + DNS scan plus **ERP portal probes** (no LLM, no API keys —
vendored `technographics/` engine) producing a line like `ERP: Oracle PeopleSoft`.
This console's default selection (`selection.erp.json`) detects ONLY the four
probe-enabled ERP suites — Oracle E-Business Suite, Oracle Fusion Cloud ERP,
PeopleSoft, JD Edwards; the template's marketing/sales coverage is out of scope for
Value Global (restore it via `TECH_SELECTION_FILE`). ERP suites never appear on the
marketing site, so the engine issues cheap static GETs against well-known portal
paths on named subdomains (e.g. `erp.<domain>/OA_HTML/AppsLogin`), with catch-all
false-positive guards (`TECH_PROBES=0` disables; `TECH_PROBE_TIMEOUT` default 4s).
Runs automatically with account research and after batch generation; results live on the
Signals view (Tech column, per-row **⌁ Detect**, bulk **Detect missing**) and are written
to the HubSpot company property `technographic_signals` (disable: `TECH_HUBSPOT_WRITEBACK=0`).
The copy playbook groups from the template (sequencing → email 2, intent/ABM or ad
pixels → email 3) remain wired but never fire under the ERP-only selection — the ERP
line reaches generation as background context. Manual runs:

```bash
P=.claude/skills/sdr-pipeline/scripts

python3 $P/tech_signals.py --domain acme.com             # scan one company (cached 90d; --force to re-scan)
python3 $P/tech_signals.py --missing --limit 50          # backfill accounts with no scan yet
python3 $P/tech_signals.py --self-test                   # offline fixture check (no network needed)
```

Add `--no-hubspot` to skip the property write-back; `--rendered` uses headless Chromium
for JS-heavy sites (Claude sessions only — the Railway image has no browser).

---

## 7. Hiring signals (is the account hiring sales/GTM roles?)

Prospeo job-postings lookup (needs `PROSPEO_API_KEY`; ONE credit per non-cached scan)
producing a line like `14 open roles · 4 sales: SDR; AE; VP Sales`. Runs automatically
with account research and after batch generation; results live on the Signals view
(Hiring column, drawer **⚑ Detect hiring**, bulk **Detect hiring**) and refresh the
HubSpot company properties `open_roles_count` / `hiring_signals_job_titles` /
`hiring_signals` (disable: `HIRING_HUBSPOT_WRITEBACK=0`). When open sales/GTM roles are
found, generation opens **email 2** on the hiring signal (email 1 keeps the researched
news signal; a tech sequencing play then shrinks to one supporting line). Manual runs:

```bash
P=.claude/skills/sdr-pipeline/scripts

python3 $P/hiring_signals.py --domain acme.com           # scan one company (cached 90d; --force to re-scan)
python3 $P/hiring_signals.py --missing --limit 50        # backfill accounts with no scan yet (credits!)
python3 $P/hiring_signals.py --self-test                 # offline check (no network, no key needed)
```

Add `--no-hubspot` to skip the property write-back. A company Prospeo can't match is
stored as "No open roles detected" with the error code in the drawer, and is not
re-scanned (or re-billed) until the refresh window lapses.

---

## 8. Company news signals (the five ERP buying triggers)

Web research per account (Anthropic API + server-side `web_search`; uses
`ANTHROPIC_API_KEY`) across the five Value Global ERP Data Retirement triggers —
**M&A carve-out** (last 90 days), **ERP migration** (Fusion Cloud / S/4HANA program),
**License audit** (Oracle audit exposure), **EBS on OCI** (lift-and-shift, still on
EBS), and **EBS performance** (runs ONLY when the tech scan detected Oracle
E-Business Suite). Each trigger is one web-search call returning a scored JSON
verdict (found, score 0-100, headline, summary, date, source URL); the found
triggers become a line like `M&A carve-out 85: Acme completes carve-out of FooCo`.
Results live on the Signals view (News column, drawer **⌕ Research news** with the
per-trigger breakdown, bulk **Research news**), are cached for `NEWS_REFRESH_DAYS`
(default 30), refresh as a tail after batch generation (`NEWS_DETECT_ENABLED=0`
disables), and are written to the HubSpot company property `erp_news_signals`
(disable: `NEWS_HUBSPOT_WRITEBACK=0`). Manual runs:

```bash
P=.claude/skills/sdr-pipeline/scripts

python3 $P/news_signals.py --domain acme.com             # research one company (cached 30d; --force to re-run)
python3 $P/news_signals.py --missing --limit 20          # backfill accounts with no research yet ($ per scan!)
python3 $P/news_signals.py --missing --sync              # force the synchronous path (skips the 50% batch discount)
python3 $P/news_signals.py --domain acme.com --triggers ma_carveout,erp_migration   # scope the triggers
python3 $P/news_signals.py --self-test                   # offline check (no network, no key needed)
python3 $P/news_signals.py --refloor --dry-run           # preview guard corrections to stored verdicts
python3 $P/news_signals.py --refloor                     # apply them (DB-only; freshness clocks preserved)
python3 $P/news_signals.py --recompose --limit 50        # composites for stored found rows ($ per row)
```

Bulk backfills (`--missing`, the UI bulk button, the intel job) run through the
**Message Batches API** by default — 50% token cost, results usually within the hour
(`NEWS_BATCH=0` disables). A full scan is 3-4 web-search calls per trigger (per-trigger
caps tuned from live data; `NEWS_MAX_SEARCHES` overrides) — prefer `--limit` on a
first bulk backfill, or scope with `--triggers` / `NEWS_TRIGGERS`. The model comes
from `NEWS_MODEL` (set to `claude-sonnet-5` in prod — unset it falls through to the
pricey `claude-opus-4-8` client default), with per-trigger overrides via
`NEWS_MODEL_<TRIGGER_ID>` (e.g. `NEWS_MODEL_LICENSE_AUDIT=claude-haiku-4-5`).
When a scan finds ≥1 trigger, a composite best-angle `signal` is synthesized into the
account's top-line Signal field (`NEWS_COMPOSITE_SIGNAL=0` disables).
A scan only stores NULL + `news_error` (and retries next touch) when EVERY trigger
call failed; partial results are kept and reused like any other.

---

## Where things live

```
skills/                         <- visible shortcut into .claude/skills (browse the code/knowledge)
data/
├─ interested-replies/          <- pulled replies + analysis/ reports
├─ campaign-stats/              <- campaign + step stats
└─ outreach/
   ├─ contacts.jsonl            <- pulled ICP contacts
   ├─ generated/<id>.json       <- generated copy per contact
   ├─ pipeline.db               <- SQLite batch state (status per contact/batch)
   └─ enroll_state.json         <- file-flow idempotency
.env                            <- keys + config
```
The product knowledge the agents write from: `skills/ai-sdr/knowledge/` (`offer.md`, `cta-offers.md`,
`icp-email.md`).

---

## Troubleshooting

- **`pending-batches` is empty but you expected batches** — run `sdr_batches.py init` (loads new
  contacts into batches). Re-run `hubspot_pull.py` first if the list changed.
- **`enroll` says `no_campaign`** — the per-persona `BISON_CAMPAIGN_*` ids aren't set in `.env`.
- **A batch had failures** — `status` shows `failed` counts; the per-contact `error` (lint reason) is
  in the DB. Re-generate those, then `ingest <id>` again, or `reset-batch <id>` to redo the whole batch.
- **New `/sdr-batches` command or `sdr-batch-runner` agent not found** — reload the Claude Code session.
- **Blank line before the email signature** — set this in Bison (prepend a blank line to the sender
  signature); Bison strips trailing whitespace from the lead body, so it can't come from the copy.
- **Bison/HubSpot/HeyReach 401** — token/instance mismatch; check the matching `*_API_KEY` and
  `*_BASE_URL` in `.env`.
- **Always dry-run before a live enroll** (`enroll --dry-run`) to eyeball routing + payloads.
```

---

## Replies management (webui → Replies)

The Replies view is a two-pane inbox over both reply channels (Bison email + HeyReach
LinkedIn). Left: the message list — **Interested** (draft & send), **Possible interested**
(review), **Other** (not interested / low confidence), **Done & dismissed**. Right: the full
conversation thread (outbound sequence, the reply, and any console-sent follow-ups, merged
chronologically) plus the actions for that reply.

- **Dismiss** ("handled in CRM") clears a lead from the queue without sending anything —
  for leads you've already replied to / booked from HubSpot. Dismissals persist across
  rescans (`data/interested-replies/reply_state.json`, keyed per lead) and auto-expire the
  moment the lead sends a **new** reply.
- **Reclassify as interested** promotes a misclassified "Other" reply: it's enriched on
  demand, flipped to interested (the model's original verdict is kept for audit), tagged in
  Bison, and moved to the Interested queue. The override survives rescans.
- **Reply agents** — each interested reply has an agent dropdown + Regenerate:
  - **Standard**: the playbook-grounded drafter (`draft_followups.py`).
  - **Signal Playbook**: builds a personalized signal play for the lead's company
    (research → deck-data → single-file HTML via `deck-renderer/`), publishes it as a
    LIVE HubSpot website page at `everworker.ai/signal-plays/<company>-ai-sdr-playbook`
    (`content` scope required; instant publish via draft → push-live, same URL on
    rebuilds), and drafts a reply embedding the page link. Runs as a background job with
    stage progress; artifacts land in `data/signal-plays/<slug>/`. Falls back to a
    standard draft if the build fails.
- **HubSpot logging is automatic** — an hourly background loop logs replies in/out (and the
  outbound sequence every 12th cycle). There is no manual button. The toolbar shows
  `Last scanned` plus a red dot if the last auto-log failed.

### HubSpot duplicate-logging runbook

If contacts show the same email logged multiple times:

1. **Audit (read-only):** `python3 .claude/skills/sdr-pipeline/scripts/hubspot_activity_audit.py --sample 5`
   (or `--contact-id <id>`; also `GET /api/hubspot/activity/audit`). Each duplicate cluster
   is labeled:
   - `overlapping_loggers` — our sync AND another logger (HubSpot native inbox/BCC logging,
     or HeyReach's native sync + our webhook drain for LinkedIn) both log the same email.
     Fix: turn one off (e.g. disconnect native logging for the SDR aliases, or set
     `HEYREACH_ACTIVITY_AUTOSYNC=0`).
   - `ledger_loss_or_manual` — engagements re-created after the dedup ledger was wiped
     (non-durable volume) or logged by a human.
   - `dedup_bug` — duplicates that are all ours; report it.
2. **Guard:** with an empty ledger, a live sync that would create more than
   `HUBSPOT_ACTIVITY_FRESH_MAX` (default 50) engagements refuses and reports instead.
3. **Recover a wiped ledger:** `python3 .claude/skills/sdr-pipeline/scripts/hubspot_activity_sync.py --reconcile-from-hubspot`
   — adopts existing engagements (matched by contact + timestamp) into the ledger instead of
   re-creating them.

### Data durability (Railway)

Everything the console records lives under `/app/data` — attach a **Railway Volume** mounted
at `/app/data` (service → Settings → Volumes) or it resets on every redeploy. The app checks
this at boot (`/api/system/status`, entrypoint boot marker) and shows a warning banner when
the data dir looks non-durable.
