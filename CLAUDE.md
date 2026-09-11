# CLAUDE.md — project context for Claude sessions

Read this first. It captures the architecture, deployment topology, and operational
facts that aren't obvious from the code. Companion docs: `README.md` (overview),
`USAGE.md` (CLI runbook), `webui/README.md` (console), `.env.example` (every env var,
documented), `.claude/skills/*/SKILL.md` (pipeline logic).

## What this is

Autonomous outbound pipeline + web console configured for **Value Global's ERP Data
Retirement service** (Oracle EBS archiving/retirement on the InfoCorvus ROAD platform).
It ingests ICP contacts (CSV audiences primarily — the client has no CRM; HubSpot paths
are dormant), runs signal intelligence (ERP technographics + the five ROAD triggers),
generates read-first outreach with Claude, and enrolls into Email Bison (email) +
HeyReach (LinkedIn, deferred).

## Value Global configuration (2026-09, from the kick-off)

- **Buyer group:** erp-owner / dba / data-governance / it-leadership
  (`ai-sdr/scripts/buyer_group.py`); GTM titles, CEOs, procurement are NOT-ICP.
  Messaging is uniform across personas by client decision.
- **Suppression (blockers before any send):** the client's account do-not-contact list
  (`sdr-pipeline/scripts/suppression.py`, `suppression_accounts` table, seeded from
  `data/outreach/suppression_seed.csv` — a DRAFT pending client sign-off) and the
  **Fusion-only rule** (Fusion detected with no on-prem ERP → suppress; Fusion + EBS
  co-detection = mid-migration, keep). Both enforced at CSV ingest, the segment gate
  (`suppressed` segment), and enrollment.
- **ERP mention rule:** detected EBS/PeopleSoft/JDE is NAMED in copy (pattern + question,
  never "we scanned you") — `erp_mention_block()` in `generate_batch.py`.
- **Lead quality:** contacts carry country/industry/employees/import_flags; imports FLAG
  (never drop) non-US/CA, healthcare, sub-1,000 headcount, and missing LinkedIn URLs.
- **Copy system:** knowledge base + examples + agents are Value Global content; the
  linter (`ai-sdr/scripts/lint_sequence.py`) enforces the client ban list, claim
  discipline (licensing = cores/users, not data volume), standalone subjects (each touch
  its own email, no RE: threading), and the read-first offer ladder. Volume guardrail:
  `ENROLL_MONTHLY_CAP` (default 3000).

## Architecture (verified, don't re-derive)

- **One deployed process.** `webui/server/app.py` (~2,800 lines, Python 3.12) serves BOTH
  the JSON API and the built React SPA via `ThreadingHTTPServer`. Routing is a manual
  `if path == "/api/...":` dispatch in `do_GET` / `do_POST`. Every `/api/*` route is
  auth-gated (Bearer token from `/api/login`) unless listed in `_EXEMPT_GET`/`_EXEMPT_POST`.
- **Backend deps:** stdlib only, EXCEPT `requirements.txt` (`pymongo` for the attribution
  store, `dnspython` for technographic detection). Anything importing either must do it
  lazily (see `mongo_store.py`, `tech_signals.py`) so the server still boots without them.
- **Frontend:** React 18 + Vite 5 + Recharts in `webui/frontend/`. API wrappers live in
  `src/api.js`; shared stat tiles / spinners in `src/components/ui.jsx`.
- **Convention:** heavy/write work shells out via `run_script()` to
  `.claude/skills/*/scripts/*.py` (each script prints progress lines, then a JSON summary
  as the LAST stdout line — callers parse that). Read endpoints are in-process
  `*_payload()` functions that must degrade gracefully, never 500.
- **Pipeline scripts** live in `.claude/skills/sdr-pipeline/scripts/`. `hubspot_client.py`
  is the shared stdlib HubSpot client (retry/backoff, `.env` autoload) — extend it, don't
  write new HTTP code.

## Deployment (Railway)

- Railway project has the **sdr-console service** (Docker build from this repo's
  `Dockerfile`; deploys on push to `main`) and a **MongoDB service**.
- **Railway Volume** mounted at `/app/data` = the LIVE data dir. The committed `data/` is
  only a first-boot seed (`docker-entrypoint.sh` copies it in when `pipeline.db` is
  absent). **Prod data on the volume is far ahead of the repo snapshot** — never assume
  repo `data/` reflects production.
- **MongoDB** is wired via `MONGO_URL=${{MongoDB.MONGO_URL}}` (reference variable on the
  sdr-console service, private networking, same project/environment). Database `aisdr`.
- The **Railway CLI is NOT available in cloud Claude sessions** (it's on the user's local
  machine). For Railway dashboard changes (variables, services), either give the user
  click-by-click instructions or ask for a project token. Code changes ship via GitHub →
  merge to `main` → Railway auto-deploys.
- Env vars live in Railway service variables in prod, `.env` locally. `.env.example`
  documents all of them.

## Data stores

| Store | Where | What |
|---|---|---|
| SQLite `data/outreach/pipeline.db` | volume | contacts, batches, signals cache (`account_signals`, incl. the technographic `tech_*`, hiring `hiring_*`, and ERP-trigger news `news_*` columns), `hubspot_activity_log` (engagement-logging ledger), `bison_lead_map`, `heyreach_events` inbox, `unenrollment_log` (suppression ledger). Schema: `scripts/batch_db.py`. The web server opens it READ-ONLY; writes go through pipeline scripts (and their in-process module calls, e.g. signal refresh / tech + hiring + news detect). |
| JSONL under `data/` | volume | campaign stats, generated outreach copy, interested-reply threads/analysis. |
| MongoDB db `aisdr` | Railway MongoDB service | AI SDR deal attribution: `emails`, `contacts`, `deals`, `sync_state` (see below). Accessed only through `scripts/mongo_store.py`. |

## AI SDR deal attribution (added 2026-07, PR #18)

Nightly job answering "which HubSpot deals did the AI SDR create, and what are they worth?"

