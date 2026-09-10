"""Company-news signal research runner — module + CLI.

Web-researches the five Value Global ERP Data Retirement buying triggers for
one company (Anthropic Messages API + the server-side web_search tool — the
same research channel generate_batch.py uses) and stores the verdicts on the
`account_signals` row for that domain:

    news_signals    "M&A carve-out 85: Acme completes carve-out of FooCo · ERP
                    migration 70: hiring S/4HANA leads" (found triggers, score
                    descending; or the literal "No ERP news signals detected";
                    NULL only if the scan itself failed — every trigger errored)
    news_detail     JSON: {triggers: {id: verdict}, found_count, web_searches,
                    duration_ms, model, max_searches (per-trigger dict of the
                    researched budgets), hubspot, composite ({ok, stored,
                    error} — present when the composite step ran)}
    news_checked_at ISO-8601 Z; scans are reused for NEWS_REFRESH_DAYS (30 —
                    shorter than tech/hiring because most triggers carry a
                    90-day recency window)
    news_error      set only when the whole scan failed (such rows retry next
                    touch); per-trigger errors live in news_detail

The five triggers (instruction sets user-approved 2026-09):
    ma_carveout      acquisition / divestiture / carve-out in the last 90 days
    erp_migration    SaaS ERP modernization (Fusion Cloud / S/4HANA program)
    license_audit    Oracle license-audit exposure (composite proxy signals;
                     cross-references ma_carveout's verdict)
    ebs_oci          EBS lifted onto OCI, still on EBS (cross-references
                     erp_migration — a full re-platform to Fusion is NOT this)
    ebs_performance  EBS month-end-close / data-volume pain. Pre-condition:
                     runs ONLY when the technographic scan detected Oracle
                     E-Business Suite at the account; otherwise recorded as
                     skipped (not an error).

Each trigger is one Messages call with web search (per-trigger max_searches, 3-4,
tuned from the 2026-09 live run; NEWS_MAX_SEARCHES overrides all of them when
set) returning a strict JSON verdict {found, score 0-100, headline,
summary, date, source_url, details}. Calls run in two waves so cross-referenced
triggers see their upstream verdicts:
    wave 1: ma_carveout, erp_migration, ebs_performance   (concurrent)
    wave 2: license_audit, ebs_oci                        (concurrent)

Failure semantics (tech_signals invariant "non-NULL = definitive answer"):
a trigger whose API call failed or returned unparseable JSON records `error`
in its detail entry; the scan only counts as failed (news_signals NULL +
news_error, retried next touch) when EVERY researched trigger errored. A
partial scan stores the successful verdicts and is reused for the refresh
window like any other.

After a successful scan the found-trigger lines are also written to the
HubSpot company property `erp_news_signals` (best-effort, matched by domain;
NEWS_HUBSPOT_WRITEBACK=0 to disable).

Import contract: this module is stdlib-only at import time (batch_db +
hubspot_client._load_dotenv, both stdlib). anthropic_client (also stdlib) is
imported lazily so webui/server/app.py keeps booting with no ANTHROPIC_API_KEY
— `news_available()` reports why research is off instead of crashing.

A scan that FOUND >=1 trigger also synthesizes a composite `signal` (one extra
no-web-search call; fill-only via upsert_composite_signal — a fresh
generation-researched signal always wins, company_name is never touched).
Copy generation's default path treats that fresh signal as cached research.

Env knobs: NEWS_DETECT_ENABLED (post-batch tail hook), NEWS_REFRESH_DAYS (30),
NEWS_MAX_SEARCHES (unset = per-trigger caps 3-4; integers clamp to >=1),
NEWS_MODEL (defaults to CLAUDE_MODEL), NEWS_TRIGGERS (comma-scoped trigger
ids, default all), NEWS_HUBSPOT_WRITEBACK (1), NEWS_COMPOSITE_SIGNAL (1).

CLI (run_script conventions: progress lines on stderr, JSON summary as the
LAST stdout line):

    python3 news_signals.py --domain acme.com [--company "Acme"] [--force]
                            [--triggers ma_carveout,erp_migration] [--no-hubspot]
    python3 news_signals.py --missing [--stale-days N] [--limit N]
                            [--workers N] [--force] [--no-hubspot]
    python3 news_signals.py --self-test      # offline; no network, no key
    python3 news_signals.py --refloor [--dry-run] [--limit N]   # guard stored verdicts (DB-only)
    python3 news_signals.py --recompose [--limit N] [--force]   # composites for stored found rows

Every non-skipped scan costs real Anthropic spend — up to 6 API calls (5
research with 3-4 web searches each, capped at 16 total, plus 1 no-search
composite when triggers were found) — so prefer --limit on a first bulk
backfill.
"""

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import batch_db as db
from hubspot_client import _load_dotenv

_load_dotenv()

HERE = Path(__file__).resolve().parent                       # sdr-pipeline/scripts
AI_SDR_SCRIPTS = HERE.parents[1] / "ai-sdr" / "scripts"      # anthropic_client

NO_NEWS = "No ERP news signals detected"
NEWS_PROPERTY = "erp_news_signals"
NEWS_PROPERTY_LABEL = "ERP News Signals"
MAX_TOKENS = 1500        # a verdict is small; searching happens server-side
CALL_TIMEOUT = 300       # seconds per trigger call (web search rounds are slow)
HEADLINE_MAX = 300       # chars kept from a model headline (word-boundary + ellipsis)
LINE_HEADLINE_MAX = 100  # chars of headline shown in the news_signals line


