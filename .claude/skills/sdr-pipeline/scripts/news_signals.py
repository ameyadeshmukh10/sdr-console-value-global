"""Company-news signal research runner — module + CLI.

Web-researches the five Value Global ERP Data Retirement buying triggers for
one company (Anthropic Messages API + the server-side web_search tool — the
same research channel generate_batch.py uses) and stores the verdicts on the
`account_signals` row for that domain:

    news_signals    "M&A carve-out 85: Acme completes carve-out of FooCo · ERP
                    migration 70: hiring S/4HANA leads" (found triggers, score
                    descending; or the literal "No ERP news signals detected";
                    NULL only if the scan itself failed — every trigger errored)
    news_detail     JSON: {triggers: {id: verdict}, found_count, top,
                    web_searches, duration_ms, model, max_searches, hubspot}
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

Each trigger is one Messages call with web search (NEWS_MAX_SEARCHES per call,
default 4) returning a strict JSON verdict {found, score 0-100, headline,
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

Env knobs: NEWS_DETECT_ENABLED (post-batch tail hook), NEWS_REFRESH_DAYS (30),
NEWS_MAX_SEARCHES (4), NEWS_MODEL (defaults to CLAUDE_MODEL), NEWS_TRIGGERS
(comma-scoped trigger ids, default all), NEWS_HUBSPOT_WRITEBACK (1).

CLI (run_script conventions: progress lines on stderr, JSON summary as the
LAST stdout line):

    python3 news_signals.py --domain acme.com [--company "Acme"] [--force]
                            [--triggers ma_carveout,erp_migration] [--no-hubspot]
    python3 news_signals.py --missing [--stale-days N] [--limit N]
                            [--workers N] [--force] [--no-hubspot]
    python3 news_signals.py --self-test      # offline; no network, no key

Every non-skipped scan costs real Anthropic spend (up to 5 web-search calls),
so prefer --limit on a first bulk backfill.
"""

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
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
DISQUALIFIERS (report found=false): a migration COMPLETED more than a year ago with nothing still
running; generic "digital transformation" talk with no ERP program behind it.
""",
    },
    "license_audit": {
        "label": "License audit",
        "context_from": "ma_carveout",
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

Scoring: composite trigger. One weak signal alone = 30-50; two or more, or one strong direct signal
(public Oracle-spend commentary, a fresh SAM hire) = 55-80; direct evidence of an active or announced
Oracle audit = 80-95.
DISQUALIFIERS (report found=false): no EBS and no broader Oracle footprint; a company clearly
reducing Oracle to zero (already fully migrated off Oracle).
""",
    },
    "ebs_oci": {
        "label": "EBS on OCI",
        "context_from": "erp_migration",
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
    return SYSTEM_PROMPT.format(today=datetime.now().strftime("%B %d, %Y"),
                                max_searches=max_searches)


def build_user(trigger_id, company, domain, max_searches, tech_line=None, prior=None):
    """The user message for one trigger call. prior = upstream verdict for
    cross-referenced triggers (see TRIGGERS[..]['context_from'])."""
    spec = TRIGGERS[trigger_id]
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%B %d, %Y")
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


def format_line(trigger_results):
    """The news_signals display line: found triggers, score descending, as
    '<label> <score>: <headline>' joined with ' · '; NO_NEWS when none found.
    trigger_results = {id: verdict-or-skip dict} (errored/skipped entries are
    simply not found)."""
    found = [(tid, r) for tid, r in trigger_results.items() if r.get("found")]
    if not found:
        return NO_NEWS
    found.sort(key=lambda x: (-(x[1].get("score") or 0), list(TRIGGERS).index(x[0])))
    parts = []
    for tid, r in found:
        label = TRIGGERS[tid]["label"]
        headline = (r.get("headline") or "").strip()
        if len(headline) > LINE_HEADLINE_MAX:
            headline = headline[:LINE_HEADLINE_MAX - 1].rstrip() + "…"
        parts.append(f"{label} {r.get('score', 0)}: {headline}" if headline
                     else f"{label} {r.get('score', 0)}")
    return " · ".join(parts)


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
_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def _client():
    """Lazy shared AnthropicClient (NEWS_MODEL wins over CLAUDE_MODEL)."""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            if str(AI_SDR_SCRIPTS) not in sys.path:
                sys.path.insert(0, str(AI_SDR_SCRIPTS))
            from anthropic_client import AnthropicClient  # noqa: E402 — lazy: needs API key
            _CLIENT = AnthropicClient(model=(os.environ.get("NEWS_MODEL") or "").strip() or None)
        return _CLIENT


def _research_trigger(trigger_id, company, domain, max_searches, tech_line=None, prior=None):
    """One trigger = one Messages call with web search. Returns the classified
    verdict plus call metadata; on failure a verdict-shaped dict with `error`
    set (never raises)."""
    if str(AI_SDR_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(AI_SDR_SCRIPTS))
    from anthropic_client import extract_json, AnthropicError, AnthropicJSONError  # noqa: E402
    t0 = time.monotonic()
    out = {"label": TRIGGERS[trigger_id]["label"], "found": False, "score": 0,
           "headline": "", "summary": "", "date": "", "source_url": "",
           "details": {}, "error": None, "web_searches": 0, "duration_ms": 0}
    try:
        res = _client().complete(
            build_system(max_searches),
            build_user(trigger_id, company, domain, max_searches,
                       tech_line=tech_line, prior=prior),
            use_web_search=True, max_web_searches=max_searches,
            max_tokens=MAX_TOKENS, timeout=CALL_TIMEOUT,
        )
        out["web_searches"] = res.get("web_search_count", 0)
        out.update(classify_verdict(extract_json(res["text"])))
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

    max_searches = max(1, _env_int("NEWS_MAX_SEARCHES", 4))
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
                tid: ex.submit(_research_trigger, tid, company, host, max_searches,
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
        model = _CLIENT.model if _CLIENT else None
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

    # HubSpot write-back BEFORE the upsert (no DB connection held across the
    # HTTP call) so its outcome persists into news_detail for the drawer.
    hs = None
    if res["formatted"] is not None:
        enabled = _flag("NEWS_HUBSPOT_WRITEBACK", True) if hubspot is None else bool(hubspot)
        hs = hubspot_writeback(host, res["triggers"]) if enabled else {"ok": False, "reason": "disabled"}

    detail = {
        "triggers": res["triggers"],
        "found_count": res["found_count"],
        "web_searches": res["web_searches"],
        "duration_ms": res["duration_ms"],
        "model": res["model"],
        "max_searches": max(1, _env_int("NEWS_MAX_SEARCHES", 4)),
        "hubspot": hs,
    }
    conn = _db()
    try:
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
    found = [(tid, r) for tid, r in trigger_results.items() if r.get("found")]
    if not found:
        return NO_NEWS
    found.sort(key=lambda x: -(x[1].get("score") or 0))
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


# ---- bulk ---------------------------------------------------------------------
def backfill(domains=None, stale_days=None, limit=None, workers=2, force=False,
             hubspot=None, progress=None, triggers=None):
    """Research many domains (ThreadPoolExecutor; keep workers small — each
    scan already fans out to up to 3 concurrent web-search calls, and every
    non-skipped scan is real API spend). domains=None pulls the never-scanned
    (plus stale, when stale_days is set) from account_signals. Returns {total,
    detected, skipped, errors, hubspot_ok, hubspot_missing}."""
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


# ---- offline self-test ---------------------------------------------------------
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
    ap.add_argument("--company", help="company name to fill on a blank row (--domain only)")
    ap.add_argument("--triggers", help="comma-scoped trigger ids (default: all / NEWS_TRIGGERS)")
    ap.add_argument("--force", action="store_true", help="re-research even if fresh")
    ap.add_argument("--no-hubspot", action="store_true", help="skip the HubSpot write-back")
    ap.add_argument("--stale-days", type=int, default=None,
                    help="with --missing: also re-research scans older than N days")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

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
                       triggers=triggers)
    log(f"done: {summary}")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