- **Flow:** the pipeline logs every AI SDR email to HubSpot as an `emails` engagement
  with header From = `HUBSPOT_AISDR_FROM_EMAIL` (`ai-sdr@everworker.ai`). The sync
  (`scripts/aisdr_attribution_sync.py`) searches those engagements (subject/body/date in
  the search response), resolves email→contact and contact→deal associations in batches,
  snapshots everything into Mongo, computes attribution, and writes
  `ai_sdr_deal_created=true` back to HubSpot deals + contacts (only where not already
  true; never un-sets).
- **Attribution rule (user-approved):** a deal counts iff it's associated with an
  AI-SDR-emailed contact AND `deal.createdate >= that contact's first AI SDR email`.
  "Total pipeline" = sum of `amount` over ALL flagged deals incl. closed lost.
- **Scheduling:** `_aisdr_sync_loop()` daemon thread in `app.py`, fires at
  `AISDR_SYNC_HOUR` (default 0 = midnight) America/New_York, DST-safe. First run against
  empty Mongo = full seed; after that incremental via `sync_state.watermark_ms`
  (10-min overlap; upserts by engagement id = idempotent). Contact→deal associations and
  deal snapshots are re-swept EVERY run so late-created deals get attributed and
  stages/amounts stay fresh.
- **Endpoints:** `GET /api/analytics/aisdr` (tiles), `GET /api/hubspot/aisdr/status`,
  `POST /api/hubspot/aisdr/sync` (`{full?, dry_run?}`, background thread, 409 if running).
  UI: top of AnalyticsPage — two accent tiles + "Sync attribution" button.
- **CLI:** `python3 .claude/skills/sdr-pipeline/scripts/aisdr_attribution_sync.py
  [--json] [--dry-run] [--full] [--limit N]`.
- **Operational facts (from the 2026-07-09 seed):** 18,971 AI SDR email engagements,
  5,039 contacts emailed, 9 deals / $282,000 attributed. The seed takes ~15 min at this
  volume; incremental runs take seconds. HubSpot's raw `ai_sdr_deal_created=true` deal
  count (47) is HIGHER than the tiles (9) because ~38 deals carried pre-existing manual
  flags — the tiles show the strict computed attribution from Mongo; the sync preserves
  the manual flags.
- **Gotchas:** reading email engagements requires the **`sales-email-read`** scope on the
  HubSpot private app token (preflight surfaces a clear `missing_scope` error to the UI).
  HubSpot CRM search caps at 10,000 results — the sync windows past it by restarting the
  `hs_timestamp GT` filter (do the same in any new search-based code). `amount` is
  portal-currency, assumed single-currency (USD formatting).

## Technographic detection (added 2026-07; ERP reconfiguration 2026-09)

Deterministic scan of which tech an account runs — no LLM, no third-party API.
**This console is configured for Value Global: it detects ONLY the four probe-enabled
ERP suites** (Oracle E-Business Suite, Oracle Fusion Cloud ERP, PeopleSoft, JD Edwards).
The template's marketing/sales coverage (CRM / ad pixels / martech / salestech) is out
of scope here — the catalogue still carries it, restore via `TECH_SELECTION_FILE`.

- **Engine:** vendored at root `technographics/` (from the `technographic-signals` repo —
  provenance + re-sync in `technographics/VENDORED.md`). DNS fingerprinting (dnspython,
  resolvers 1.1.1.1/8.8.8.8: MX/TXT/NS/A/SOA + CNAME subdomain probes) + static-HTML
  fingerprinting + **ERP portal probes** (`subdomain_prober.py`: ERP suites never appear
  on the marketing site, so vendors declaring `subdomains_to_probe` + `probe_paths` get
  cheap static GETs at `https://<sub>.<domain><path>`, e.g. `erp.acme.com/OA_HTML/
  AppsLogin`, matched against ONLY that vendor's signature), all against a
  Wappalyzer-derived catalogue (7.5k vendors), scoped by `TECH_SELECTION_FILE`
  (default: `selection.erp.json` — the 4 ERP suites).
- **Runner:** `.claude/skills/sdr-pipeline/scripts/tech_signals.py` (module + CLI). Both
  fetchers are stdlib urllib (NO requests/bs4/httpx — the probe step injects a urllib
  `fetcher` into `probe_subdomains()` so the vendored default, which lazily imports
  httpx, never runs); **NO Playwright in the Railway image** — `--rendered` exists for
  Claude sessions only (Chromium preinstalled there). Probe knobs: `TECH_PROBES=0`
  disables the step, `TECH_PROBE_TIMEOUT` (default 4.0 s) bounds each GET; budget is 8
  probes/vendor path-major, ≤ ~27 GETs per scan at concurrency 8.
- **When it runs:** (1) inline in `generate_batch.py` on a research cache miss (under the
  per-domain lock, before copy is written) and after a UI signal refresh; (2) Signals view
  per-row "Detect" + bulk "Detect missing" (`POST /api/signals/tech/detect`,
  `POST /api/signals/tech/backfill` + `GET /api/signals/tech/status/<id>`); (3) a
  fire-and-forget tail after a Message-Batches job completes; (4) `build_play.py` scans the
  prospect pre-research and the play TARGET post-research (appended as a
  "6c-verified" block in research.md).
- **Storage:** `account_signals.tech_signals` (formatted line, e.g. `"ERP: Oracle
  PeopleSoft"`, or the literal `"No signals detected"`; NULL = scan itself failed),
  `tech_detail` (detections JSON — probe hits carry `source:"probe"`; also
  `probe_error`), `tech_checked_at` (reused for `TECH_REFRESH_DAYS`, default 90),
  `tech_error`.
- **Consumers:** generation prompts get the line as background context (reference ONE
  relevant tool max, never list the stack). The template's **playbook plays**
  (`playbook_groups()` / `PLAYBOOK_*` sets, `_cached_tech()` → `(line, playbook)`,
  sequencing → EMAIL 2, intent/ABM or ads → EMAIL 3) are still wired end-to-end but
  **never fire under the ERP-only selection** — all groups come back empty and the ERP
  line rides along as plain background. HubSpot write-back PATCHes the
  `technographic_signals` company property (best-effort — company matched by `domain`;
  `TECH_HUBSPOT_WRITEBACK=0` kills it; needs company read/write + schema scopes on the
  token, otherwise it logs and moves on).
- **Gotchas:** every import of `tech_signals`/dnspython must stay lazy (boot rule above).
  A scan only counts as failed when BOTH primary channels died (fetch error AND zero DNS
  records) AND no probe detection rescued it — never store a network-dead run as
  "No signals detected". Probe false-positive guards (in the vendored prober — don't
  re-implement): 4xx/5xx responses dropped; a match whose only evidence is the probe URL
  we constructed is discarded unless the server organically redirected OFF the probed
  site (e.g. `erp.bk.rw` → `*.fa.ocs.oraclecloud.com` = genuine Fusion evidence;
  a bounce back to apex/www is not). Probe TLS is deliberately unverified (on-prem
  portals run self-signed certs). `--self-test` runs offline (works without
  dnspython/network) and asserts the ERP config: 4 vendors, probe specs, per-suite
  fingerprints, catch-all guards, and that the marketing fixtures no longer match.

## Hiring signals (added 2026-07)

Per-account job-postings scan — is the company hiring, and specifically for sales/GTM
roles? Ported from the `hubspot-hiring-signals` repo as a **stdlib rewrite** (urllib +
hand-rolled retry; NO requests/tenacity/pyyaml — zero new pip deps).

- **Engine:** `.claude/skills/sdr-pipeline/scripts/hiring_signals.py` (module + CLI).
  One Prospeo `enrich-company` call per domain (`PROSPEO_API_KEY`, **one credit per
  non-cached scan**) → `job_postings.active_count/active_titles`; the sales/GTM subset
  comes from the regex taxonomy ported verbatim from that repo's `config.yaml`
  (exclude beats include).
- **Storage** (`account_signals.hiring_*`, semantics mirror tech): `hiring_signals` =
  formatted line (`"14 open roles · 4 sales: SDR; AE; VP Sales"`) or the literal
  `"No open roles detected"`; NULL + `hiring_error` = the scan itself failed (retries
  next touch). Prospeo's definitive non-matches (NO_MATCH/NO_RESULT/NO_IDENTIFIER/
  INVALID_DATAPOINT) store the literal + `error_code` in `hiring_detail` — a credit
  guard so unmatchable domains aren't re-billed every touch. `hiring_checked_at` drives
  `HIRING_REFRESH_DAYS` (default 90).