def _truncate(text, limit):
    """Cut on a word boundary with an ellipsis instead of a mid-word hard slice
    (the drawer renders these verbatim, so a bare slice reads as cut off)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip() + "…"


def _utcnow():
    """Timezone-aware UTC now. The prompts' 'today'/cutoff and the stored
    news_checked_at (batch_db.now(), UTC) must agree — naive local time drifts
    a day either side of midnight on non-UTC hosts, and these prompts reason
    about dates."""
    return datetime.now(timezone.utc)


def log(msg):
    sys.stderr.write(f"[news] {msg}\n")
    sys.stderr.flush()


def _flag(name, default=True):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def news_available():
    """(ok, reason). False when ANTHROPIC_API_KEY is missing — callers degrade
    instead of crashing (tech_available pattern). Plain env read; .env was
    already autoloaded at import."""
    if (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return (True, "")
    return (False, "ANTHROPIC_API_KEY not set")


# _clean_domain: same normalization as tech_signals.py / hiring_signals.py.
def _clean_domain(domain):
    if not domain:
        return None
    d = str(domain).strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/", 1)[0].split("?", 1)[0]
    if d.startswith("www."):
        d = d[4:]
    return d or None


_WARNED_ENV = set()


def _news_search_override():
    """NEWS_MAX_SEARCHES, parsed strictly: unset/blank -> None (the per-trigger
    caps apply); any integer -> clamped to >=1 (an explicit 0 still means 'the
    minimum', as before the per-trigger caps existed); malformed -> warn once
    and ignore rather than silently disabling the operator's override."""
    raw = (os.environ.get("NEWS_MAX_SEARCHES") or "").strip()
    if not raw:
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        if raw not in _WARNED_ENV:
            _WARNED_ENV.add(raw)
            log(f"NEWS_MAX_SEARCHES={raw!r} is not an integer — override ignored")
        return None


def _max_searches(tid):
    """Per-trigger web-search budget: NEWS_MAX_SEARCHES (when set) overrides
    everything, then TRIGGERS[..]["max_searches"], then 4. Caps tuned from the
    2026-09 live run (1,262 accounts): found verdicts needed a 4th search only
    on erp_migration (median 4); ma_carveout found at median 2, ebs_oci ≤3."""
    env = _news_search_override()
    return env if env is not None else (TRIGGERS[tid].get("max_searches") or 4)


# ---- trigger instruction sets ------------------------------------------------
# Each brief is the user message for one Messages call. Placeholders: {company}
# {domain} {cutoff_date} {max_searches} plus per-trigger context tokens filled
# by _build_user. JSON examples double their braces for str.format.

SYSTEM_PROMPT = """\
You are a B2B sales-research agent for Value Global's ERP Data Retirement offering. You research ONE
company for ONE specific buying trigger, using web search, and return a strict JSON verdict.

# Today's date
Today is {today}. Judge every date you find against this — recency windows are strict. BEFORE you use
any item, verify its date; never present old news as recent.
Web pages are written at a moment in time and go stale: a page describing a "projected", "planned",
or "upcoming" event may have been written long before you are reading it. NEVER adopt a source's
tense — convert every date a source mentions into how long ago (or how far ahead) it is relative to
today before judging recency or stage. A "projected go-live" or "expected close" date that is
already in the past means that event has likely HAPPENED; reason about what is true today, not what
was true when the page was written.

# Ground rules
- The email domain is ground truth for WHO the company is. If the stated company name does not match
  the company operating that domain today (acquisition, rebrand, merger, stale CRM data), research the
  DOMAIN's company under its current name. Exception: a personal/free email domain (gmail.com and the
  like) says nothing about the employer — keep the stated company then.
- Report only what a source actually says. NEVER invent, embellish, or upgrade a weak signal — a wrong
  found=true poisons outreach; a clean found=false is a good result.
- Prefer primary sources: company newsroom, SEC filings, official press releases, the company's own
  careers pages. Aggregators and rumors do not qualify on their own.
- Every found=true verdict MUST cite the best source_url you actually used.
- Use AT MOST {max_searches} web searches. If nothing qualifies, STOP searching and report
  found=false — do not keep burning searches.

# Output
Return ONLY one JSON object, no prose, no markdown fences:
{{"found": true|false,
  "score": <integer 0-100; 0 when found=false>,
  "headline": "<one line, max ~120 chars: what you found; empty when found=false>",
  "summary": "<2-4 sentences: the evidence and why it qualifies, or the closest miss and why it was disqualified>",
  "date": "<YYYY-MM of the event, or ''>",
  "source_url": "<best source URL, or ''>",
  "details": {{<the trigger-specific fields listed in the task>}}}}
"""

TRIGGERS = {
    "ma_carveout": {
        "label": "M&A carve-out",
        "max_searches": 3,  # live run: found verdicts at median 2 searches; only 2/84 used a 4th
        # Deterministic backstop for the brief's "LAST 90 DAYS" rule (month
        # granularity — a found verdict dated strictly before the cutoff month
        # is downgraded; the boundary month is kept).
        "recent_window_days": 90,
        "brief": """\
# Trigger: M&A Carve-Out — an acquisition or divestiture announced in the LAST 90 DAYS
Why it matters (context only, never quote it): when a company divests or acquires, the separated
entity keeps running on the parent's ERP through a 6-12 month Transition Services Agreement window,
with messy commingled data — any acquisition or divestiture starts an ERP data-separation clock.

Research news STRICTLY within the last 90 days (on or after {cutoff_date}). Sources in priority order:
1. Company newsroom / press releases.
2. SEC EDGAR 8-K filings: search site:sec.gov "{company}" 8-K
3. PE firm portfolio announcements (if a PE buyer is involved).
4. Reputable M&A press (Reuters, Bloomberg, industry trades).
Searches to draw from (pick the most promising first):
"{company}" divestiture / "{company}" "carve-out" / "{company}" spinoff /
"{company}" "definitive agreement" / "{company}" completes acquisition

details to extract: {{"deal_type": "divestiture|spinoff|acquisition", "entity": "<the entity being separated or acquired>", "close_date": "<announced/expected close date, or ''>"}}

Scoring: divestiture is hottest (75-95), then spinoff (65-85), then acquisition (50-80) — scale
within the band by deal size / ERP implication and how recent the announcement is.
DISQUALIFIERS (report found=false): stock buybacks; minor asset purchases with no ERP implication
(a small product line, IP, a single facility); deals announced more than 90 days ago; rumors with no
primary source.
""",
    },
    "erp_migration": {
        "label": "ERP migration",
        "brief": """\
# Trigger: SaaS ERP migration / modernization (Oracle Fusion Cloud, SAP S/4HANA)
Why it matters (context only): a new SaaS ERP only accepts open items and recent data — 20 years of
legacy EBS transactional history stays behind under audit, tax, and legal-retention obligations.

Highest-signal sources:
1. Job postings: "{company}" "S/4HANA" / "{company}" "Oracle Fusion" / "{company}" "ERP transformation"
   — reqs like "S/4HANA Migration Lead", "Oracle Cloud ERP Functional", "ERP Program Manager".
2. SI press releases: "{company}" Deloitte OR Accenture OR Infosys S/4 OR Fusion
3. Earnings-call transcripts — CFOs announce ERP modernization: "{company}" earnings transcript ERP
4. Company press / analyst coverage.

details to extract: {{"platform": "<Oracle Fusion Cloud|SAP S/4HANA|other|''>", "stage": "<evaluating|selected|mid-implementation|''>", "si_partner": "<SI partner if named, or ''>"}}

Scoring: evaluating = 40-60, selected = 60-75, MID-IMPLEMENTATION = 75-95 (the sweet spot —
data-migration pain is imminent). Program news may be older than 90 days if the program is clearly
still running.
Determine the stage AS OF TODAY, not as of the source's writing: compare every milestone date
(selection, kickoff, projected go-live) against today's date. A "projected" or "planned" go-live
that is already in the past is NOT mid-implementation — the cutover most likely happened; only
report mid-implementation with evidence the program is still running today (post-go-live
stabilization work, ongoing data migration, currently-open program job reqs). A go-live that passed
without such evidence is a completed migration — score it as such or disqualify it.
DISQUALIFIERS (report found=false): a migration COMPLETED more than a year ago with nothing still
running; generic "digital transformation" talk with no ERP program behind it.
""",
    },
    "license_audit": {
        "label": "License audit",
        "context_from": "ma_carveout",
        # Deterministic backstop for the prompt's "never found below 55" rule:
        # live data (2026-09, 1,262 accounts) showed 105 of 123 found verdicts in
        # the weak 30-54 one-proxy band — segment filler, not outreach anchors.
        "min_found_score": 55,
        "max_searches": 3,  # live run: found at median 3; the 4th search fed the weak band

        "brief": """\
# Trigger: Oracle License Audit exposure
Why it matters (context only): when Oracle's audit clock starts, every database, module, and
environment where retired or dormant EBS data still lives counts toward the license and support bill
— the bigger the live footprint at audit time, the bigger the liability. Look for signs this company
is heading into (or worried about) an Oracle audit or an Oracle-spend review.

Research signals (ANY of these raises the score):
1. Recent M&A — audits often follow acquisitions, license counts get messy.{ma_context}
2. A new hire in Software Asset Management (SAM) / IT Procurement / Vendor Management:
   search site:linkedin.com "{company}" "software asset management" OR "IT procurement"
3. Public commentary about Oracle spend, Java SE licensing exposure, or cost optimization:
   "{company}" Oracle cost / "{company}" "Java" licensing
4. Broader news of Oracle audit activity in the company's industry.

details to extract: {{"signals": ["<one line per signal actually found>"]}}

Scoring: composite trigger with a HIGH bar. found=true requires TWO OR MORE independent signals, or
ONE strong direct signal (public Oracle-spend/Java-licensing commentary, a fresh SAM hire, an
Oracle usage assessment or audit-adjacent tender) = 55-80; direct evidence of an active or announced
Oracle audit = 80-95. One weak proxy alone (company size, industry, generic recent M&A with no
license implication) does NOT qualify — report found=false and name the near-miss in the summary.
Never report found=true with a score below 55.
DISQUALIFIERS (report found=false): a single weak proxy signal; no EBS and no broader Oracle
footprint; a company clearly reducing Oracle to zero (already fully migrated off Oracle).
""",
    },
    "ebs_oci": {
        "label": "EBS on OCI",
        "context_from": "erp_migration",
        "max_searches": 3,  # live run: all 8 found verdicts needed ≤3 searches
        "brief": """\
# Trigger: EBS lift-and-shift onto OCI (still running EBS, hosted on Oracle's cloud)
Do NOT confuse these: Oracle OCI (Oracle Cloud Infrastructure) is just infrastructure that hosts an
existing EBS install. Oracle Fusion Cloud ERP is a DIFFERENT application — Oracle's modern SaaS ERP
that would REPLACE EBS. This trigger fires only when the company is still RUNNING EBS but has moved
(or is moving) it onto OCI. Why it matters (context only): they moved the problem to the cloud
instead of shrinking it — full license, support, compute, and storage on 20 years of mostly dormant
history.

Research:
1. Oracle publishes OCI wins heavily — check oracle.com/customers and Oracle press for the company.
2. Searches: "{company}" OCI / "{company}" "Oracle Cloud Infrastructure" /
   "{company}" EBS "lift and shift" / "{company}" Oracle Cloud migration
3. Job reqs: "OCI", "Oracle Cloud Infrastructure Architect" at the company.
4. Earnings/IR mentions of migrating to Oracle Cloud.{migration_context}

details to extract: {{"on_oci": true|false, "ebs_confirmed": true|false, "cloud": "<OCI|AWS|Azure|''>", "note": "<one line>"}}

Scoring: confirmed EBS running on OCI = 80-95. Strong indication (OCI and EBS both present, the link
implied but not stated) = 60-75.
DISQUALIFIERS (report found=false): lifted to AWS or Azure instead (still note it in details — it is
a different pitch); a full re-platform OFF EBS to Fusion (that is the ERP-migration trigger, not this
one — if the source says Fusion, not EBS-on-OCI, report found=false with a clear note); OCI usage
unrelated to EBS (analytics, a website).
""",
    },
    "ebs_performance": {
        "label": "EBS performance",
        "requires_ebs": True,
        "max_searches": 3,  # proxy-only trigger; rarely runs (EBS pre-condition)
        "max_found_score": 75,  # deterministic backstop for the brief's "cap the score at 75"
        "brief": """\
# Trigger: EBS performance pain (month-end close, table locks, batch overruns)
Pre-condition, already verified: our deterministic technographic scan detected Oracle E-Business
Suite at this company{tech_context}.
Why it matters (context only): two decades of accumulated transactional data bloats the production
EBS database — commit failures, table locks, batch jobs overrunning, the month-end close dragging,
and capital spend on hardware just to keep the same processes running.

This is a PROXY-SIGNAL trigger — direct public evidence is rare. Research (more proxies = more likely
pain):
1. Company size / age: a large, long-established enterprise on EBS = higher likelihood (data volumes
   scale up year over year).
2. High-transaction-volume industry (retail, manufacturing, distribution, logistics, telco).
3. Job reqs mentioning tuning: "{company}" Oracle "performance tuning" / "{company}" "Apps DBA" —
   reqs citing "batch optimization", "database performance".
4. Any rare public mention of close delays or system performance projects.

details to extract: {{"industry": "<their industry>", "proxy_signals": ["<one line per proxy actually found>"]}}

Scoring: proxy-only trigger — cap the score at 75. Size + industry fit alone = 30-50; plus a
tuning/Apps-DBA job req or a performance-project mention = 55-75.
DISQUALIFIERS (report found=false): small / low-transaction-volume org; recently modernized with
performance already addressed.
""",
    },
}

# Wave 1 runs concurrently; wave 2 sees wave 1's verdicts (cross-references).
TRIGGER_WAVES = (("ma_carveout", "erp_migration", "ebs_performance"),
                 ("license_audit", "ebs_oci"))


def enabled_triggers(triggers=None):
    """Trigger ids to research, in canonical order: the explicit `triggers`
    argument, else NEWS_TRIGGERS (comma ids), else all. Unknown ids raise."""
    if triggers is None:
        raw = (os.environ.get("NEWS_TRIGGERS") or "").strip()
        triggers = [t.strip() for t in raw.split(",") if t.strip()] if raw else None
    if triggers is None:
        return list(TRIGGERS)
    unknown = [t for t in triggers if t not in TRIGGERS]
    if unknown:
        raise ValueError(f"unknown news trigger(s): {', '.join(unknown)} "
                         f"(valid: {', '.join(TRIGGERS)})")
    return [t for t in TRIGGERS if t in set(triggers)]


# ---- prompt assembly ---------------------------------------------------------
def _cross_context(trigger_id, prior):
    """The cross-reference line a wave-2 trigger appends about its upstream
    verdict. prior = the upstream trigger's classified verdict dict, or None
    when the upstream trigger didn't run. An upstream that errored or was
    skipped gets NO line — only a completed scan may claim "found nothing",
    otherwise a transport failure would wrongly suppress the searches."""
    if trigger_id == "license_audit":
        if prior and prior.get("found"):
            when = f", {prior['date']}" if prior.get("date") else ""
            return (f"\n   Cross-reference: our M&A carve-out scan already found: "
                    f"\"{prior.get('headline', '')}\"{when}. Count it as a signal — "
                    f"do not re-run those searches.")
        if prior is not None and not prior.get("error") and "skipped" not in prior:
            return ("\n   Our M&A carve-out scan of the last 90 days found nothing — "
                    "do not re-run those searches.")
        return ""
    if trigger_id == "ebs_oci":
        if prior and prior.get("found"):
            return (f"\n\nCross-reference: our ERP-migration scan already found: "
                    f"\"{prior.get('headline', '')}\". If that program replaces EBS entirely "
                    f"(a Fusion or S/4HANA re-platform), report found=false here.")
        return ""
    return ""


def build_system(max_searches):
    return SYSTEM_PROMPT.format(today=_utcnow().strftime("%B %d, %Y"),
                                max_searches=max_searches)


def build_user(trigger_id, company, domain, max_searches, tech_line=None, prior=None):
    """The user message for one trigger call. prior = upstream verdict for
    cross-referenced triggers (see TRIGGERS[..]['context_from'])."""
    spec = TRIGGERS[trigger_id]
    cutoff = (_utcnow() - timedelta(days=90)).strftime("%B %d, %Y")
    tech_context = f" (scan result: {tech_line})" if tech_line else ""
    brief = spec["brief"].format(
        company=company or domain, cutoff_date=cutoff, max_searches=max_searches,
        tech_context=tech_context,
        ma_context=_cross_context("license_audit", prior) if trigger_id == "license_audit" else "",
        migration_context=_cross_context("ebs_oci", prior) if trigger_id == "ebs_oci" else "",
    )
    head = f"Company: {company or domain} (email domain: {domain}).\n"
    if tech_line and not spec.get("requires_ebs"):
        head += f"Deterministic technographic scan of their estate (reliable): {tech_line}\n"
    return head + "\n" + brief + "\nResearch now and return only the JSON object."


# ---- verdict classification --------------------------------------------------
def classify_verdict(data):
    """Normalize one trigger's model output into a stored verdict: found bool,
    score clamped to 0-100 (forced 0 when not found), headline/summary/date/
    source_url strings, details dict. Tolerates junk types — a research verdict
    must never crash the scan."""
    if not isinstance(data, dict):
        data = {}
    found = bool(data.get("found"))
    try:
        score = int(round(float(data.get("score") or 0)))
    except (TypeError, ValueError):
        score = 50 if found else 0
    score = max(0, min(100, score))
    if not found:
        score = 0
    headline = _truncate(str(data.get("headline") or ""), HEADLINE_MAX)
    summary = _truncate(str(data.get("summary") or ""), 800)
    if found and not headline:
        headline = _truncate(summary, 160) or "signal found"
    details = data.get("details")
    if not isinstance(details, dict):
        details = {}
    return {"found": found, "score": score, "headline": headline, "summary": summary,
            "date": str(data.get("date") or "").strip()[:20],
            "source_url": str(data.get("source_url") or "").strip()[:500],
            "details": details}


def _found_sorted(trigger_results):
    """Found verdicts strongest-first with the canonical trigger order as the
    tiebreaker — the ONE sort every renderer (display line, composite prompt,
    HubSpot property) shares, so "strongest" never disagrees between surfaces."""
    order = list(TRIGGERS)
    found = [(tid, r) for tid, r in trigger_results.items()
             if isinstance(r, dict) and r.get("found")]
    found.sort(key=lambda x: (-(x[1].get("score") or 0),
                              order.index(x[0]) if x[0] in order else len(order)))
    return found


def _apply_verdict_guards(trigger_id, out, today=None):
    """Deterministic backstops applied after classify_verdict, mirroring each
    brief's hard rules so a drifting model can never seed a segment it
    shouldn't (the 2026-09 run showed prompt-only bars drift):
    - max_found_score (ebs_performance 75): clamp a found score to the cap.
    - recent_window_days (ma_carveout 90): a found verdict whose YYYY-MM date
      is strictly older than the cutoff month is outside the window -> not
      found (month granularity: the boundary month is conservatively kept).
    - min_found_score (license_audit 55): a weak found -> not found.
    Downgrades keep the evidence: the original score/headline move losslessly
    into details, and the summary gets only a short bracketed marker."""
    spec = TRIGGERS[trigger_id]

    def _downgrade(key, marker):
        out["details"] = {**(out.get("details") or {}),
                          key: {"score": out.get("score"), "headline": out.get("headline")}}
        out["summary"] = _truncate(f"{marker} {out.get('summary') or ''}", 800)
        out["found"], out["score"], out["headline"] = False, 0, ""

    cap = spec.get("max_found_score")
    if cap and out.get("found") and (out.get("score") or 0) > cap:
        out["score"] = cap
    window = spec.get("recent_window_days")
    if window and out.get("found") and out.get("date"):
        try:
            y, m = int(str(out["date"])[:4]), int(str(out["date"])[5:7])
            cut = (today or _utcnow()) - timedelta(days=window)
            if (y, m) < (cut.year, cut.month):
                _downgrade("outside_window",
                           f"[event dated {out['date']}, outside the {window}-day window]")
                return
        except (ValueError, TypeError):
            pass
    floor = spec.get("min_found_score")
    if floor and out.get("found") and (out.get("score") or 0) < floor:
        _downgrade("below_found_bar",
                   f"[scored {out.get('score')}, below the {floor}-point found bar]")


def format_line(trigger_results):
    """The news_signals display line: found triggers, score descending, as
    '<label> <score>: <headline>' joined with ' · '; NO_NEWS when none found.
    trigger_results = {id: verdict-or-skip dict} (errored/skipped entries are
    simply not found)."""
    found = _found_sorted(trigger_results)
    if not found:
        return NO_NEWS
    parts = []
    for tid, r in found:
        label = TRIGGERS[tid]["label"]
        headline = (r.get("headline") or "").strip()
        if len(headline) > LINE_HEADLINE_MAX:
            headline = headline[:LINE_HEADLINE_MAX - 1].rstrip() + "…"
        parts.append(f"{label} {r.get('score', 0)}: {headline}" if headline
                     else f"{label} {r.get('score', 0)}")
    return " · ".join(parts)


# ---- composite signal (the account's top-line "Signal") ----------------------
# One extra no-web-search call after a scan that FOUND at least one trigger:
# synthesizes the verdicts (+ tech/hiring context) into the best outreach angle
# and stores it via upsert_signal, so the Signals table/drawer "Signal" field —
# empty for scan-created rows — carries a human-readable composite.
# NEWS_COMPOSITE_SIGNAL=0 disables.
COMPOSITE_SYSTEM = """\
You are a B2B sales-research synthesizer for Value Global's ERP Data Retirement offering (Oracle EBS
archiving/retirement on the InfoCorvus ROAD platform). Today is {today}. You are given one account's
research: the ERP-trigger verdicts a web-research pass already produced, plus deterministic
technographic and hiring scans. Write the account's SIGNAL — the composite best outreach angle.

Rules:
- 2-3 plain sentences, max ~90 words. No markdown, no preamble, no headings.
- Lead with the strongest trigger (what happened and when), then connect it to the ERP
  data-retirement need it creates. Weave in a second trigger or the detected ERP stack only when it
  sharpens the angle — never list everything.
- Use ONLY the facts given. Never invent or upgrade a weak signal. Judge every event date relative
  to today — an event in the past is described as past.
- Plain factual research prose — no advice, no meta-commentary about outreach or angles, no second
  person. This text is reused verbatim as cached research context by copy generation, so it must
  read as a neutral account-research summary: what happened, when, and the data-retirement
  implication."""


def build_composite_user(company, domain, trigger_results, tech_line=None, hiring_line=None):
    """The user message for the composite-signal call: found verdicts score-desc
    plus the deterministic scan context."""
    found = _found_sorted(trigger_results)
    lines = [f"Company: {company or domain} (email domain: {domain})",
             f"Detected tech stack: {tech_line or '(not scanned)'}"]
    if hiring_line:
        lines.append(f"Hiring scan: {hiring_line}")
    lines.append("\nFound ERP triggers (strongest first):")
    for tid, r in found:
        when = f" [{r['date']}]" if r.get("date") else ""
        lines.append(f"- {TRIGGERS[tid]['label']} (score {r.get('score', 0)}){when}: "
                     f"{r.get('headline', '')}\n  {r.get('summary', '')}")
    lines.append("\nWrite the composite signal now — plain text only.")
    return "\n".join(lines)


def compose_signal(company, domain, trigger_results, tech_line=None, hiring_line=None):
    """Synthesize the composite signal. Returns (text, error) — never raises;
    (None, <error>) on failure. Failures and empty/truncated outputs are
    stderr-logged (the tech/hiring write-back house pattern) so a
    systematically failing composite is visible in a backfill's output."""
    try:
        model = (os.environ.get("NEWS_COMPOSITE_MODEL") or "").strip() or None
        res = _client(model).complete(
            COMPOSITE_SYSTEM.format(today=_utcnow().strftime("%B %d, %Y")),
            build_composite_user(company, domain, trigger_results,
                                 tech_line=tech_line, hiring_line=hiring_line),
            max_tokens=400, timeout=120,
        )
        if res.get("stop_reason") == "max_tokens":
            # A mid-sentence fragment must never become the account's signal.
            log(f"composite for {domain}: output truncated at max_tokens — not stored")
            return None, "output truncated at max_tokens"
        text = _truncate(" ".join((res.get("text") or "").split()).strip(), 700)
        if not text:
            log(f"composite for {domain}: model returned empty output")
            return None, "empty model output"
        return text, None
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"[:300]
        log(f"composite for {domain} failed: {err}")
        return None, err


def _ebs_detected(row):
    """Did the tech scan detect Oracle E-Business Suite on this account?
    True/False from a completed scan; None when no tech scan has run (or the
    row is missing) — the ebs_performance pre-condition then reports 'tech
    scan not run' instead of a hard no."""
    if not row or not row.get("tech_checked_at"):
        return None
    try:
        detections = (json.loads(row.get("tech_detail") or "{}") or {}).get("detections") or []
    except (ValueError, TypeError):
        detections = []
    if any((d or {}).get("vendor_id") == "oracle_ebs" for d in detections):
        return True
    # legacy rows with no parseable detail: fall back to the formatted line
    if not detections and "e-business" in (row.get("tech_signals") or "").lower():
        return True
    return False


# ---- research ---------------------------------------------------------------
_CLIENTS = {}
_CLIENT_LOCK = threading.Lock()


def _client(model=None):
    """Lazy per-model AnthropicClient cache. model=None resolves NEWS_MODEL →
    CLAUDE_MODEL → the client default; a per-trigger model (see _trigger_model)
    gets its own cached instance so one scan can mix model tiers."""
    key = (model or "").strip() or (os.environ.get("NEWS_MODEL") or "").strip() or None
    with _CLIENT_LOCK:
        if key not in _CLIENTS:
            if str(AI_SDR_SCRIPTS) not in sys.path:
                sys.path.insert(0, str(AI_SDR_SCRIPTS))
            from anthropic_client import AnthropicClient  # noqa: E402 — lazy: needs API key
            _CLIENTS[key] = AnthropicClient(model=key)
        return _CLIENTS[key]


def _trigger_model(trigger_id):
    """Model for one trigger's research call: NEWS_MODEL_<TRIGGER_ID> env (e.g.
    NEWS_MODEL_LICENSE_AUDIT=claude-haiku-4-5 — set per trigger to test cheaper
    tiers, no code change) → TRIGGERS[..]["model"] → None (the _client default
    chain, NEWS_MODEL → CLAUDE_MODEL)."""
    return ((os.environ.get(f"NEWS_MODEL_{trigger_id.upper()}") or "").strip()
            or TRIGGERS[trigger_id].get("model") or None)


def _usage_compact(usage):
    """The billing-relevant integers from a Messages usage object, including
    the billed web-search request count (usage.server_tool_use — the
    authoritative number behind the ~$10/1k search fee; the block-count
    `web_searches` field is only a proxy). Note: token counts are RAW — batch
    rows bill at 50% of them (detail.batched marks those)."""
    keep = ("input_tokens", "output_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens")
    out = {k: usage[k] for k in keep if isinstance((usage or {}).get(k), int)}
    stu = (usage or {}).get("server_tool_use")
    if isinstance(stu, dict) and isinstance(stu.get("web_search_requests"), int):
        out["web_search_requests"] = stu["web_search_requests"]
    return out


def _finish_verdict(trigger_id, out, res):
    """Classify one completed Messages response (sync or batch result) into the
    verdict dict `out` in place: search/usage metadata, JSON verdict, and the
    deterministic verdict guards (found floor / score cap / recency window).
    Raises on unparseable output — callers wrap."""
    out["web_searches"] = res.get("web_search_count", 0)
    usage = _usage_compact(res.get("usage"))
    if usage:
        out["usage"] = usage
    out.update(classify_verdict(extract_json_lazy(res["text"])))
    _apply_verdict_guards(trigger_id, out)


def extract_json_lazy(text):
    if str(AI_SDR_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(AI_SDR_SCRIPTS))
    from anthropic_client import extract_json  # noqa: E402
    return extract_json(text)


def _blank_verdict(trigger_id, model=None):
    out = {"label": TRIGGERS[trigger_id]["label"], "found": False, "score": 0,
           "headline": "", "summary": "", "date": "", "source_url": "",
           "details": {}, "error": None, "web_searches": 0, "duration_ms": 0}
    if model:
        out["model"] = model
    return out


def _research_trigger(trigger_id, company, domain, max_searches, tech_line=None, prior=None):
    """One trigger = one Messages call with web search. Returns the classified
    verdict plus call metadata; on failure a verdict-shaped dict with `error`
    set (never raises)."""
    if str(AI_SDR_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(AI_SDR_SCRIPTS))
    from anthropic_client import AnthropicError, AnthropicJSONError  # noqa: E402
    t0 = time.monotonic()
    client = None
    out = _blank_verdict(trigger_id)
    try:
        client = _client(_trigger_model(trigger_id))
        out["model"] = client.model
        res = client.complete(
            build_system(max_searches),
            build_user(trigger_id, company, domain, max_searches,
                       tech_line=tech_line, prior=prior),
            use_web_search=True, max_web_searches=max_searches,
            max_tokens=MAX_TOKENS, timeout=CALL_TIMEOUT,
        )
        _finish_verdict(trigger_id, out, res)
    except (AnthropicError, AnthropicJSONError) as exc:
        out["error"] = str(exc)[:300]
    except Exception as exc:  # noqa: BLE001 — one trigger never kills the scan
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    out["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return out


def detect_domain(domain, company=None, triggers=None, tech_line=None, ebs=None):
    """Pure research (no DB): run the enabled triggers in two waves. Returns
    {formatted, triggers, error, found_count, web_searches, duration_ms,
    model}. `error` is set — and formatted is None — ONLY when every researched
    trigger errored (a partial scan is still a definitive answer). `ebs` is the
    _ebs_detected pre-condition (True/False/None) for ebs_performance."""
    ok, reason = news_available()
    if not ok:
        raise RuntimeError(f"news research unavailable: {reason}")
    host = _clean_domain(domain)
    if not host:
        return {"formatted": None, "triggers": {}, "error": f"invalid domain: {domain!r}",
                "found_count": 0, "web_searches": 0, "duration_ms": 0, "model": None}

    wanted = enabled_triggers(triggers)
    t0 = time.monotonic()
    results = {}

    def _skip(tid):
        if tid != "ebs_performance":
            return None
        if ebs is None:
            return "tech scan has not run (Oracle EBS pre-condition unknown)"
        if not ebs:
            return "Oracle EBS not detected by the tech scan"
        return None

    for wave in TRIGGER_WAVES:
        runnable = []
        for tid in wave:
            if tid not in wanted:
                continue
            reason = _skip(tid)
            if reason:
                results[tid] = {"label": TRIGGERS[tid]["label"], "skipped": reason}
            else:
                runnable.append(tid)
        if not runnable:
            continue
        with ThreadPoolExecutor(max_workers=len(runnable)) as ex:
            futures = {
                tid: ex.submit(_research_trigger, tid, company, host, _max_searches(tid),
                               tech_line=tech_line,
                               prior=results.get(TRIGGERS[tid].get("context_from")))
                for tid in runnable
            }
            for tid, fut in futures.items():
                results[tid] = fut.result()

    # re-key in canonical order so detail JSON (and the drawer) reads stably
    results = {tid: results[tid] for tid in TRIGGERS if tid in results}
    researched = [r for r in results.values() if "skipped" not in r]
    errors = [r for r in researched if r.get("error")]
    error = None
    formatted = format_line(results)
    if researched and len(errors) == len(researched):
        error = "; ".join(f"{r['label']}: {r['error']}" for r in errors)[:500]
        formatted = None

    model = None
    try:
        model = _client().model if _CLIENTS else None
    except Exception:  # noqa: BLE001
        pass
    return {"formatted": formatted, "triggers": results, "error": error,
            "found_count": sum(1 for r in results.values() if r.get("found")),
            "web_searches": sum(r.get("web_searches", 0) for r in researched),
            "duration_ms": int((time.monotonic() - t0) * 1000), "model": model}


# ---- store + orchestrate -----------------------------------------------------
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


def _db():
    global _SCHEMA_READY
    conn = db.connect()
    if not _SCHEMA_READY:
        with _SCHEMA_LOCK:
            if not _SCHEMA_READY:
                db.init_schema(conn)  # guarantees the news_* columns exist
                _SCHEMA_READY = True
    return conn


def _found_count(news_detail):
    try:
        triggers = (json.loads(news_detail or "{}") or {}).get("triggers") or {}
        return sum(1 for r in triggers.values() if (r or {}).get("found"))
    except (ValueError, TypeError):
        return 0


def detect_and_store(domain, company=None, force=False, hubspot=None, triggers=None):
    """Cache-aware research for one domain: skip when the last SUCCESSFUL scan
    is younger than NEWS_REFRESH_DAYS (failed scans always retry), else research
    the triggers, upsert the account_signals row, and best-effort write the
    found-trigger lines to HubSpot. hubspot=None follows NEWS_HUBSPOT_WRITEBACK;
    True/False overrides."""
    ok, reason = news_available()
    if not ok:
        raise RuntimeError(f"news research unavailable: {reason}")
    host = _clean_domain(domain)
    if not host:
        raise ValueError(f"invalid domain: {domain!r}")

    conn = _db()
    try:
        row = db.get_signal(conn, host)
        refresh_days = _env_float("NEWS_REFRESH_DAYS", 30)
        if (row and not force and row.get("news_signals") is not None
                and db.news_fresh(row, days=refresh_days)):
            return {"domain": host, "skipped": True,
                    "news_signals": row.get("news_signals"),
                    "news_error": row.get("news_error"),
                    "news_checked_at": row.get("news_checked_at"),
                    "found_count": _found_count(row.get("news_detail")),
                    "hubspot": None}
        if not company:
            company = (row or {}).get("company_name")
    finally:
        conn.close()

    res = detect_domain(host, company=company, triggers=triggers,
                        tech_line=(row or {}).get("tech_signals"),
                        ebs=_ebs_detected(row))
    return _store_scan(host, company, res, hubspot)


def _store_scan(host, company, res, hubspot):
    """Persist one completed scan `res` (the detect_domain shape) — HubSpot
    write-back, composite signal, detail JSON, upserts. Shared by the sync
    path (detect_and_store) and the batched backfill; network calls happen
    BEFORE any DB connection is opened. The row is re-read here (never trusted
    from before the scan): research blocks for minutes sync and hours batched."""
    hs = None
    if res["formatted"] is not None:
        enabled = _flag("NEWS_HUBSPOT_WRITEBACK", True) if hubspot is None else bool(hubspot)
        hs = hubspot_writeback(host, res["triggers"]) if enabled else {"ok": False, "reason": "disabled"}

    # Re-read the row: the scan blocked for minutes, so the pre-scan snapshot
    # is stale — concurrent tech/hiring tails may have landed context the
    # composite should use.
    conn = _db()
    try:
        row = db.get_signal(conn, host)
    finally:
        conn.close()

    # Composite signal (network call — like the write-back, NEVER hold a DB
    # connection across it). Only when the scan actually found something; a
    # found_count of 0 must never blank an existing signal.
    composite, comp_err = None, None
    if res["found_count"] > 0 and _flag("NEWS_COMPOSITE_SIGNAL", True):
        composite, comp_err = compose_signal(
            company, host, res["triggers"],
            tech_line=(row or {}).get("tech_signals"),
            hiring_line=(row or {}).get("hiring_signals"))

    detail = {
        "triggers": res["triggers"],
        "found_count": res["found_count"],
        "web_searches": res["web_searches"],
        "duration_ms": res["duration_ms"],
        "model": res["model"],
        # Effective per-trigger budgets, RESEARCHED triggers only (skipped ones
        # made zero calls), env override + clamp included — one shape always.
        "max_searches": {t: _max_searches(t) for t, r in res["triggers"].items()
                         if t in TRIGGERS and isinstance(r, dict) and "skipped" not in r},
        "hubspot": hs,
    }
    if res.get("batched"):
        detail["batched"] = True
    conn = _db()
    try:
        # The composite write goes FIRST (fill-only, never clobbers a fresh
        # generation-researched signal, never touches company_name) so the
        # detail the news upsert serializes records what actually happened.
        stored_composite = False
        if composite:
            try:
                stored_composite = db.upsert_composite_signal(
                    conn, host, composite, model=res["model"],
                    has_recent=any(isinstance(r, dict) and r.get("found")
                                   for t, r in res["triggers"].items()
                                   if t != "ebs_performance"))
            except Exception as exc:  # noqa: BLE001 — the news upsert must still land
                comp_err = f"store failed: {exc}"[:300]
                log(f"composite store for {host} failed: {exc}")
        if res["found_count"] > 0 and _flag("NEWS_COMPOSITE_SIGNAL", True):
            detail["composite"] = {"ok": composite is not None and comp_err is None,
                                   "stored": stored_composite, "error": comp_err}
        db.upsert_news_signals(conn, host, res["formatted"],
                               news_detail=json.dumps(detail, ensure_ascii=False),
                               news_error=res["error"], company_name=company)
        stored = db.get_signal(conn, host) or {}
    finally:
        conn.close()

    return {"domain": host, "skipped": False, "news_signals": res["formatted"],
            "news_error": res["error"], "news_checked_at": stored.get("news_checked_at"),
            "found_count": res["found_count"], "hubspot": hs}


# ---- HubSpot write-back (best-effort; never raises) --------------------------
_HS_LOCK = threading.Lock()
_HS_CLIENT = None
_HS_PROPERTY_READY = False


def property_value(trigger_results):
    """The erp_news_signals company-property value: one line per found trigger
    (score, headline, date, source URL), or the NO_NEWS literal."""
    found = _found_sorted(trigger_results)
    if not found:
        return NO_NEWS
    lines = []
    for tid, r in found:
        line = f"{TRIGGERS[tid]['label']} ({r.get('score', 0)}): {r.get('headline', '')}"
        if r.get("date"):
            line += f" [{r['date']}]"
        if r.get("source_url"):
            line += f" — {r['source_url']}"
        lines.append(line)
    return "\n".join(lines)[:65000]


def hubspot_writeback(domain, trigger_results):
    global _HS_CLIENT, _HS_PROPERTY_READY
    try:
        if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
            return {"ok": False, "reason": "no_token"}
        import hubspot_client as hc
        with _HS_LOCK:
            if _HS_CLIENT is None:
                _HS_CLIENT = hc.HubSpotClient()
            client = _HS_CLIENT
            if not _HS_PROPERTY_READY:
                client.ensure_company_property(NEWS_PROPERTY, NEWS_PROPERTY_LABEL)
                _HS_PROPERTY_READY = True
        company_id = client.find_company_id_by_domain(domain)
        if not company_id:
            return {"ok": False, "reason": "no_company"}
        client.update_company(company_id, {NEWS_PROPERTY: property_value(trigger_results)})
        return {"ok": True, "company_id": company_id}
    except Exception as exc:  # noqa: BLE001 — write-back must never fail a scan
        log(f"hubspot writeback failed for {domain}: {exc}")
        return {"ok": False, "error": str(exc)[:300]}


# ---- batched research (Message Batches API — 50% token cost) -----------------
# Bulk backfills run through /v1/messages/batches by default (NEWS_BATCH=0
# forces the old synchronous thread pool): tokens bill at half price, web-search
# fees are unchanged. The two-wave cross-reference design maps to two
# sequential batches; results usually land well under an hour. The drawer's
# single-account "Research news" stays synchronous (detect_and_store).
def _batch_verdict_from_result(tid, result, model):
    """One batch result line → the same verdict shape _research_trigger emits."""
    out = _blank_verdict(tid, model=model)
    try:
        if (result or {}).get("type") == "succeeded":
            if str(AI_SDR_SCRIPTS) not in sys.path:
                sys.path.insert(0, str(AI_SDR_SCRIPTS))
            from anthropic_client import parse_message  # noqa: E402
            _finish_verdict(tid, out, parse_message(result["message"]))
        else:
            err = (result or {}).get("error") or {}
            out["error"] = f"batch {((result or {}).get('type') or 'missing')}: " \
                           f"{json.dumps(err)[:220]}"
    except Exception as exc:  # noqa: BLE001 — one bad result never kills the run
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    return out


def _run_batch_wave(reqs, idx_map, phase, label):
    """Submit one wave's requests, poll to completion, and classify every line.
    Returns {custom_id: verdict}; a request the results file never mentions
    gets an errored verdict (never silently dropped)."""
    client = _client()
    batch = client.create_batch(reqs)
    bid = batch.get("id") or ""
    log(f"{label}: batch {bid} submitted ({len(reqs)} requests)")
    poll = max(5, _env_int("NEWS_BATCH_POLL_S", 20))
    deadline = time.monotonic() + max(300, _env_int("NEWS_BATCH_TIMEOUT_S", 14400))
    while batch.get("processing_status") != "ended":
        if time.monotonic() > deadline:
            raise RuntimeError(f"{label}: batch {bid} timed out "
                               f"(NEWS_BATCH_TIMEOUT_S) — cancel or retry later")
        time.sleep(poll)
        batch = client.get_batch(bid)
        counts = batch.get("request_counts") or {}
        finished = sum(counts.get(k, 0) for k in ("succeeded", "errored", "canceled", "expired"))
        if phase:
            try:
                phase(f"{label}: {finished}/{len(reqs)} requests done (batch {bid})")
            except Exception:  # noqa: BLE001 — phase is cosmetic
                pass
    verdicts = {}
    for line in client.get_batch_results(batch["results_url"]):
        cid = line.get("custom_id") or ""
        if cid in idx_map:
            tid, _plan, model = idx_map[cid]
            verdicts[cid] = _batch_verdict_from_result(tid, line.get("result"), model)
    for cid, (tid, _plan, model) in idx_map.items():
        if cid not in verdicts:
            v = _blank_verdict(tid, model=model)
            v["error"] = f"batch {bid}: no result line returned"
            verdicts[cid] = v
    return verdicts


def _backfill_batched(items, workers, force, hubspot, progress, phase, triggers):
    """The batched twin of the sync backfill loop: same freshness rule, same
    stored shapes, same summary. items = [(domain, company|None)]."""
    wanted = enabled_triggers(triggers)
    summary = {"total": len(items), "detected": 0, "skipped": 0, "errors": 0,
               "hubspot_ok": 0, "hubspot_missing": 0, "batched": True}
    done = [0]

    def _tick(domain, res):
        done[0] += 1
        if res.get("error_exc") or res.get("news_error"):
            summary["errors"] += 1
        elif res.get("skipped"):
            summary["skipped"] += 1
        else:
            summary["detected"] += 1
        hs = res.get("hubspot") or {}
        if hs.get("ok"):
            summary["hubspot_ok"] += 1
        elif hs.get("reason") == "no_company":
            summary["hubspot_missing"] += 1
        if progress:
            try:
                progress(done[0], len(items), domain, res)
            except Exception:  # noqa: BLE001 — progress is cosmetic
                pass

    # Plan pass: same freshness skip as detect_and_store, plus the row context
    # (tech line, hiring line, EBS pre-condition) every later step needs.
    refresh_days = _env_float("NEWS_REFRESH_DAYS", 30)
    plans = []
    conn = _db()
    try:
        for idx, (d, c) in enumerate(items):
            row = db.get_signal(conn, d)
            if (row and not force and row.get("news_signals") is not None
                    and db.news_fresh(row, days=refresh_days)):
                _tick(d, {"skipped": True})
                continue
            plans.append({"idx": idx, "domain": d,
                          "company": c or (row or {}).get("company_name"),
                          "row": row, "tech_line": (row or {}).get("tech_signals"),
                          "ebs": _ebs_detected(row), "results": {}})
    finally:
        conn.close()
    if not plans:
        return summary

    def _skip_reason(tid, plan):
        if tid != "ebs_performance":
            return None
        if plan["ebs"] is None:
            return "tech scan has not run (Oracle EBS pre-condition unknown)"
        if not plan["ebs"]:
            return "Oracle EBS not detected by the tech scan"
        return None

    for wave_no, wave in enumerate(TRIGGER_WAVES, start=1):
        reqs, idx_map = [], {}
        for plan in plans:
            for tid in wave:
                if tid not in wanted:
                    continue
                reason = _skip_reason(tid, plan)
                if reason:
                    plan["results"][tid] = {"label": TRIGGERS[tid]["label"], "skipped": reason}
                    continue
                ms = _max_searches(tid)
                client = _client(_trigger_model(tid))
                cid = f"{tid}:{plan['idx']}"  # custom_id caps at 64 chars — never the domain
                reqs.append({"custom_id": cid, "params": client.build_body(
                    build_system(ms),
                    build_user(tid, plan["company"], plan["domain"], ms,
                               tech_line=plan["tech_line"],
                               prior=plan["results"].get(TRIGGERS[tid].get("context_from"))),
                    use_web_search=True, max_web_searches=ms,
                    max_tokens=MAX_TOKENS, cache_ttl="1h")})
                idx_map[cid] = (tid, plan, client.model)
        if not reqs:
            continue
        verdicts = _run_batch_wave(reqs, idx_map, phase, f"wave {wave_no}")
        for cid, verdict in verdicts.items():
            tid, plan, _model = idx_map[cid]
            plan["results"][tid] = verdict

    # Assemble each domain's detect_domain-shaped result, then store — the
    # write-back/composite finalize is network-bound, so a small thread pool.
    default_model = None
    try:
        default_model = _client().model
    except Exception:  # noqa: BLE001
        pass

    def _finalize(plan):
        results = {tid: plan["results"][tid] for tid in TRIGGERS if tid in plan["results"]}
        researched = [r for r in results.values() if "skipped" not in r]
        errors = [r for r in researched if r.get("error")]
        error = None
        formatted = format_line(results)
        if researched and len(errors) == len(researched):
            error = "; ".join(f"{r['label']}: {r['error']}" for r in errors)[:500]
            formatted = None
        res = {"formatted": formatted, "triggers": results, "error": error,
               "found_count": sum(1 for r in results.values() if r.get("found")),
               "web_searches": sum(r.get("web_searches", 0) for r in researched),
               "duration_ms": None, "model": default_model, "batched": True}
        try:
            return plan["domain"], _store_scan(plan["domain"], plan["company"],
                                               plan["row"], res, hubspot)
        except Exception as exc:  # noqa: BLE001 — one bad domain never kills the run
            return plan["domain"], {"domain": plan["domain"], "error_exc": str(exc)[:300]}

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        for d, res in ex.map(_finalize, plans):
            _tick(d, res)
    return summary


# ---- bulk ---------------------------------------------------------------------
def backfill(domains=None, stale_days=None, limit=None, workers=2, force=False,
             hubspot=None, progress=None, triggers=None, batched=None, phase=None):
    """Research many domains. Default path: the Message Batches API (50% token
    cost; NEWS_BATCH=0 or batched=False forces the synchronous thread pool,
    which also handles runs smaller than NEWS_BATCH_MIN, default 3). Sync mode:
    ThreadPoolExecutor — keep workers small, each scan already fans out to up
    to 3 concurrent web-search calls, and every non-skipped scan is real API
    spend. domains=None pulls the never-scanned (plus stale, when stale_days is
    set) from account_signals. `phase` (optional) receives coarse batch-progress
    strings. Returns {total, detected, skipped, errors, hubspot_ok,
    hubspot_missing, batched?}."""
    ok, reason = news_available()
    if not ok:
        raise RuntimeError(f"news research unavailable: {reason}")

    if domains is None:
        conn = _db()
        try:
            rows = db.domains_missing_news(conn, stale_days=stale_days, limit=limit)
        finally:
            conn.close()
        items = [(r["domain"], r.get("company_name")) for r in rows]
    else:
        items = [(d, None) for d in (_clean_domain(x) for x in domains) if d]
        if limit:
            items = items[:int(limit)]

    summary = {"total": len(items), "detected": 0, "skipped": 0, "errors": 0,
               "hubspot_ok": 0, "hubspot_missing": 0}
    if not items:
        return summary

    use_batch = _flag("NEWS_BATCH", True) if batched is None else bool(batched)
    if use_batch and len(items) >= max(1, _env_int("NEWS_BATCH_MIN", 3)):
        return _backfill_batched(items, workers, force, hubspot, progress, phase, triggers)

    def work(item):
        d, c = item
        try:
            return d, detect_and_store(d, company=c, force=force, hubspot=hubspot,
                                       triggers=triggers)
        except Exception as exc:  # noqa: BLE001 — one bad domain never kills the run
            return d, {"domain": d, "error_exc": str(exc)[:300]}

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        for d, res in ex.map(work, items):
            done += 1
            if res.get("error_exc") or res.get("news_error"):
                summary["errors"] += 1
            elif res.get("skipped"):
                summary["skipped"] += 1
            else:
                summary["detected"] += 1
            hs = res.get("hubspot") or {}
            if hs.get("ok"):
                summary["hubspot_ok"] += 1
            elif hs.get("reason") == "no_company":
                summary["hubspot_missing"] += 1
            if progress:
                try:
                    progress(done, len(items), d, res)
                except Exception:  # noqa: BLE001 — progress is cosmetic
                    pass
    return summary


# ---- stored-data migrations (the guards/composite ship after 1,262 rows) ------
def refloor_stored(limit=None, dry_run=False):
    """Apply the deterministic verdict guards (found floor / score cap /
    recency window) to STORED news_detail verdicts — new-scan guards never
    touch rows already researched, so without this the ~105 weak license_audit
    verdicts keep seeding the segment until their refresh. DB-only, no API
    calls; news_checked_at is preserved (a correction, not a re-scan). The
    recency window is judged as of each row's news_checked_at, not today."""
    conn = _db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT domain, news_detail, news_checked_at FROM account_signals "
            "WHERE news_detail IS NOT NULL")]
    finally:
        conn.close()
    if limit:
        rows = rows[:int(limit)]
    updates = []
    for r in rows:
        try:
            detail = json.loads(r["news_detail"]) or {}
        except (ValueError, TypeError):
            continue
        triggers = detail.get("triggers") or {}
        checked = None
        try:
            checked = datetime.strptime(r.get("news_checked_at") or "",
                                        "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
        touched = False
        for tid, v in triggers.items():
            if tid in TRIGGERS and isinstance(v, dict) and v.get("found"):
                before = (v.get("found"), v.get("score"))
                _apply_verdict_guards(tid, v, today=checked)
                if (v.get("found"), v.get("score")) != before:
                    touched = True
        if not touched:
            continue
        detail["found_count"] = sum(1 for v in triggers.values()
                                    if isinstance(v, dict) and v.get("found"))
        updates.append((format_line(triggers),
                        json.dumps(detail, ensure_ascii=False), r["domain"]))
    if not dry_run and updates:
        conn = _db()
        try:
            for line, dj, dom in updates:
                db.update_news_verdicts(conn, dom, line, dj)
        finally:
            conn.close()
    for line, _dj, dom in updates[:20]:
        log(f"refloor {dom}: {line[:90]}")
    return {"scanned": len(rows), "changed": len(updates), "dry_run": bool(dry_run)}


def recompose_stored(limit=None, force=False):
    """Synthesize composites for rows whose STORED scan found >=1 trigger but
    whose signal column is empty or stale — the composite feature shipped
    after those rows were researched, and the cache-hit skip means they'd
    otherwise wait out NEWS_REFRESH_DAYS. One no-search API call per row
    (real spend — use --limit). Fill-only semantics, same as a live scan."""
    ok, reason = news_available()
    if not ok:
        raise RuntimeError(f"news research unavailable: {reason}")
    conn = _db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT domain, company_name, signal, researched_at, tech_signals, "
            "hiring_signals, news_detail FROM account_signals "
            "WHERE news_detail IS NOT NULL ORDER BY updated_at DESC")]
    finally:
        conn.close()
    summary = {"eligible": 0, "attempted": 0, "stored": 0, "skipped_fresh": 0, "errors": 0}
    for r in rows:
        try:
            triggers = (json.loads(r["news_detail"]) or {}).get("triggers") or {}
        except (ValueError, TypeError):
            continue
        if not any(isinstance(v, dict) and v.get("found") for v in triggers.values()):
            continue
        summary["eligible"] += 1
        if not force and (r.get("signal") or "").strip() and db.signal_fresh(r):
            summary["skipped_fresh"] += 1
            continue
        if limit and summary["attempted"] >= int(limit):
            break
        summary["attempted"] += 1
        text, err = compose_signal(r.get("company_name"), r["domain"], triggers,
                                   tech_line=r.get("tech_signals"),
                                   hiring_line=r.get("hiring_signals"))
        if not text:
            summary["errors"] += 1
            continue
        model = None
        try:
            model = _client().model
        except Exception:  # noqa: BLE001
            pass
        conn = _db()
        try:
            if db.upsert_composite_signal(
                    conn, r["domain"], text, model=model,
                    has_recent=any(isinstance(v, dict) and v.get("found")
                                   for t, v in triggers.items() if t != "ebs_performance")):
                summary["stored"] += 1
        finally:
            conn.close()
        log(f"[{summary['attempted']}] {r['domain']}: composed")
    return summary