- **Rate-limit gotcha (load-bearing):** Prospeo signals throttling as **HTTP 200 +
  `{"error":true,"error_code":"Rate limit exceeded"}`** — `_prospeo_request` retries it
  like a 429 (Retry-After honored, capped 16s; 5 attempts, expo backoff) and must never
  store it as a result. Unknown error codes classify as transport failures (retry).
- **When it runs:** same three hooks as tech — (1) inline in `generate_batch.py` on a
  research cache miss + after a UI signal refresh; (2) Signals view (drawer "⚑ Detect
  hiring", bulk "Detect hiring": `POST /api/signals/hiring/detect`,
  `POST /api/signals/hiring/backfill` + `GET /api/signals/hiring/status/<id>`, separate
  `HIRING_JOBS` registry); (3) fire-and-forget tail after Message-Batches (separate
  thread from the tech tail).
- **Copy consumer (the rule both engines carry):** when the sales subset is non-empty,
  `_cached_hiring()` feeds a compact line into the prompt and **email 2 opens on it**
  (count + 1-2 roles, tied to covering pipeline while the new reps ramp); email 1 keeps
  the researched news signal; skip if email 1 already covers hiring; never name the data
  source or claim postings are new. When a tech sequencing play is also present, hiring
  still opens email 2 and the sequencing point shrinks to one supporting line. Matching
  wording lives in: `generate_batch.py` (`build_user` + earn/show WRITE_RULES),
  `icp-email.md` 4-touch table, `cta-offers.md` cadence + Tier lists, `offer.md` (Memgraph
  signal-activation story + 2-5x infra claim), the gold example
  (`examples/icp-email-sequence.md`), `sdr-batch-runner.md`, and the 4 persona agents —
  `grep -rn "email 2\|EMAIL 2\|email 3\|EMAIL 3\|signal-mapping\|run[- ]rate"` over those
  files is the drift checklist. Counts with NO sales roles are not a hook.
- **HubSpot write-back:** refreshes the SAME three company properties the
  hubspot-hiring-signals job maintains — `open_roles_count` (str int),
  `hiring_signals_job_titles` (`<br>`-joined HTML, ALL titles), `hiring_signals`
  (`"; "`-joined **sales subset** — NOT the display line). Matched-with-0-postings
  clears stale values; non-matches skip. Best-effort by domain;
  `HIRING_HUBSPOT_WRITEBACK=0` kills it.
- **Gotchas:** keep every `import hiring_signals` lazy (boot rule); the server must boot
  with `PROSPEO_API_KEY` unset (endpoints return `hiring_available:false`, detect → 501).
  Run a first bulk backfill with `--limit` — every non-skipped scan is a credit.
  `--self-test` is offline (no key/network/DB).

## Company news signals — the five ERP triggers (added 2026-09)

Per-account web research across the five Value Global ERP Data Retirement buying
triggers, via the Anthropic Messages API + server-side `web_search` (the same channel
`generate_batch.py` researches with — `ANTHROPIC_API_KEY`, no new deps).

- **Engine:** `.claude/skills/sdr-pipeline/scripts/news_signals.py` (module + CLI;
  offline `--self-test`). Triggers, canonical order: `ma_carveout` (acquisition/
  divestiture/carve-out, strict 90-day window; divestiture scores hottest),
  `erp_migration` (Fusion Cloud / S/4HANA program; mid-implementation is the sweet
  spot), `license_audit` (composite Oracle-audit-exposure proxies; cross-references
  ma_carveout's verdict), `ebs_oci` (EBS lifted onto OCI and STILL on EBS — OCI ≠
  Fusion; cross-references erp_migration so a full re-platform isn't double-counted),
  `ebs_performance` (month-end-close pain proxies, score capped at 75; **pre-condition:
  runs ONLY when the tech scan detected `oracle_ebs`** — otherwise recorded as skipped,
  and re-evaluated on the next refresh once a tech scan lands). One Messages call per
  trigger, run in two waves so cross-referenced triggers see their upstream
  verdicts: wave 1 = ma_carveout + erp_migration + ebs_performance (concurrent),
  wave 2 = license_audit + ebs_oci. Each call returns strict JSON
  `{found, score 0-100, headline, summary, date, source_url, details}`; verdicts are
  clamped/normalized (`classify_verdict`, incl. per-call token `usage`) and a bad
  verdict never crashes the scan. `license_audit` additionally carries a deterministic
  **found floor of 55** (`min_found_score`) — the 2026-09 live run showed 105/123 of
  its found verdicts in the weak 30-54 one-proxy band, pure segment filler.
- **Model resolution (2026-09 cost lesson, load-bearing):** per trigger,
  `NEWS_MODEL_<TRIGGER_ID>` env → `TRIGGERS[..]["model"]` → `NEWS_MODEL` →
  `CLAUDE_MODEL` → the client default `claude-opus-4-8`. **The first full run burned
  Opus 4.8 rates ($5/$25 per MTok) on all 1,262 accounts because nothing was set** —
  `NEWS_MODEL=claude-sonnet-5` ($2/$10) is now a Railway service variable (2026-09-10).
  Test cheaper tiers per trigger without code changes, e.g.
  `NEWS_MODEL_LICENSE_AUDIT=claude-haiku-4-5`; `NEWS_COMPOSITE_MODEL` scopes the
  composite-signal call. `_client()` keeps one cached client per model.
- **Search caps (live-tuned 2026-09):** per-trigger `max_searches` — ma_carveout /
  license_audit / ebs_oci / ebs_performance 3, erp_migration 4. From the 1,262-account
  run: found verdicts needed a 4th search only on erp_migration (median 4); ma_carveout
  found at median 2, all 8 ebs_oci finds at ≤3. `NEWS_MAX_SEARCHES` (when set)
  overrides every cap. Web search bills ~$10/1k calls PLUS each result's tokens.
- **Storage** (`account_signals.news_*`, semantics mirror tech/hiring): `news_signals`
  = found triggers score-desc as `"M&A carve-out 85: <headline> · ERP migration 70: …"`
  or the literal `"No ERP news signals detected"`; NULL + `news_error` ONLY when EVERY
  researched trigger errored (retries next touch) — a partial scan stores its
  successes and counts as definitive. `news_detail` = per-trigger verdicts JSON (incl.
  skipped reasons, per-trigger errors, searches, model, HubSpot outcome).
  `news_checked_at` drives `NEWS_REFRESH_DAYS` (**default 30**, shorter than
  tech/hiring because the trigger windows are 90-day).
- **When it runs:** (1) Signals view — drawer "⌕ Research news" (in-process, blocks
  1-3 min behind a spinner) + bulk "Research news" (`POST /api/signals/news/detect`,
  `POST /api/signals/news/backfill` + `GET /api/signals/news/status/<id>`, separate
  `NEWS_JOBS` registry, workers=2); (2) fire-and-forget tail after a Message-Batches
  job completes (`NEWS_DETECT_ENABLED=0` kills it); (3) CLI (`--missing --limit N`,
  `--triggers a,b` scoping; `NEWS_TRIGGERS` env scopes everywhere). Deliberately NOT
  inline under `generate_batch`'s per-domain lock — a scan takes minutes, not seconds.
- **HubSpot write-back:** found-trigger lines (with score/date/source URL) PATCH the
  company property `erp_news_signals` (auto-ensured, textarea; matched by `domain`;
  best-effort; `NEWS_HUBSPOT_WRITEBACK=0` kills it).
- **Composite signal (2026-09):** when a scan FOUND ≥1 trigger, `detect_and_store` makes
  one extra no-web-search call (`compose_signal`) that synthesizes the verdicts + tech/
  hiring context (re-read fresh post-scan) into the account's top-line `signal`. Stored
  via **`upsert_composite_signal` — fill-only**: a fresh (<90d) generation-researched
  signal always wins, `company_name` is never touched, `has_recent` derives from
  non-proxy triggers. **Deliberate consequence:** the fresh `researched_at` makes
  `generate_batch`'s default/SLA path reuse the composite as cached research (no new
  web search) for 90 days — the composite prompt therefore writes neutral factual
  research prose, never SDR meta-commentary; the erp-trigger path is unaffected.
  Failures/empty outputs are stderr-logged and recorded in `news_detail.composite`
  ({ok, stored, error}); a max_tokens-truncated output is never stored;
  `NEWS_COMPOSITE_SIGNAL=0` kills it. Both prompts anchor today's date (UTC — matches
  the stored clocks) and forbid adopting a stale source's tense (a past "projected
  go-live" is a completed event — the 2026-09 date-reasoning fix).
- **Verdict guards (`_apply_verdict_guards`):** deterministic backstops after
  `classify_verdict`, sync and batch and stored-data alike — license_audit found
  floor 55, ebs_performance score cap 75, ma_carveout 90-day recency window (month
  granularity). Downgrades keep the evidence in `details` (`below_found_bar` /
  `outside_window`) with only a short summary marker. **Migrations for
  already-stored rows:** `news_signals.py --refloor` (DB-only, preserves freshness
  clocks) and `--recompose` (composites for stored found rows; API spend, use
  `--limit`).