# ---- offline self-test ---------------------------------------------------------
def _search_override_probe():
    """Exercise _news_search_override under controlled env, restoring whatever
    the ambient environment held (a set .env value must never fail — or be
    destroyed by — the offline gate)."""
    saved = os.environ.get("NEWS_MAX_SEARCHES")
    try:
        results = []
        for raw in ("0", "four", None):
            if raw is None:
                os.environ.pop("NEWS_MAX_SEARCHES", None)
            else:
                os.environ["NEWS_MAX_SEARCHES"] = raw
            results.append(_max_searches("erp_migration"))
        return results
    finally:
        if saved is None:
            os.environ.pop("NEWS_MAX_SEARCHES", None)
        else:
            os.environ["NEWS_MAX_SEARCHES"] = saved


_FIXTURE_FOUND = {"found": True, "score": 88, "headline": "Acme completes carve-out of FooCo unit",
                  "summary": "Announced August 2026; PE buyer named.", "date": "2026-08",
                  "source_url": "https://example.com/pr",
                  "details": {"deal_type": "divestiture", "entity": "FooCo", "close_date": "2026-12"}}
_FIXTURE_NOT_FOUND = {"found": False, "score": 0, "headline": "", "date": "",
                      "summary": "Only a 2024 acquisition; outside the 90-day window.",
                      "source_url": "", "details": {}}