- **Batched backfills (2026-09, opt-in):** the deliberate bulk paths — the UI bulk
  "Research news" button and CLI `--missing` — go through the **Message Batches
  API** (50% token cost, search fees unchanged) via `backfill(batched=True)`; the
  intel job and the post-batch tail stay synchronous (they sit on interactive /
  fire-and-forget paths where hour-scale batch latency is wrong). The two waves map
  to two sequential batches (custom_id `<trigger>_<index>` — the API allows only
  `[A-Za-z0-9_-]`, never the domain); results classify through the same
  `_finish_verdict` path as sync. Durability: every submitted batch id is persisted
  to `data/outreach/news_batches.json` before polling, poll errors retry until the
  deadline instead of failing the run, a failed/timed-out wave degrades to per-row
  error verdicts (retried next run) instead of discarding the other wave, and a
  timeout best-effort-cancels the batch. The finalize step (HubSpot write-back →
  composite → upserts, per-domain freshness re-check) runs in its own thread pool
  (`NEWS_FINALIZE_WORKERS`, default 4). Knobs: `NEWS_BATCH` (0 disables batch even
  for opt-in callers), `NEWS_BATCH_MIN` (3), `NEWS_BATCH_POLL_S` (20),
  `NEWS_BATCH_TIMEOUT_S` (86400 — the API guarantees results within 24h). Coarse
  progress rides the job's `current` slot ("wave 1: 240/1048 requests done").
- **Cost gotcha (load-bearing):** every non-skipped scan is up to 6 API calls (5
  research × 3-4 searches, ≤16 total, + 1 composite) — run first backfills with
  `--limit`, keep `NEWS_MODEL` pinned to a mid-tier model, and remember the
  post-batch tail researches every new domain a batch touches. The 2026-09 run:
  9,102 searches across 1,262 accounts, all on the accidental Opus 4.8 default.
  Per-verdict `usage` in `news_detail.triggers` records raw token counts
  (`input/output/cache_*` + `web_search_requests`; batched rows bill tokens at 50%
  of those numbers — `detail.batched` marks them). API credit exhaustion
  mid-backfill stores `news_error` rows ("credit balance is too low"), which retry
  on the next run.
- **Copy consumer (2026-09):** the gated approval flow (section below) consumes the
  verdicts — contacts approved through a trigger segment generate via
  `generate_batch.py`'s **erp-trigger** path, which anchors email 1 on the stored
  verdict. Autonomous (SLA) generation never reads the `news_signals` column or the
  erp-trigger path — but since 2026-09 it DOES reach news research indirectly: a fresh
  composite in `signal` is reused as its cached research (see the composite bullet).

## Agent studio — editable instructions (Orchestration view, added 2026-09)

The Orchestration view is the operator's editing surface for how the AI SDR thinks:
custom ICP keywords, per-persona framing (pain/outcome), the five ERP trigger plays,
and the knowledge-base markdown (offer.md / cta-offers.md / icp-email.md) — all
editable in plain language, no code.

- **Override layer:** `data/outreach/instructions/` on the volume (gitignored),
  managed by `ai-sdr/scripts/instructions.py` (stdlib-only; `INSTRUCTIONS_DIR` env
  override for tests). Readers NEVER raise and fall back to the committed defaults
  on any problem — an absent/broken layer means exactly the pre-studio behavior.
  Writers are the web server only (atomic replace under `INSTR_LOCK`).
- **Consumers:** `generate_batch.load_knowledge()` (knowledge doc overrides),
  `erp_play()` (per-field play merge), `persona_framing()` (composed from persona
  pain/outcome edits); `buyer_group.buyer_role()` reads custom include/exclude
  keywords per call (custom excludes run before everything, custom includes only
  after every built-in bucket fails — precedence never changes);
  `orchestration_config` renders EFFECTIVE docs + overlays persona edits so the
  view never shows text the pipeline stopped using.
- **Endpoints:** `GET /api/instructions` (defaults + overrides + meta),
  `POST /api/instructions/save` `{kind: persona|play|knowledge|icp, key, content}`
  (validated; returns soft warnings, e.g. banned terms in a knowledge edit),
  `POST /api/instructions/reset` `{kind, key}`, `GET /api/instructions/icp-test
  ?title=` (classify a title with the live rules).
- **Deliberately NOT editable:** `lint_sequence.py` and the suppression gates —
  every generated email still passes the linter regardless of edits, so a bad edit
  surfaces as lint failures in Pipeline/Outreach, never as a bad send.
- **Edits apply to the NEXT generation run** — already-generated copy is unchanged
  until regenerated (the UI says so on every save).

## Gated approval flow — segments → review → enroll (added 2026-09)

User-approved workflow change: manual list pulls + CSV uploads stop at TWO human gates;
**SLA-sourced contacts bypass both** (fully autonomous, the pre-gate behavior). The Use /
Replies / Analytics / Trends views are unchanged; Pipeline is the staged workflow and
Outreach is the review surface.

- **Contact columns** (`batch_db.py`, additive migration): `gated` (1 = in the approval
  flow; set at insert via `upsert_contacts(rows, gated=True)` — `sdr_batches init
  --gated`, `csv_audience.py ingest --gated`; `do_ingest` passes it for every non-SLA
  source), `segment` + `account_approved_at` (stamped at the segment gate), `approved_at`
  (stamped at the outreach gate). **`assign_batches` skips gated contacts until their
  account is approved** — they sit batch-less in the segments screen. `status` values are
  unchanged (pending/generated/enrolled/failed/skipped).
- **Stage 2 — signal intelligence first:** after a gated ingest, `start_intel_job`
  (app.py, `INTEL_JOBS`, single-flight) runs tech → hiring → news `backfill(domains=…)`
  over every account awaiting review (cache-aware, so re-runs are cheap; unavailable
  engines are recorded per-stage, never fatal). `POST /api/intel/run` re-kicks manually;
  state rides on `GET /api/segments` + `GET /api/intel/status`.
- **Stage 3-4 — segments:** `GET /api/segments` groups awaiting accounts by memberships
  (every FOUND news trigger + `hiring` when sales roles exist + `no_signals`; an account
  can be in several; `news_signals IS NULL` rows are `pending` research).
  `POST /api/segments/approve {segments|domains|all}` stamps each approved account's
  WINNING segment (highest-scoring selected trigger, else hiring, else no_signals; the
  five trigger winners also get `variant='erp-trigger'`), batches the contacts
  (`approve_accounts`), and auto-starts generation on the new batches
  (`start_generate_job` now takes a LIST of batch ids — one job, sequential batches).
  Approve-all pushes unresearched accounts through on the default path.
- **Trigger-anchored generation:** `generate_batch.py` `ERP_PLAYS` (the five
  user-approved Problem/Solution instruction sets) + `ERP_SYSTEM` (Value Global ERP Data
  Retirement copywriter, NOT the EverWorker knowledge base) + `generate_contact_erp` —
  **write-only, no web search** (the stored verdict from `news_detail` IS the research;
  `_segment_verdict` fetches it). Email 1 opens on the trigger event; linter is
  `lint_erp` (structural: 28-110 words, question in touch 1, breakup in touch 4, no
  dashes/hype/pricing; NO metric requirement). Registered in `LINTERS['erp-trigger']`,
  so ingest lint + re-lint route correctly; enrollment falls back to the persona
  campaign (no erp-trigger Bison campaign env yet). A missing verdict (re-scan dropped
  it) falls back to the default research path with default rules — never crashes. Both
  the real-time path and Message-Batches (`prepare_batch_requests`) branch this way.
- **Stage 5-6 — outreach gate:** `POST /api/outreach/<cid>/update {email?, linkedin?}`
  applies a HUMAN edit to the generated asset (saved verbatim + `edited_at`/`edited_by`;
  lint runs only for soft warnings; a complete edit re-promotes even a `failed` contact
  to `generated` — human wins). `POST /api/outreach/approve {contact_ids|all}` stamps
  `approved_at`. **Enrollment eligibility** = `status='generated'` AND (`gated=0` OR
  `approved_at` set) — enforced in `sdr_batches cmd_enroll` via `db.enroll_eligible`;
  `review_counts` feeds the Pipeline stage tiles + `enroll_ready`.
- **UI:** Pipeline = `SegmentsPanel` (self-polling; approve buttons; hands the
  generation job id to the existing `GenerateJobPanel`) → batch progress → review
  banner → `EnrollPanel` ("N approved ready" + "M awaiting approval"). Outreach =
  approval filter/column, row checkboxes + bulk approve, and `OutreachDetail` edit mode
  (✎ Edit copy / Save / ✓ Approve for enrollment).
- **Gotchas:** the server does a best-effort `init_schema` at boot (read-write, once) so
  its read-only queries never hit missing approval columns; `db_contact_meta` also
  falls back to the old SELECT. Approval writes go through batch_db in-process
  (`retry_locked`, the detect-engine pattern) — never hold them across network calls.
  There is deliberately NO env kill-switch: "Approve all" is the escape hatch if a
  backlog must flow through un-reviewed.

## Signal notes contact write-back (added 2026-08)

After every batch ingest, the researched signal + **email touch 1's body only**
(user-approved scope/format: `Signal:\n<signal>\n\n<body1>` — no subject, no body label,
never touches 2-4 or LinkedIn) is mirrored to the HubSpot **contact** property
`sdr_signal_notes` (multi-line text, pre-existing in the portal).

- **Helper:** `.claude/skills/sdr-pipeline/scripts/signal_notes.py` (stdlib-only) —
  `format_note()` / `note_update()` / `sync_contacts()`. Best-effort in the tech/hiring
  write-back style: lazy client singleton, whole-body try/except, stderr logging, never
  raises. `SDR_NOTES_HUBSPOT_WRITEBACK=0` kills it (caller-evaluated via `enabled()`).
- **Hook:** `sdr_batches.py cmd_ingest` — the single choke point all three generation
  paths funnel through (inline UI job, Message-Batches poller, CLI). Contacts that
  ingest as `generated` are collected and PATCHed in ONE `batch_update("contacts", ...)`
  call (contact_id IS the HubSpot id — no lookup). Latest generation wins (re-ingest
  re-syncs); an asset with no touch 1 yields no update, so notes are never blanked.
- **stdout contract (load-bearing):** `cmd_ingest` prints exactly ONE stdout line and
  `app.py` head-truncates it to 200 chars — the sync outcome is appended to that line
  (`, notes: N synced`); all detail goes to stderr. Never add stdout lines before it.
- **Backfill:** `sdr_batches.py notes-backfill [--dry-run] [--limit N]
  [--status generated,enrolled,skipped]` — one-time catch-up over pipeline.db contacts
  joined to `generated/<cid>.json`; idempotent, no ledger. `--dry-run` prints a sample
  note to stderr.
- **Gotchas:** one deleted/merged contact id 4xx-fails a whole 100-chunk — `sync_contacts`
  retries that chunk one-by-one to isolate it (matters for the ~2k backfill, not fresh
  25-contact ingests). `ensure_contact_property` may 403 without schema scopes — it's
  wrapped separately and skipped (the property already exists). Notes truncate at 65,000
  chars (HubSpot multi-line cap is 65,536).

## Unenrollment checker (added 2026-07)

Suppression sweeps: contacts RevOps tagged with the HubSpot contact property
`everworker_tag = "false"` (enumeration, values `"true"`/`"false"`) must never be touched
by the AI SDR again — they booked a meeting, became an opportunity, etc.

- **Engine:** `.claude/skills/sdr-pipeline/scripts/unenrollment_check.py` (module + CLI:
  `--json --dry-run --limit --force --contact-id`; `--contact-id` implies `--force`).
  Per sweep: search contacts where
  `everworker_tag EQ "false"` (10k re-window via `hs_object_id GT`), then per contact —
  **Bison**: email → lead (`bison_lead_map`, live fallback) → `lead_scheduled_emails` →
  `stop_future_emails` in each campaign with steps still queued (`scheduled`/`sending
  paused`); **HeyReach**: linkedin url → `get_campaigns_for_lead` → `stop_lead_in_campaign`
  per non-finished campaign. One HubSpot timeline note per actually-stopped contact
  (`UNENROLL_HUBSPOT_NOTE=0` kills it; never for never-enrolled contacts).