_FIXTURE_JUNK = {"found": True, "score": "very high", "headline": None,
                 "summary": "x" * 2000, "details": "not-a-dict"}
_FIXTURE_TECH_EBS = {"tech_checked_at": "2026-09-01T00:00:00Z", "tech_signals": "ERP: Oracle E-Business Suite",
                     "tech_detail": json.dumps({"detections": [{"vendor_id": "oracle_ebs",
                                                                "vendor_name": "Oracle E-Business Suite"}]})}
_FIXTURE_TECH_FUSION = {"tech_checked_at": "2026-09-01T00:00:00Z", "tech_signals": "ERP: Oracle Fusion Cloud ERP",
                        "tech_detail": json.dumps({"detections": [{"vendor_id": "oracle_fusion_cloud_erp",
                                                                   "vendor_name": "Oracle Fusion Cloud ERP"}]})}


def self_test():
    """Offline check of the trigger config, prompt assembly, verdict
    classification, line formatting, and the EBS pre-condition (no network,
    no ANTHROPIC_API_KEY, no DB). Exit code 0/1."""
    wave_ids = [t for wave in TRIGGER_WAVES for t in wave]
    v_found = classify_verdict(_FIXTURE_FOUND)
    v_none = classify_verdict(_FIXTURE_NOT_FOUND)
    v_junk = classify_verdict(_FIXTURE_JUNK)
    v_over = classify_verdict({**_FIXTURE_FOUND, "score": 130})

    prompts = {}
    prompt_errors = []
    for tid in TRIGGERS:
        try:
            prompts[tid] = build_user(tid, "Acme Corp", "acme.com", 4,
                                      tech_line="ERP: Oracle E-Business Suite",
                                      prior=v_found)
        except Exception as exc:  # noqa: BLE001
            prompt_errors.append(f"{tid}: {exc}")

    # Per-trigger model + search-cap resolution (env saved/restored)
    saved = {k: os.environ.get(k) for k in ("NEWS_MODEL_MA_CARVEOUT", "NEWS_MAX_SEARCHES")}
    try:
        os.environ["NEWS_MODEL_MA_CARVEOUT"] = "claude-haiku-4-5"
        tm_env = _trigger_model("ma_carveout")
        tm_default = _trigger_model("erp_migration")
        os.environ["NEWS_MAX_SEARCHES"] = "2"
        ms_env = _max_searches("erp_migration")
        os.environ.pop("NEWS_MAX_SEARCHES")
        ms_spec = (_max_searches("ma_carveout"), _max_searches("erp_migration"))
    finally:
        for k, v in saved.items():
            (os.environ.__setitem__(k, v) if v is not None else os.environ.pop(k, None))

    # Batch result lines classify exactly like the sync path (offline: no key)
    bv_ok = _batch_verdict_from_result("ma_carveout", {"type": "succeeded", "message": {
        "content": [{"type": "text", "text": json.dumps(_FIXTURE_FOUND)}],
        "usage": {"input_tokens": 1200, "output_tokens": 150},
        "stop_reason": "end_turn"}}, "claude-sonnet-5")
    bv_floor = _batch_verdict_from_result("license_audit", {"type": "succeeded", "message": {
        "content": [{"type": "text", "text": json.dumps({**_FIXTURE_FOUND, "score": 40})}],
        "usage": {}, "stop_reason": "end_turn"}}, None)
    bv_err = _batch_verdict_from_result("ebs_oci", {"type": "errored",
                                                    "error": {"type": "api_error"}}, None)

    line = format_line({"ma_carveout": v_found,
                        "erp_migration": {"found": True, "score": 91, "headline": "Mid-implementation S/4HANA program"},
                        "license_audit": v_none,
                        "ebs_performance": {"label": "EBS performance", "skipped": "no EBS"}})
    xref_found = _cross_context("license_audit", v_found)
    xref_none = _cross_context("license_audit", v_none)
    xref_err = _cross_context("license_audit", {**v_none, "error": "HTTP 500: boom"})
    xref_skip = _cross_context("license_audit", {"label": "M&A carve-out", "skipped": "scoped out"})
    xref_mig = _cross_context("ebs_oci", {"found": True, "headline": "Moving to Fusion"})

    checks = [
        ("five triggers in canonical order",
         list(TRIGGERS) == ["ma_carveout", "erp_migration", "license_audit", "ebs_oci", "ebs_performance"]),
        ("waves partition the trigger set exactly",
         sorted(wave_ids) == sorted(TRIGGERS) and len(wave_ids) == len(set(wave_ids))),
        ("cross-referenced triggers run a wave after their upstream",
         all(TRIGGERS[t]["context_from"] in TRIGGER_WAVES[0]
             for t in TRIGGER_WAVES[1] if TRIGGERS[t].get("context_from"))),
        ("only ebs_performance carries the EBS pre-condition",
         [t for t in TRIGGERS if TRIGGERS[t].get("requires_ebs")] == ["ebs_performance"]),
        ("every trigger prompt formats cleanly", not prompt_errors),
        ("prompts carry the company and their key searches",
         all("Acme Corp" in p for p in prompts.values())
         and '"carve-out"' in prompts.get("ma_carveout", "")
         and "S/4HANA" in prompts.get("erp_migration", "")
         and "software asset management" in prompts.get("license_audit", "")
         and "Oracle Cloud Infrastructure" in prompts.get("ebs_oci", "")
         and "performance tuning" in prompts.get("ebs_performance", "")),
        ("system prompt formats with the JSON schema intact",
         '"found": true|false' in build_system(4)),
        ("found verdict passes through with its score",
         v_found["found"] and v_found["score"] == 88 and v_found["source_url"]),
        ("not-found verdict forces score 0", not v_none["found"] and v_none["score"] == 0),
        ("junk types are tolerated (score fallback, headline from summary, details dict)",
         v_junk["found"] and v_junk["score"] == 50 and v_junk["headline"]
         and isinstance(v_junk["details"], dict)),
        ("scores clamp to 100", v_over["score"] == 100),
        ("line orders found triggers by score, skips the rest",
         line.startswith("ERP migration 91:") and "M&A carve-out 88:" in line
         and "License audit" not in line and "EBS performance" not in line),
        ("empty scan formats as the NO_NEWS literal", format_line({}) == NO_NEWS),
        ("license_audit cross-ref quotes the found M&A headline",
         "carve-out of FooCo" in xref_found and "found nothing" in xref_none),
        ("an errored or skipped upstream never claims 'found nothing'",
         xref_err == "" and xref_skip == ""),
        ("ebs_oci cross-ref warns about a full re-platform", "found=false" in xref_mig),
        ("EBS pre-condition: detected via tech_detail", _ebs_detected(_FIXTURE_TECH_EBS) is True),
        ("EBS pre-condition: Fusion-only scan is not EBS", _ebs_detected(_FIXTURE_TECH_FUSION) is False),
        ("EBS pre-condition: no tech scan yet -> unknown", _ebs_detected(None) is None
         and _ebs_detected({"tech_checked_at": None}) is None),
        ("property value lists found triggers with source URLs",
         "https://example.com/pr" in property_value({"ma_carveout": v_found})
         and property_value({}) == NO_NEWS),
        ("composite system prompt formats with today's date",
         "Today is January 01, 2026" in COMPOSITE_SYSTEM.format(today="January 01, 2026")),
        ("composite user carries only FOUND verdicts plus the scan context",
         (lambda p: "Acme Corp" in p and "M&A carve-out" in p and "License audit" not in p
          and "ERP: Oracle E-Business Suite" in p)
         (build_composite_user("Acme Corp", "acme.com",
                               {"ma_carveout": v_found, "license_audit": v_none},
                               tech_line="ERP: Oracle E-Business Suite"))),
        ("system prompt warns against adopting a stale source's tense",
         "NEVER adopt a source's" in build_system(4)),
        ("license_audit carries the 55-point found floor",
         TRIGGERS["license_audit"].get("min_found_score") == 55
         and "Never report found=true with a score below 55" in TRIGGERS["license_audit"]["brief"]
         and not any(TRIGGERS[t].get("min_found_score") for t in TRIGGERS if t != "license_audit")),
        ("per-trigger model: env override wins, default falls through",
         tm_env == "claude-haiku-4-5" and tm_default is None),
        ("search caps: live-tuned spec values, NEWS_MAX_SEARCHES overrides",
         ms_spec == (3, 4) and ms_env == 2),
        ("batch result classifies like the sync path (usage + model captured)",
         bv_ok["found"] and bv_ok["score"] == 88 and bv_ok["model"] == "claude-sonnet-5"
         and bv_ok.get("usage", {}).get("input_tokens") == 1200),
        ("batch result honors the license_audit found floor",
         not bv_floor["found"] and bv_floor["score"] == 0
         and "below the 55-point found bar" in bv_floor["summary"]),
        ("errored batch line yields an error verdict, never found",
         not bv_err["found"] and (bv_err["error"] or "").startswith("batch errored")),
        ("found floor downgrades a weak verdict, evidence preserved in details",
         (lambda v: (_apply_verdict_guards("license_audit", v) or True)
          and not v["found"] and v["score"] == 0 and v["headline"] == ""
          and v["details"]["below_found_bar"] == {"score": 40, "headline": "Weak proxy"}
          and v["summary"].startswith("[scored 40, below the 55-point found bar]")
          and v["summary"].endswith("tail-evidence"))
         ({"found": True, "score": 40, "headline": "Weak proxy", "date": "",
           "summary": ("x" * 40 + " tail-evidence"), "details": {}})),
        ("recency window downgrades a stale ma_carveout date, keeps a current one",
         (lambda old, new: (_apply_verdict_guards("ma_carveout", old) or True)
          and (_apply_verdict_guards("ma_carveout", new) or True)
          and not old["found"] and "outside_window" in old["details"]
          and new["found"] and new["score"] == 80)
         ({"found": True, "score": 80, "headline": "Old deal", "date": "2024-01",
           "summary": "s", "details": {}},
          {"found": True, "score": 80, "headline": "Fresh deal",
           "date": _utcnow().strftime("%Y-%m"), "summary": "s", "details": {}})),
        ("ebs_performance found score clamps to the 75 cap",
         (lambda v: (_apply_verdict_guards("ebs_performance", v) or True)
          and v["found"] and v["score"] == 75)
         ({"found": True, "score": 92, "headline": "h", "date": "",
           "summary": "s", "details": {}})),
        ("found sort breaks score ties in canonical trigger order everywhere",
         [t for t, _ in _found_sorted({
             "ebs_oci": {"found": True, "score": 70},
             "ma_carveout": {"found": True, "score": 70},
             "license_audit": {"found": False, "score": 0}})]
         == ["ma_carveout", "ebs_oci"]),
        ("NEWS_MAX_SEARCHES: 0 clamps to 1, malformed is ignored, blank is unset",
         _search_override_probe() == [1, 4, 4]),
    ]
    failed_names = [name for name, passed in checks if not passed]
    for name, passed in checks:
        log(f"{'PASS' if passed else 'FAIL'}  {name}")
    if prompt_errors:
        for e in prompt_errors:
            log(f"  prompt error: {e}")
    log(f"line: {line}")
    print(json.dumps({"ok": not failed_names, "failed": failed_names, "line": line}))
    return 0 if not failed_names else 1


# ---- CLI ------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Company-news signal research (ERP triggers, web search)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--domain", help="research one domain (cache-aware; see --force)")
    mode.add_argument("--missing", action="store_true",
                      help="research every account_signals domain with no news scan yet")
    mode.add_argument("--self-test", action="store_true", dest="self_test",
                      help="offline config/classifier check (no network, no API key)")
    mode.add_argument("--refloor", action="store_true",
                      help="apply the deterministic verdict guards to STORED verdicts "
                           "(DB-only migration; freshness clocks preserved; see --dry-run)")
    mode.add_argument("--recompose", action="store_true",
                      help="synthesize composites for stored found-trigger rows with an "
                           "empty/stale signal (one API call per row — use --limit)")
    ap.add_argument("--company", help="company name to fill on a blank row (--domain only)")
    ap.add_argument("--triggers", help="comma-scoped trigger ids (default: all / NEWS_TRIGGERS)")
    ap.add_argument("--force", action="store_true", help="re-research even if fresh")
    ap.add_argument("--no-hubspot", action="store_true", help="skip the HubSpot write-back")
    ap.add_argument("--stale-days", type=int, default=None,
                    help="with --missing: also re-research scans older than N days")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--sync", action="store_true",
                    help="force the synchronous thread pool (skip the Message "
                         "Batches API and its 50%% token discount)")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="with --refloor: report what would change, write nothing")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())
    if args.refloor:
        summary = refloor_stored(limit=args.limit, dry_run=args.dry_run)
        log(f"refloor done: {summary}")
        print(json.dumps(summary))
        return
    if args.recompose:
        summary = recompose_stored(limit=args.limit, force=args.force)
        log(f"recompose done: {summary}")
        print(json.dumps(summary))
        return

    triggers = [t.strip() for t in args.triggers.split(",") if t.strip()] if args.triggers else None
    hubspot = False if args.no_hubspot else None
    if args.domain:
        log(f"researching {args.domain} ...")
        res = detect_and_store(args.domain, company=args.company, force=args.force,
                               hubspot=hubspot, triggers=triggers)
        log("skipped (fresh)" if res.get("skipped")
            else f"-> {res.get('news_signals') or res.get('news_error')}")
        print(json.dumps(res, ensure_ascii=False))
        return

    def progress(done, total, domain, res):
        status = ("error" if (res.get("error_exc") or res.get("news_error"))
                  else "skip" if res.get("skipped") else "ok")
        log(f"[{done}/{total}] {domain}: {status}")

    summary = backfill(stale_days=args.stale_days, limit=args.limit, workers=args.workers,
                       force=args.force, hubspot=hubspot, progress=progress,
                       triggers=triggers, batched=False if args.sync else None,
                       phase=lambda msg: log(msg))
    log(f"done: {summary}")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