- **Ledger:** `unenrollment_log` in pipeline.db, dedup key `<rule>:<channel>:<contact_id>`
  — sweeps are idempotent; `failed` retries every sweep; `skipped_unconfigured` re-arms
  automatically once the channel's API key lands; `done` rows re-verify after
  `UNENROLL_RECHECK_HOURS` (default 24) so a contact re-enrolled while still flagged is
  re-stopped within a day; `--force` re-checks everything now.
  **One-way**: flipping the tag back to `"true"` only re-permits future enrollment (the
  gate's live check wins over the ledger) — stopped sequences stay stopped.
- **Gates:** `hubspot_pull.py` drops flagged contacts at pull time (`skipped.suppressed`);
  `sdr_batches.py cmd_enroll` + `enroll.py main()` call
  `unenrollment_check.suppressed_set()` (live tag check, ledger fallback, fail-open —
  the sweeper is the backstop) and skip suppressed contacts with a `suppressed` count.
- **Server:** `_unenrollment_loop()` every `UNENROLL_CHECK_MINUTES` (default 30,
  `UNENROLL_CHECK_ENABLED=0` disables); `GET /api/unenroll/status` (rules[]-shaped —
  future suppression rules append entries), `POST /api/unenroll/run` (`{dry_run?}`,
  409 if running). UI: Orchestration view — a dashed "safety gate" bar in the diagram +
  an "Unenrollment & suppression rules" card section (Run now / Dry run buttons).
- **Scale + queueing (2026-08 fix, load-bearing):** a RevOps bulk workflow flagged
  ~23k contacts, and the original ascending-id sweep starved never-checked contacts
  behind thousands of retrying failures — newly flagged, still-mid-sequence contacts
  were literally never reached before the 1h `run_script_streaming` timeout killed
  the run. The sweep now queues NEVER-CHECKED contacts first, newest id first
  (capped `UNENROLL_FRESH_LIMIT`, default 1000), then rotates failed/re-check
  retries oldest-ledger-entry first (capped `UNENROLL_RETRY_LIMIT`, default 500).
  Channel API calls are paced (`UNENROLL_PACE_S`, default 0.25s — HeyReach caps at
  300 req/min) and a channel failing `UNENROLL_CIRCUIT_THRESHOLD` (default 8) times
  consecutively is deferred for the rest of the sweep WITHOUT ledger rows (so those
  contacts keep full priority next sweep instead of minting failed rows). Ledger
  writes retry lock contention and never abort the sweep (`ledger_errors` in the
  summary). `unenrollment_counts` exposes `top_errors`; the UI card shows the top
  failure + "Flagged in HubSpot" (last sweep's search total) vs "Contacts swept"
  (ledger coverage) — they legitimately differ while a backlog drains.
- **Gotchas:** HeyReach lead/campaign endpoints all live under the `/campaign/`
  controller (`/campaign/GetCampaignsForLead`, NOT `/lead/…` — the first live run
  404'd on that; paths are cross-checked against the bcharleson/heyreach-cli client).
  Keep `import unenrollment_check` cheap (its module imports are stdlib + batch_db
  only; clients import lazily inside functions — boot rule). NEVER hold a pipeline.db
  write transaction across network calls in ANY writer (the activity sync's lead-map
  refresh once did — its interleaved Bison pagination held the write lock past
  busy_timeout and crashed concurrent sweeps with "database is locked"; it now
  buffers the fetch and writes in one short transaction).

## SLAs — automatic enrollment rules (ported from the Radicle console, 2026-09)

- **Store:** `webui/server/sla_store.py` → `data/outreach/slas.json` (gitignored; `SLA_STORE_PATH`
  override for tests). Validation: name < 100 chars; schedule `every ∈ {5,15} minutes | {1,5,10}
  hours | {3,7,14} days` (days fire at 22:00 UTC — `next_run_after`); ≤ 10 company + ≤ 10
  contact filters (`{property, operator, value | values | high_value, label, type}` with HubSpot
  search operators; IN/NOT_IN → `values`, BETWEEN → value+high_value, HAS_PROPERTY/
  NOT_HAS_PROPERTY valueless); at least one criterion (form or any filter). Per SLA:
  `hubspot_list_id`, `added_ids`, `last_run`, `runs[≤20]`, `totals`, `next_run_at`.
- **Run:** `scripts/sla_run.py --sla-file … --sla-id … [--dry-run] --json` — evaluate
  (company filters → ids → contacts via `associations.company IN` chunks of 100, AND contact
  filters, windowed past the 10k search cap by `hs_object_id GT`; form → `form-integrations/v1/
  submissions/forms/{id}` since `last_run_at` → emails → `find_existing_email_ids` → filtered
  by the criteria) → minus pipeline-DB contacts (via `batch_db.connect()`) and `added_ids` →
  `create_list("AI SDR SLA · <name>")` (idempotent by name) + `add_contacts_to_list` → JSON
  summary as the last stdout line. Dry-run never writes. `app.run_sla` (single-flight per SLA,
  `SLA_RUNS`) then runs `do_ingest(list_id, source="sla:<id>")` under `INGEST_LOCK` (manual
  `/api/ingest` takes the same lock → 409 if busy) and `record_run` rolls totals + next_run.
  `_sla_loop` checks `due()` every 60 s (`SLA_ENABLED` gate, read via `read_env()` like the
  other loops). HubSpot metadata for the wizard: `hubspot_forms_payload` (`/marketing/v3/forms`,
  10-min cache) and `hubspot_properties_payload` (`/crm/v3/properties/{contacts|companies}`,
  options for enumerations, exact/prefix-ranked search).
- **Pull history:** `data/outreach/pull_history.json` (gitignored; `record_pull` in `do_ingest`
  parses the pull's "Pulled N list members → M read → K ICP contacts." stdout line) →
  `pulled_at/pulled_kept/pulled_source/pulls` merged into `/api/hubspot/lists` rows;
  `ListPicker` shows the badge/column/"Pull again". `search_lists` in `hubspot_client.py` now
  pages through every match and orders newest-created first (the API has no sort).
- **UI (Use view, reworked to match):** "Select Target Audience" header; one **CRM List** panel —
  type dropdown + search + collapsed show/hide-lists table (Created/Size/Pulled/ID columns),
  Select → inline "2 · Selected … Confirm — run pull + init" banner; `SlaPanel.jsx`
  (table + `SlaWizard`: schedule → criteria (FormPicker, FilterGroup ×2 with PropertyPicker +
  FilterRow operators/values by type; enum options are UI-only and re-fetched on edit) →
  confirm) mounted below it. Stats + pending batches live on the Pipeline tab.

## CSV audiences (added 2026-09)

Third way to feed the pipeline from the Use view (next to a CRM list and SLAs): upload a
contact CSV, name it, and it lands as a named **audience** of pipeline batches.

- **Runner:** `.claude/skills/sdr-pipeline/scripts/csv_audience.py` (stdlib-only; module +
  CLI `ingest --file … --name … [--id aud-<hex>] [--by] [--batch-size 25] [--dry-run]`,
  offline `--self-test`). Parses flexible header spellings (first/last name, job title,
  email, LinkedIn URL, country, company name/website/industry/employees; comma/semicolon/
  tab + BOM handled), requires a valid email per row, dedups in-file and against
  pipeline.db by email, assigns personas via `buyer_group.persona_for_title` — an
  **unmatched title defaults to `sales-leadership`** (`persona_defaulted` count) instead of
  being dropped: a hand-picked upload is trusted, so no ICP/geo filters apply (unlike
  `hubspot_pull.py`). Inserts via `batch_db.upsert_contacts` + `assign_batches` (same
  idempotent `retry_locked` shape as `sdr_batches init`), prints a JSON summary as the
  LAST stdout line.
- **Synthetic ids (load-bearing):** contacts get `csv-<suffix>-<n>` ids — these are NOT
  HubSpot ids. Everything HubSpot-id-keyed skips or tolerates them by design:
  `signal_notes.note_update` returns None for non-numeric ids (never 4xx-poisons a batch
  chunk), `unenrollment_check.suppressed_set` already filters to digits, activity sync
  resolves by email, `heyreach_account_for` hashes non-numeric ids. Enrollment (Bison by
  email, HeyReach by LinkedIn URL) and generation work unchanged. Live status joins back
  to an audience by id prefix.
- **Domain preference:** rows carry `domain` from the company website (falls back to the
  email domain in `upsert_contacts`), so signal research / tech / hiring scans hit the
  company site even for personal-mailbox contacts. (`upsert_contacts` now honors a
  caller-provided non-empty `domain` for any caller.)
- **Store:** registry `data/outreach/csv_audiences.json` + raw uploads under
  `data/outreach/csv-uploads/<audience_id>.csv` (both gitignored, live on the volume).
  The web server is the registry's single writer (`record_audience`/`rename_audience`
  under `CSV_AUD_LOCK`, atomic replace) — the script only writes the DB. Extra CSV
  columns (country/industry/employees) aren't stored in the DB; the kept raw CSV is the
  provenance.
- **Endpoints:** `POST /api/audiences/upload` `{name, filename, csv}` (CSV as a JSON
  string field; 10 MB cap; takes `INGEST_LOCK` → 409 while a pull/SLA runs),
  `GET /api/audiences` (registry + live per-status counts from the read-only DB),
  `GET /api/audiences/<id>` (audience + its contacts), `POST /api/audiences/<id>/rename`.
  UI: Use view "CSV Upload" panel (file + name → summary banner; audiences table with
  status chips, inline rename, expandable contact list).

## Background jobs (daemon threads started in `app.py main()`)

1. `_activity_autosync_loop` — hourly: logs new email/LinkedIn activity to HubSpot.
2. `_aisdr_sync_loop` — nightly midnight ET: deal attribution (above).
3. HeyReach webhook drain — near-real-time LinkedIn activity logging.
4. `_unenrollment_loop` — every 30 min: everworker_tag suppression sweeps (above).
5. `_sla_loop` — every 60 s: runs due SLAs (`SLA_ENABLED` gate; section above).

## HubSpot notes

- The app's own token (`HUBSPOT_ACCESS_TOKEN`, private app, EU portal 144358290 — but API
  host is always `api.hubapi.com`) is the source of truth for API capability.
- The HubSpot **MCP** connector available in Claude sessions has narrower scopes: it can
  read/search deals & contacts but CANNOT read email engagements or run SQL
  (`reporting-base-read` missing). Use it for spot-checks; use the app's scripts for real
  work.
- Custom properties in the portal: deals `ai_sdr_deal_created` (bool); contacts
  `ai_sdr_deal_created`, `ai_sdr_meeting_booked`, `ai_sdr_reply_generated` (+
  `ai_sdr_status`, `ai_sdr_errors`), and `everworker_tag` (RevOps-maintained
  enumeration `"true"`/`"false"`; `"false"` = do-not-contact, drives the
  unenrollment checker). Contact LinkedIn URL property =
  `HUBSPOT_LINKEDIN_PROPERTY` (default `hs_linkedin_url`). Owner/created-by id→name maps
  come free from the `hubspot_owner_id` / `hs_created_by_user_id` property definitions'
  enum options — no extra scope needed.

## Dev / verification quickies

```bash
python3 -m py_compile webui/server/app.py .claude/skills/sdr-pipeline/scripts/*.py
env -u PORT python3 webui/server/app.py --port 8787   # boots WITHOUT pymongo/MONGO_URL/dnspython
cd webui/frontend && npm ci && npm run build           # SPA build (Dockerfile stage 1)
python3 .claude/skills/sdr-pipeline/scripts/tech_signals.py --self-test   # offline detector check
python3 .claude/skills/sdr-pipeline/scripts/hiring_signals.py --self-test # offline hiring-classifier check
```
The server must always boot with `MONGO_URL` unset (aisdr endpoints return
`{"configured": false}`, nightly loop self-disables) — preserve that when touching
anything Mongo-related.
