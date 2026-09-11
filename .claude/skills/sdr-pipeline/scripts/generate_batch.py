"""Generate SDR outbound copy for one batch via the Anthropic API.

Replicates what the `sdr-batch-runner` Claude Code sub-agent does, but as a
direct API call (stdlib urllib) so it can be triggered from the web UI. Per
contact: research a recent signal (server-side web_search), write the 4-touch
email + LinkedIn copy as strict JSON, validate against the SAME linter the
`ingest` step uses, retrying with the lint errors fed back.

Two entry points:
  - CLI:  python3 generate_batch.py <batch_id>     (generates, then runs ingest)
  - lib:  generate_batch(batch_id, progress_cb, cancel_event)  (used by the webui
          job worker; the caller runs `sdr_batches.py ingest` afterwards)

DB writes go through the existing `ingest`, so the read-only web API sees them.
"""

import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent                      # sdr-pipeline/scripts
SKILLS = HERE.parents[1]                                     # .claude/skills
sys.path.insert(0, str(HERE))                               # batch_db
sys.path.insert(0, str(SKILLS / "ai-sdr" / "scripts"))      # anthropic_client, lint_sequence

import batch_db as db                                        # noqa: E402
import lint_sequence as L                                    # noqa: E402
from anthropic_client import (                               # noqa: E402
    AnthropicClient, extract_json, parse_message, AnthropicError, AnthropicJSONError,
)

KNOWLEDGE_DIR = SKILLS / "ai-sdr" / "knowledge"
EXAMPLE = SKILLS / "ai-sdr" / "examples" / "icp-email-sequence.md"
SDR_BATCHES = HERE / "sdr_batches.py"
MAX_ATTEMPTS = 3
MAX_WORKERS = 4

# Messaging is UNIFORM across the buying group by client decision (2026-09
# kick-off): personas gate ICP entry and drive reporting, not copy variants.
# One framing, four keys, so persona routing keeps working end to end.
_VG_FRAMING = (
    "Frame the pain as decades of dormant transactional history riding on production "
    "infrastructure at full cost: the hardware treadmill, batch runs and month-end close "
    "slowing down, audit and retention obligations nobody wants to own. "
    "Gives: touch 1 the short POV read (send-over ask only), touch 2 the free Data "
    "Lifecycle Assessment, touch 3 a 20-minute call to set it up, touch 4 a breakup that "
    "leaves the read on the table. Write to the role in the title, not a seniority script."
)
PERSONA_FRAMING = {
    "erp-owner": _VG_FRAMING,
    "dba": _VG_FRAMING,
    "data-governance": _VG_FRAMING,
    "it-leadership": _VG_FRAMING,
}

# Shared research block. For this offer the anchor is the ROLE + the general pattern, never
# congrats-news: hunting for funding rounds or announcements is the wrong motion for a
# 20-year-old ERP conversation (the client's own message set retired it).
RESEARCH_BLOCK = """\
# Today's date
Today is {today}.

# Step 0 — the email domain is ground truth for WHO the company is
The contact block gives the company name from our CRM and the contact's email domain. CRM
names go stale (acquisitions, rebrands, mergers, junk values), but the domain is where the
email actually lands — trust it. You may use AT MOST ONE web search, and ONLY to verify which
company operates the domain when the stated name looks stale or wrong; write for the domain's
company under its current name. A personal/free email domain (gmail.com and the like) says
nothing about the employer — keep the stated company then.

# Step 1 — the anchor (no news hunting)
Do NOT search for funding rounds, executive hires, launches, or any congrats-news, and never
open on one. The anchor for every touch is:
1. The person's ROLE (verifiable from their title) — the question is put to that role.
2. The detected ERP platform, when the contact block provides one.
3. The general pattern about long-running ERP systems (the 10/20/70 story in the knowledge).

# The `signal` field must be auditable
Set `signal` to the anchor you used, e.g. "role anchor - Director of Enterprise Applications
on detected Oracle EBS" or "role anchor - IT leadership, no platform detected".
"""

# ---- Variant write rules (Step 3). Value Global's own guidance: A/B the CTA itself -------------
# ("send the POV" vs "does that match what you are seeing" vs the white-paper framing), same
# 4-touch structure in all three. Shared rules live in the knowledge base (icp-email.md).
_VG_SHARED_RULES = """\
Shared rules (every variant): 35-110 words per email (the approved set runs 45-105); short
paragraphs separated by a blank line; ONE idea and ONE ask per email; subject 1 is
"What's it costing to keep data nobody touches?"; every touch lands as its OWN standalone
email, never a threaded reply: subjects 2-4 are each a distinct plain subject line (no "RE:"
prefix, never reuse a subject) naming that touch's give (touch 2 the assessment, touch 3 the
20 minutes, touch 4 the goodbye); open with a question, never a claim; state the 10/20/70
pattern about long-running EBS systems in general, NEVER about the recipient's environment;
never open with "Congrats" or assert their ERP tenure; no meeting or assessment ask in touch
1; touch 2 carries the free Data Lifecycle Assessment (its five deliverables +
www.valueglobal.net/archiving-solutions); touch 3 is the 20-minute call ask with two windows
named in prose as weekday plus time of day only ("Tuesday morning or Thursday afternoon"),
never "this week", today, tomorrow, or a calendar date (the email sends days after it is
written, so dated windows go stale), and never a booking link; touch 4 is a breakup that
concedes timing and leaves the read on the table; never offer case studies or "how similar
companies handled it" (none exist yet); no em or en dashes; NO sign-off or trailing name; no
hype; no pricing; no production-access reassurance; at most two credibility stats per email;
the licensing rule from the knowledge applies.
"""

WRITE_RULES = {
    # VG baseline: the POV send-over ask ("want me to send it over?").
    "value-give": """\
# Step 3 — write the sequence (POV send-over)
Write a 4-touch cold EMAIL sequence plus 3 LinkedIn touches per the knowledge above.
Touch 1's ask is the POV send-over, modeled on approved Message 1: "If that is a live issue,
I have written a short POV on it... Want me to send it over?"

""" + _VG_SHARED_RULES,

    # Resonance check: the lighter A/B arm ("does that match what you are seeing?").
    "earn": """\
# Step 3 — write the sequence (resonance check)
Write a 4-touch cold EMAIL sequence plus 3 LinkedIn touches per the knowledge above.
Touch 1's ask is the resonance check, modeled on approved Message 2: "If that is on your
radar, I have a short write-up on it. Glad to share it. Does that match what you are seeing?"
The give is still the read; the ask is agreement with the pattern, which is even lighter than
a send-over.

""" + _VG_SHARED_RULES,

    # White-paper track (Track B): the give is framed as a white paper; Track B's follow-up
    # and breakup shapes.
    "show": """\
# Step 3 — write the sequence (white-paper track)
Write a 4-touch cold EMAIL sequence plus 3 LinkedIn touches per the knowledge above, on the
approved Track B shape. Touch 1's give is "the white paper" (same asset, named differently):
a two-minute read on what dormant ERP data costs and what companies do about it. Touch 2
describes what the white paper covers (how ERP data splits into active, aging and dormant;
why the cost of the dormant part compounds; why most teams leave it alone; what a governed
archive changes; the four answers companies reach for) and then offers the assessment. Touch 4
is Track B's breakup: "I will stop here rather than keep filling your inbox... if archiving
old ERP data lands on your desk later this year, or on someone else's... Reply any time and I
will send it."

""" + _VG_SHARED_RULES,
}
DEFAULT_VARIANT = "value-give"

OUTPUT_SCHEMA = """\
# Output
Return ONLY a single JSON object, no prose, no markdown code fences, matching EXACTLY this shape
(use \\n\\n for paragraph breaks inside bodies):

{
  "company": "<the company name you verified for the email domain (echo the given company if you did not research)>",
  "signal": "<the recent signal with its month/date, or 'no recent signal - <anchor>'>",
  "email": {
    "subject1": "...", "body1": "...",
    "subject2": "...", "body2": "...",
    "subject3": "...", "body3": "...",
    "subject4": "...", "body4": "..."
  },
  "linkedin": {
    "li_connect": "<=280 char connection note referencing the signal/anchor, no pitch",
    "li_msg1": "value-first give after connection",
    "li_msg2": "soft follow-up nudge + give"
  }
}
"""


def _today():
    now = datetime.now()
    return {"today": now.strftime("%B %d, %Y"), "year": now.year, "prev_year": now.year - 1}


# ----------------------------------------------------------------------------
# ERP Data Retirement trigger-anchored generation (Value Global segment flow).
# Contacts approved through a news-trigger segment carry variant "erp-trigger"
# + a `segment` (one of the five ERP trigger ids); their copy is written from
# the stored trigger VERDICT (news_signals.py research) + the sales play below
# — write-only, no web search: the research already happened at the account
# gate. Play problem/solution texts are the user-approved instruction sets.
# ----------------------------------------------------------------------------
ERP_VARIANT = "erp-trigger"

ERP_PLAYS = {
    "ma_carveout": {
        "label": "M&A carve-out",
        "problem": "When a company divests or acquires, the separated entity keeps running on the "
                   "parent's ERP through a 6-12 month Transition Services Agreement window. TSA data "
                   "is messy and commingled, nobody wants to keep running and paying for two ERPs, "
                   "and the clock to separate the data cleanly started the day the deal was announced.",
        "solution": "ERP Data Retirement helps the separated entity extract, segregate, and retire or "
                    "archive its data clean, so it can stand up independently on its own ERP instance "
                    "(or move to a new system) before the TSA window closes.",
        "opener": "the deal itself (the divestiture / carve-out / spinoff / acquisition found), tied "
                  "to the TSA and data-separation clock it starts",
    },
    "erp_migration": {
        "label": "ERP migration",
        "problem": "They are moving to a SaaS ERP (Fusion Cloud / S/4HANA), but the new platform only "
                   "accepts open items and recent data. Twenty years of legacy transactional history "
                   "will not load, yet it stays under audit, tax, and legal-retention obligations once "
                   "the old system is switched off, and no implementation partner wants to own that "
                   "question.",
        "solution": "ERP Data Retirement extracts and archives the full legacy history into a "
                    "compliant, independently queryable archive that lives outside the new ERP, so "
                    "they can retire the old system entirely, satisfy retention and audit "
                    "requirements, and go live on the new platform clean.",
        "opener": "their ERP modernization program (platform and stage when known), tied to the "
                  "legacy-history question the new platform will not take",
    },
    "license_audit": {
        "label": "License audit",
        "problem": "When Oracle's audit clock starts, the company is exposed on every database, "
                   "module, and environment where retired or dormant data still lives. Non-production "
                   "copies, old modules, and aged history all count toward the license and support "
                   "bill they are defending; the bigger the live footprint at audit time, the bigger "
                   "the liability.",
        "solution": "ERP Data Retirement compresses the Oracle footprint before the audit closes: "
                    "retiring inactive data and decommissioning dormant modules and environments so "
                    "there is less to defend and a smaller support renewal (decommissioned "
                    "environments genuinely come off the license bill; archiving alone does not cut "
                    "the license on a live one). They walk into the audit with a lean, justifiable "
                    "footprint instead of a sprawling one.",
        "opener": "the audit-exposure signal found (the M&A event, the SAM/procurement hire, or the "
                  "Oracle-spend commentary), tied to what still counts toward an audit",
    },
    "ebs_oci": {
        "label": "EBS on OCI",
        "problem": "They lifted EBS onto OCI, so the cloud bill now carries the full weight of 20 "
                   "years of history, most of which is dormant and never queried: compute, storage, "
                   "and backup scale with database size, and an environment kept alive mostly for "
                   "lookups still runs at full freight. They moved the problem to the cloud instead "
                   "of shrinking it.",
        "solution": "ERP Data Retirement retires the dormant history off the live OCI instance into "
                    "a low-cost governed archive, so they pay cloud rates only for the data they "
                    "actually use while retained history stays accessible for audit and legal hold. "
                    "Where an EBS environment exists mostly for compliance reads, full retirement "
                    "takes that environment's Oracle license and support to zero. Never attach a "
                    "percentage figure to any of this in cold copy.",
        "opener": "their EBS-on-OCI move (the lift-and-shift found), tied to hosting dormant "
                  "history at full freight",
    },
    "ebs_performance": {
        "label": "EBS performance",
        "problem": "Two decades of accumulated transactional data is bloating the production EBS "
                   "database. Large tables mean commit failures, table locks, and batch jobs that "
                   "overrun, dragging out the month-end close and forcing a cycle of hardware "
                   "upgrades and tuning just to keep the same processes running. The data they never "
                   "use is slowing down the data they do.",
        "solution": "ERP Data Retirement moves aged, inactive transactions out of the live tables "
                    "into an archive, shrinking the working set so month-end batch runs faster, locks "
                    "and commit failures drop, and the close tightens, recovering performance without "
                    "buying more hardware or re-architecting the system.",
        "opener": "running EBS at their scale and data age (plus any tuning/DBA proxy found), tied to "
                  "month-end close and batch performance; never claim to know their close is slow, "
                  "pose it as the question; never claim licensing savings here, the cost story for a "
                  "system they keep is hardware and infrastructure",
    },
}

ERP_SYSTEM = """\
You are an expert B2B SDR copywriter for Value Global's ERP Data Retirement service, delivered
on the ROAD platform by InfoCorvus.

# What we sell (ground truth; never invent beyond this)
Value Global's ERP Data Retirement extracts, archives, and retires legacy ERP data (Oracle
E-Business Suite is the sweet spot; PeopleSoft and JD Edwards secondary): aged and dormant
records move out of the live system into a low-cost, governed, independently queryable archive
(license-free PostgreSQL) that satisfies audit, tax, and legal-retention obligations. That lets
companies retire old systems entirely (taking that environment's Oracle license and support to
zero), separate data cleanly after M&A, and recover month-end performance, without losing
access to history. ROAD cannot archive out of Oracle Fusion or any cloud ERP; never imply it can.

# Your task
You are GIVEN a VERIFIED buying trigger researched for this account (with its source and date),
plus the sales play for that trigger. Do NOT search the web. Write a 4-touch cold EMAIL sequence
plus 3 LinkedIn touches anchored on that trigger.

# Write rules
- 35-110 words per email. Two or three short paragraphs separated by a blank line. ONE idea and
  ONE ask per email.
- Every touch lands in the inbox as its OWN standalone email, never a threaded reply. Write a
  DISTINCT subject line for each touch: no "RE:" or "Fwd:" prefix, never reuse a subject.
  Subject 1 names the trigger angle; subject 2 the assessment give; subject 3 the 20-minute
  ask; subject 4 the goodbye. Plain and specific, roughly 4 to 8 words, no clickbait.
- Touch 1, in this exact shape. Open on the trigger event (name what actually happened, with
  its month/date when given) and make the play's problem concrete for THEIR situation in one
  or two lines, in the voice of a senior consultant who has run this playbook talking to a
  peer: no consulting filler, no "defined window for separating and migrating" phrasing. Then
  state what that dormant or legacy data costs in plain operational terms, adapted from the
  play: storage plus performance, slower queries, heavier backups, delays cloning the database
  for dev and test, a close that drags. Then one bridge sentence: what if we showed you a way
  to move that dormant data off the live system while keeping it fully searchable and
  audit-ready? Close with the permission ask, using their real company name: "Can I send you
  some more details to show how <Company> could benefit?" NEVER end touch 1 on an open-ended
  question ("how are you thinking about...", "what's your plan for..."): the only questions
  allowed are the what-if bridge and the closed send-details ask. No meeting ask in touch 1.
- Touch 2: open with ONE fresh insight tied to the trigger event, a different angle than touch
  1's opener, never "just following up". Then offer the free Data Lifecycle Assessment by what
  they get: a data-growth profile, which modules are archive candidates, a quantified savings
  number, and a phased roadmap, with the link www.valueglobal.net/archiving-solutions. End
  with: "Can I send over a sample report so you can see exactly what that looks like for an
  environment like <Company>'s?" NEVER offer case studies, references, success stories, or
  "how similar companies/acquirers handled it": no case studies exist for this offering yet.
  No meeting ask in touch 2.
- Touch 3: a short, specific meeting ask: 20 minutes to walk through what the archive / separation /
  footprint compression would look like for their environment. Name two windows in prose as
  weekday plus time of day only ("Tuesday morning or Thursday afternoon work on my end").
  NEVER anchor a window to "this week", today, tomorrow, or a calendar date: the email goes
  out days or weeks after it is written, so a dated window reads stale. Never a booking link.
- Touch 4, the breakup, in this exact shape (45-75 words, three short paragraphs). First: "I
  will stop here rather than keep filling your inbox." Then leave the asset: if archiving old
  ERP data lands on their desk later this year, or on someone else's, the white paper is worth
  having, a two-minute read on what dormant ERP data costs and what companies do about it.
  Close: "Reply any time and I will send it. Happy to stay connected either way." No guilt,
  no new argument, no meeting ask.
- The trigger verdict and the play are your ONLY facts about this company (plus a detected-ERP
  line when provided). Never invent numbers, dates, names, or internal details.
- When a detected on-prem ERP platform is provided, name it once, naturally: an ERP that has run
  for 15 to 20 years accumulates very high data volumes, has archiving that history come up?
  Pattern plus question, never a claim about their environment; never mention scanning.
- The licensing rule: Oracle licensing is metered on cores and users, not data volume. Never
  claim archiving cuts the license for a system they keep running; license and support go to
  zero only when a retired environment is decommissioned. No percentage figures on licensing
  or footprint reduction in cold copy.
- Never assert facts about the recipient's environment; state patterns generally. Never open
  with "Congrats". At most two credibility stats per email. The only links allowed are
  www.valueglobal.net/archiving-solutions and the assessment demo page.
- No em or en dashes. NO sign-off or trailing name. No hype words (revolutionary, game-changing,
  cutting-edge, seamless, robust, supercharge, unlock, transform). Never write "purge",
  "estate", "AI-powered", or any production-access reassurance. Never put pricing in a cold
  email. Vary how each email opens; do not start them all the same way.
- LinkedIn: li_connect is a <=280-char connection note referencing the trigger (no pitch);
  li_msg1/li_msg2 are short, value-first follow-ups offering the read.
- Set the `signal` JSON field to the trigger finding with its month/date.

""" + OUTPUT_SCHEMA


def build_erp_user(contact, segment, verdict, tech_line=None, prior_issues=None):
    play = ERP_PLAYS[segment]
    details = verdict.get("details") or {}
    base = (
        f"Contact:\n"
        f"- name: {contact.get('first_name','')} {contact.get('last_name','')}\n"
        f"- title: {contact.get('title','')}\n"
        f"- company: {contact.get('company','')}\n"
        f"- email domain: {contact.get('domain') or db.email_domain(contact.get('email'))}\n\n"
        f"# Verified buying trigger: {play['label']} (research confidence {verdict.get('score', 0)}/100)\n"
        f"- What we found: {verdict.get('headline','')}"
        + (f" ({verdict['date']})" if verdict.get("date") else "") + "\n"
        + (f"- Evidence: {verdict['summary']}\n" if verdict.get("summary") else "")
        + (f"- Details: {json.dumps(details, ensure_ascii=False)}\n" if details else "")
        + (f"- Source: {verdict['source_url']}\n" if verdict.get("source_url") else "")
        + "\n# The sales play\n"
        f"- The problem this creates for them: {play['problem']}\n"
        f"- What ERP Data Retirement does about it: {play['solution']}\n"
        f"- Email 1 opens on: {play['opener']}\n"
    )
    mention = erp_mention_block(tech_line)
    if mention:
        base += "\n" + mention
    base += "\nUse the contact's first name in the copy. Write the sequence. Return only the JSON object."
    if prior_issues:
        base += ("\n\nYour previous attempt FAILED these checks:\n- "
                 + "\n- ".join(prior_issues)
                 + "\nFix ALL of them. Return only the corrected JSON object.")
    return base


def erp_signal_line(segment, verdict):
    """The auditable `signal` string stored on the asset (mirrors the cached-
    signal format): trigger label + headline + date."""
    play = ERP_PLAYS.get(segment) or {}
    line = f"{play.get('label', segment)}: {verdict.get('headline', '')}".strip().rstrip(":")
    if verdict.get("date"):
        line += f" ({verdict['date']})"
    return line


def lint_erp(email):
    """Structural checks for the ERP trigger sequence: length band, no dashes /
    sign-off / pricing / hype, soft question in touch 1, breakup in touch 4.
    No metric requirement — the trigger verdict is the anchor, not proof stats."""
    return _lint_short(email, lo=35, hi=110)


def _segment_verdict(domain, segment):
    """The stored news-trigger verdict for (domain, segment), or None when the
    account has no found verdict for it (e.g. a re-scan dropped it)."""
    if not domain or segment not in ERP_PLAYS:
        return None
    conn = db.connect()
    try:
        row = db.get_signal(conn, domain)
    finally:
        conn.close()
    try:
        triggers = (json.loads((row or {}).get("news_detail") or "{}") or {}).get("triggers") or {}
    except (ValueError, TypeError):
        return None
    v = triggers.get(segment)
    return v if isinstance(v, dict) and v.get("found") else None


def generate_contact_erp(contact, segment, verdict, client, write=True, tech_line=None):
    """Generate one trigger-anchored contact (write-only, no web search — the
    verdict IS the research). Same retry/lint/write contract as
    generate_contact; the asset carries variant 'erp-trigger'."""
    cid, persona = contact["contact_id"], contact.get("persona", "it-leadership")
    signal = erp_signal_line(segment, verdict)
    issues, last_asset = ["no output"], None
    cache_read = cache_write = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            res = client.complete(
                ERP_SYSTEM,
                build_erp_user(contact, segment, verdict, tech_line=tech_line,
                               prior_issues=None if attempt == 1 else issues),
                use_web_search=False, max_tokens=4096,
            )
            u = res.get("usage", {})
            cache_read += u.get("cache_read_input_tokens", 0) or 0
            cache_write += u.get("cache_creation_input_tokens", 0) or 0
        except AnthropicError as e:
            issues = [f"api error: {e}"]
            continue
        try:
            data = extract_json(res["text"])
        except AnthropicJSONError:
            issues = ["model did not return valid JSON"]
            continue

        last_asset = {
            "contact_id": cid, "persona": persona, "variant": ERP_VARIANT,
            "segment": segment, "signal": signal,
            "email": data.get("email", {}) or {},
            "linkedin": data.get("linkedin", {}) or {},
        }
        issues = lint_erp(last_asset["email"])
        if not issues:
            if write:
                _atomic_write(cid, last_asset)
            return {"status": "linted", "signal": signal, "asset": last_asset,
                    "company": contact.get("company", ""), "web_searches": 0,
                    "attempts": attempt, "issues": [],
                    "cache_read": cache_read, "cache_write": cache_write}

    if write and last_asset is not None:
        _atomic_write(cid, last_asset)
    return {"status": "failed", "asset": last_asset, "signal": signal,
            "company": contact.get("company", ""), "web_searches": 0,
            "attempts": MAX_ATTEMPTS, "issues": issues,
            "cache_read": cache_read, "cache_write": cache_write}


# Write-only preamble: any cached company anchor is provided, so no web search.
WRITE_PREAMBLE = """\
# Your task
Do NOT search the web. Write the sequence from the contact block below: the role anchor, the
detected ERP platform when provided, and any provided company anchor line. Never invent news.
"""

# Research-only task (UI force-refresh): verify the company + produce a one-line anchor.
# No news hunting — the anchor motion for this offer is role + platform + pattern.
RESEARCH_ONLY_TASK = """\
Today is {today}.

The company name we have may be stale — the DOMAIN is ground truth. If the given name does not
match the company operating the domain today (acquisition, rebrand, merger, junk CRM data),
research the domain's company instead and use ITS current name. (Personal/free email domains
are the exception — keep the given name.) Use at most 2 web searches, only to verify the
company and what it does.

Do NOT hunt for funding rounds, hires, or announcements. Set `signal` to a one-line anchor
describing the company (what it runs on / sells / operates) usable as background for an
ERP-archiving conversation, prefixed with "company anchor - ". Set has_recent_signal=false.

Return ONLY this JSON, no prose:
{{"company": "<the company name you verified for the domain>",
  "signal": "company anchor - <one line>", "has_recent_signal": false}}
"""


def load_knowledge():
    parts = []
    for fname in ("offer.md", "cta-offers.md", "icp-email.md"):
        parts.append((KNOWLEDGE_DIR / fname).read_text())
    if EXAMPLE.is_file():
        parts.append("# Reference sequence (emulate the shape, never copy specifics)\n\n"
                     + EXAMPLE.read_text())
    return "\n\n---\n\n".join(parts)


def build_system(knowledge, variant=DEFAULT_VARIANT, mode="research"):
    rules = WRITE_RULES.get(variant, WRITE_RULES[DEFAULT_VARIANT])
    if mode == "write":
        body = WRITE_PREAMBLE + "\n\n" + rules
    else:
        body = RESEARCH_BLOCK.format(**_today()) + "\n\n" + rules
    return (
        "You are an expert B2B SDR copywriter for Value Global's ERP Data Retirement service "
        "(delivered on the ROAD platform by InfoCorvus). Ground every claim in the knowledge base "
        "below; never invent product claims, numbers, or proof not in it.\n\n"
        + knowledge + "\n\n---\n\n" + body + "\n\n" + OUTPUT_SCHEMA
    )


# On-prem ERP names as they appear in the tech_signals display line. Fusion is
# deliberately absent: Fusion-only accounts are suppressed upstream, and a
# co-detected Fusion must never be named in copy (ROAD cannot archive from it).
_ERP_MENTION_NAMES = ("e-business suite", "peoplesoft", "jd edwards")


def erp_mention_block(tech_line):
    """The prompt block for a detected ON-PREM ERP (client rule, 2026-09
    kick-off: when EBS / PeopleSoft / JD Edwards is detected, the email SHOULD
    mention it). Returns '' when the line names no on-prem suite. This block
    must survive any future knowledge-base rework — it implements a confirmed
    client requirement, not template styling."""
    if not tech_line:
        return ""
    low = tech_line.lower()
    if not any(n in low for n in _ERP_MENTION_NAMES):
        return ""
    return (
        f"Detected ERP platform (deterministic portal/DNS scan; reliable): {tech_line}\n"
        "NAME this platform once, naturally, in the copy (touch 1 is the natural place). The "
        "angle, per the client: an ERP that has run for 15 to 20 years accumulates very high "
        "data volumes, so has archiving that history come up? State it as a general pattern "
        "about long-running systems plus a question, NEVER as a fact about their environment, "
        "and never mention scanning, detection, or how we know. If the line also names Oracle "
        "Fusion, do not mention Fusion at all.\n\n"
    )


def build_user(contact, cached_signal=None, prior_issues=None, tech_signals=None,
               tech_playbook=None, hiring_signals=None):
    persona = contact.get("persona", "it-leadership")
    framing = PERSONA_FRAMING.get(persona, PERSONA_FRAMING["it-leadership"])
    domain = contact.get("domain") or db.email_domain(contact.get("email"))
    base = (
        f"Contact:\n"
        f"- name: {contact.get('first_name','')} {contact.get('last_name','')}\n"
        f"- title: {contact.get('title','')}\n"
        f"- company: {contact.get('company','')}\n"
        + (f"- email domain: {domain}\n" if domain else "")
        + f"- persona: {persona}\n"
        f"- linkedin: {contact.get('linkedin_url','')}\n\n"
        f"Persona framing: {framing}\n\n"
    )
    # tech_playbook is accepted for call compatibility but unused: the GTM-tool
    # plays it drove can never fire under the ERP-only detection selection.
    mention = erp_mention_block(tech_signals)
    base += mention or (
        "No on-prem ERP was detected for this account. Anchor on the role and the general "
        "pattern about long-running ERP systems; say nothing about their specific stack.\n\n"
    )
    if hiring_signals:
        base += (
            f"Company hiring line (live job-postings scan; background only): {hiring_signals}\n"
            "Do NOT open on it and do not congratulate. You may use it only as quiet context for "
            "the role anchor. Never mention the data source.\n\n"
        )
    if cached_signal:
        base += (f"Company anchor (verified context; do NOT search the web): {cached_signal}\n\n"
                 f"Use the contact's first name in the copy. Write the sequence. Return only the JSON object.")
    else:
        base += (f"Use the contact's first name in the copy. Verify the domain's company if needed "
                 f"and write the sequence. Return only the JSON object.")
    if prior_issues:
        base += ("\n\nYour previous attempt FAILED these checks:\n- "
                 + "\n- ".join(prior_issues)
                 + "\nFix ALL of them. Return only the corrected JSON object.")
    return base


def lint_email(email):
    """Identical checks to enroll.lint_email_assets (the ingest source of truth)."""
    issues, steps = [], []
    for i in range(1, 5):
        subj, body = email.get(f"subject{i}", ""), email.get(f"body{i}", "")
        if not subj or not body:
            issues.append(f"missing subject{i}/body{i}")
        steps.append({"n": i, "subject": subj, "body": body})
    if issues:
        return issues
    issues += L.sequence_issues(steps)
    for idx, step in enumerate(steps):
        _, step_issues = L.lint_email(step, is_last=(idx == len(steps) - 1), is_first=(idx == 0))
        issues += [f"step{step['n']}: {it}" for it in step_issues]
    return issues


# --- the trigger-path linter: same VG claim discipline, looser structure ------
_HYPE = L.HYPE  # single source of truth for the hype list (lint_sequence)


def _wc(body):
    return len(re.findall(r"[A-Za-z0-9']+", body or ""))


def _lint_short(email, lo=35, hi=110):
    """VG rules for the trigger-anchored sequence: word band, no dashes /
    sign-off / pricing / hype, the full ban + claim-discipline list, a closed
    (never open-ended) ask in touch 1, no case-study offers, no stale call
    windows, a breakup in touch 4 that leaves the asset, standalone subjects.
    Structure is looser than the default path (the trigger opener replaces the
    canonical question opener), but the claim discipline is identical."""
    steps = []
    for i in range(1, 5):
        subj, body = email.get(f"subject{i}", ""), email.get(f"body{i}", "")
        if not subj or not body:
            return [f"missing subject{i}/body{i}"]
        steps.append({"n": i, "subject": subj, "body": body})
    issues = list(L.sequence_issues(steps))
    for s in steps:
        b = s["body"]
        wc = _wc(b)
        if not (lo - 5 <= wc <= hi + 10):
            issues.append(f"step{s['n']}: word count {wc} (need {lo}-{hi})")
        if L.DASH.search(b):
            issues.append(f"step{s['n']}: em/en dash present (use commas, colons or a full stop)")
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if lines and L.SIGNOFF_LINE.match(lines[-1]):
            issues.append(f"step{s['n']}: trailing sign-off/name (end on the ask, no sign-off)")
        for rx, msg in ((L.PRICING, "pricing language in a cold step"),
                        (L.BANNED, "banned term/phrase (see offer.md ban list)"),
                        (_HYPE, "hype word"),
                        (L.ANALYST_CLAIM, "analyst citation"),
                        (L.ORACLE_RELATIONSHIP, "Oracle co-sell/partner claim"),
                        (L.RECIPIENT_ASSERTION, "asserts a fact about the recipient"),
                        (L.LICENSING_NUMBER, "licensing/footprint percentage in cold copy"),
                        (L.BOOKING, "booking/scheduling link or tool"),
                        (L.BAD_DOMAIN, "valueglobal.com (the domain is valueglobal.net)")):
            if rx.search(b):
                issues.append(f"step{s['n']}: {msg}")
        for url in L.URL_CANDIDATE.findall(b):
            if not L.URL_ALLOWED.match(url.rstrip(".,)")):
                issues.append(f"step{s['n']}: link not on the whitelist: {url[:60]}")
        if len(L.STATS.findall(b)) > 2:
            issues.append(f"step{s['n']}: more than two credibility stats")
        if L.INFOCORVUS_FIGURES.search(b) and "infocorvus" not in b.lower():
            issues.append(f"step{s['n']}: InfoCorvus figure without the vendor attribution")
        if L.CASE_STUDY.search(b):
            issues.append(f"step{s['n']}: offers a case study / 'how similar companies "
                          "handled it' (none exist for this offering yet)")
    if "?" not in steps[0]["body"]:
        issues.append("step1: no question (touch 1 closes on the send-details permission ask)")
    if L.OPEN_ENDED.search(steps[0]["body"]):
        issues.append("step1: ends on an open-ended discovery question (never; close with "
                      "the permission ask: 'Can I send you some more details...?')")
    for s in steps[:2]:
        if L.MEETING.search(s["body"]):
            issues.append(f"step{s['n']}: asks for a call (only step 3 may)")
    if not L.MEETING.search(steps[2]["body"]):
        issues.append("step3: must ask for the short 20-minute call (two windows in prose)")
    if L.STALE_WINDOW.search(steps[2]["body"]):
        issues.append(f"step3: stale time reference "
                      f"'{L.STALE_WINDOW.search(steps[2]['body']).group(0)}' (the email sends "
                      "days after writing; name windows as weekday + time of day only)")
    if not L.BREAKUP.search(steps[-1]["body"]):
        issues.append("step4: final step is not a breakup")
    if not re.search(r"white paper|write-?up|\bread\b|\bpov\b|send it", steps[-1]["body"], re.I):
        issues.append("step4: breakup must leave the asset on the table ('reply any time "
                      "and I will send it')")
    return issues


# All three A/B variants share the canonical VG 4-touch shape and linter; the
# trigger path gets the looser-structure variant with identical claim rules.
LINTERS = {"value-give": lint_email, "earn": lint_email, "show": lint_email,
           ERP_VARIANT: lint_erp}


def lint_assets(asset):
    """Variant-aware lint used by both generation and ingest (reads asset['variant'])."""
    fn = LINTERS.get(asset.get("variant", DEFAULT_VARIANT), lint_email)
    return fn(asset.get("email", {}))


def _atomic_write(contact_id, asset):
    db.GEN_DIR.mkdir(parents=True, exist_ok=True)
    path = db.GEN_DIR / f"{contact_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asset, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def generate_contact(contact, knowledge, client, write=True, cached_signal=None,
                     variant=DEFAULT_VARIANT, tech_signals=None, tech_playbook=None,
                     hiring_signals=None):
    """Generate + validate one contact. Returns a result dict.

    cached_signal (when provided): use it as the company signal and DO NOT search
    the web — much cheaper. Otherwise research the signal with web search.
    tech_signals (when provided): the company's detected tech-stack line, passed
    to the prompt as background context.
    tech_playbook (when provided): the {ads, intent_abm, sequencing} groups from
    _cached_tech — steers the email-2 sequencing play and email-3
    signal-activation play in the prompt.
    hiring_signals (when provided): the company's open-sales-roles line, passed
    to the prompt with the email-2-only placement instruction.
    variant: which instruction set / linter to use (value-give | earn | show).
    write=False skips the file write (used by --contact-test); the asset is
    still returned under result["asset"].
    """
    cid, persona = contact["contact_id"], contact.get("persona", "it-leadership")
    mode = "write" if cached_signal else "research"
    use_search = cached_signal is None
    linter = LINTERS.get(variant, lint_email)
    issues, last_asset, web_searches = ["no output"], None, 0
    cache_read = cache_write = 0
    verified_company = ""  # the model's domain-verified company name (research mode)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            res = client.complete(
                build_system(knowledge, variant=variant, mode=mode),
                build_user(contact, cached_signal=cached_signal,
                           prior_issues=None if attempt == 1 else issues,
                           tech_signals=tech_signals, tech_playbook=tech_playbook,
                           hiring_signals=hiring_signals),
                use_web_search=use_search, max_web_searches=3, max_tokens=4096,
            )
            web_searches = res.get("web_search_count", 0)
            u = res.get("usage", {})
            cache_read += u.get("cache_read_input_tokens", 0) or 0
            cache_write += u.get("cache_creation_input_tokens", 0) or 0
        except AnthropicError as e:
            issues = [f"api error: {e}"]
            continue
        try:
            data = extract_json(res["text"])
        except AnthropicJSONError:
            issues = ["model did not return valid JSON"]
            continue

        verified_company = (data.get("company") or "").strip() or verified_company
        last_asset = {
            "contact_id": cid,
            "persona": persona,
            "variant": variant,
            # trust the cached signal verbatim; otherwise take the model's
            "signal": (cached_signal or data.get("signal") or "").strip(),
            "email": data.get("email", {}) or {},
            "linkedin": data.get("linkedin", {}) or {},
        }
        issues = linter(last_asset["email"])
        if not issues:
            if write:
                _atomic_write(cid, last_asset)
            return {"status": "linted", "signal": last_asset["signal"], "asset": last_asset,
                    "company": verified_company,
                    "web_searches": web_searches, "attempts": attempt, "issues": [],
                    "cache_read": cache_read, "cache_write": cache_write}

    # all attempts failed: write the last asset (if any) so ingest records it as failed with reason
    if write and last_asset is not None:
        _atomic_write(cid, last_asset)
    return {"status": "failed", "asset": last_asset,
            "signal": (last_asset or {}).get("signal", ""),
            "company": verified_company,
            "web_searches": web_searches, "attempts": MAX_ATTEMPTS, "issues": issues,
            "cache_read": cache_read, "cache_write": cache_write}


# Per-domain locks so a company is researched once even under the worker pool.
_DOMAIN_LOCKS = {}
_DOMAIN_LOCKS_GUARD = threading.Lock()


def _domain_lock(domain):
    with _DOMAIN_LOCKS_GUARD:
        lk = _DOMAIN_LOCKS.get(domain)
        if lk is None:
            lk = threading.Lock()
            _DOMAIN_LOCKS[domain] = lk
        return lk


def _fresh_cached_signal(domain):
    """Return a fresh (<90d) cached signal string for the domain, or None."""
    if not domain:
        return None
    conn = db.connect()
    try:
        row = db.get_signal(conn, domain)
    finally:
        conn.close()
    return row["signal"] if db.signal_fresh(row) else None


def _maybe_detect_tech(domain, company=None):
    """Best-effort technographic scan alongside signal research (cache-aware, a
    few seconds; TECH_DETECT_ENABLED=0 turns the inline hook off). Failures only
    log to stderr — generation must never break on the detector."""
    if not domain:
        return
    if (os.environ.get("TECH_DETECT_ENABLED") or "1").strip().lower() in ("0", "false", "no", "off"):
        return
    try:
        import tech_signals as _tech  # lazy: pulls in dnspython + vendored package
        _tech.detect_and_store(domain, company=company)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[generate] tech detect skipped for {domain}: {e}\n")


def _cached_tech(domain):
    """(line, playbook) for prompt use — the stored tech-stack line plus the
    copy playbook groups ({ads, intent_abm, sequencing}) classified from stored
    tech_detail. (None, None) when there is nothing to say (no row, or the 'No
    signals detected' literal); legacy rows with no parseable detail give
    (line, None) = the old line-only background behavior. Classification is
    best-effort and never breaks generation."""
    if not domain:
        return None, None
    conn = db.connect()
    try:
        row = db.get_signal(conn, domain)
    finally:
        conn.close()
    tech = (row or {}).get("tech_signals")
    if not tech or tech == "No signals detected":
        return None, None
    playbook = None
    try:
        import tech_signals as _tech  # lazy: stdlib-only at import, mirrors the boot rule
        playbook = _tech.playbook_from_detail(row.get("tech_detail"))
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[generate] tech playbook skipped for {domain}: {e}\n")
    return tech, playbook


def _maybe_detect_hiring(domain, company=None):
    """Best-effort hiring scan alongside signal research (cache-aware, one
    Prospeo credit per company per HIRING_REFRESH_DAYS; HIRING_DETECT_ENABLED=0
    turns the inline hook off, and without PROSPEO_API_KEY it is a no-op).
    Failures only log to stderr — generation must never break on the detector."""
    if not domain:
        return
    if (os.environ.get("HIRING_DETECT_ENABLED") or "1").strip().lower() in ("0", "false", "no", "off"):
        return
    try:
        import hiring_signals as _hiring  # lazy (stdlib-only, but mirror the boot rule)
        if not _hiring.hiring_available()[0]:
            return
        _hiring.detect_and_store(domain, company=company)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[generate] hiring detect skipped for {domain}: {e}\n")


def _cached_hiring(domain):
    """A compact hiring line for prompt use, or None. Fires ONLY when the scan
    found open sales/GTM roles (from hiring_detail.sales_titles) — a plain
    open-roles count with no sales roles is a weak angle for this pitch, and
    the 'No open roles detected' literal means nothing to say."""
    if not domain:
        return None
    conn = db.connect()
    try:
        row = db.get_signal(conn, domain)
    finally:
        conn.close()
    line = (row or {}).get("hiring_signals")
    if not line or line == "No open roles detected":
        return None
    try:
        detail = json.loads(row.get("hiring_detail") or "{}")
    except (ValueError, TypeError):
        return None
    sales = [t for t in (detail.get("sales_titles") or []) if t]
    if not sales:
        return None
    count = detail.get("active_count") or len(sales)
    return (f"{count} open roles, {len(sales)} in sales/GTM "
            f"(e.g. {', '.join(sales[:2])})")


def generate_one(contact, knowledge, client, write=True, variant=DEFAULT_VARIANT):
    """Cache-aware single-contact generation.

    Segment-approved trigger contacts (variant 'erp-trigger' / segment in
    ERP_PLAYS) write from their stored trigger verdict — no web search, no
    signal cache. Everyone else: reuse a fresh per-company signal (write-only),
    else research once per company under a per-domain lock and cache it.
    """
    domain = (contact.get("domain") or db.email_domain(contact.get("email")))

    # Fusion suppression guard: a pure-Fusion estate (Fusion detected, no
    # on-prem ERP) is not a prospect — ROAD cannot archive out of Fusion.
    # Normally these accounts are stopped at the segment gate; this catches
    # any path around it without spending an API call.
    try:
        import suppression as _sup  # stdlib-only
        conn = db.connect()
        try:
            sig_row = db.get_signal(conn, domain)
        finally:
            conn.close()
        if _sup.fusion_only_detected(sig_row) is True:
            return {"contact_id": contact["contact_id"], "status": "failed",
                    "issues": ["suppressed: Fusion-only estate — not a prospect"],
                    "used_cache": True}
    except Exception as e:  # noqa: BLE001 — the guard must never break generation
        sys.stderr.write(f"[generate] fusion guard skipped for {domain}: {e}\n")

    seg = (contact.get("segment") or "").strip()
    if contact.get("variant") == ERP_VARIANT or seg in ERP_PLAYS:
        verdict = _segment_verdict(domain, seg)
        if verdict:
            r = generate_contact_erp(contact, seg, verdict, client, write=write,
                                     tech_line=_cached_tech(domain)[0])
            r["used_cache"] = True  # verdict-anchored: zero research spend
            return r
        # verdict gone (news re-scan no longer finds the trigger): fall back to
        # the default research path with the default style rules
        sys.stderr.write(f"[generate] no stored {seg!r} verdict for {domain} — "
                         f"falling back to the default path\n")
        variant = DEFAULT_VARIANT

    cached = _fresh_cached_signal(domain)
    if cached:
        tech_line, tech_playbook = _cached_tech(domain)
        r = generate_contact(contact, knowledge, client, write=write, cached_signal=cached, variant=variant,
                             tech_signals=tech_line, tech_playbook=tech_playbook,
                             hiring_signals=_cached_hiring(domain))
        r["used_cache"] = True
        return r

    lock = _domain_lock(domain) if domain else None
    if lock:
        lock.acquire()
    try:
        if domain:  # another thread may have cached it while we waited
            cached = _fresh_cached_signal(domain)
            if cached:
                tech_line, tech_playbook = _cached_tech(domain)
                r = generate_contact(contact, knowledge, client, write=write, cached_signal=cached, variant=variant,
                                     tech_signals=tech_line, tech_playbook=tech_playbook,
                                     hiring_signals=_cached_hiring(domain))
                r["used_cache"] = True
                return r
        # cache miss = this thread researches the company: scan its tech + hiring
        # first (cache-aware, seconds) so this contact's copy can already use them
        _maybe_detect_tech(domain, contact.get("company", ""))
        _maybe_detect_hiring(domain, contact.get("company", ""))
        tech_line, tech_playbook = _cached_tech(domain)
        r = generate_contact(contact, knowledge, client, write=write, variant=variant,  # search + write
                             tech_signals=tech_line, tech_playbook=tech_playbook,
                             hiring_signals=_cached_hiring(domain))
        r["used_cache"] = False
        sig = (r.get("signal") or "").strip()
        if domain and sig:
            has_recent = not sig.lower().startswith("no recent signal")
            conn = db.connect()
            try:
                # prefer the model's domain-verified company name over the (possibly
                # stale) CRM value so the cache row can't say "VictorOps" for splunk.com
                db.upsert_signal(conn, domain, r.get("company") or contact.get("company", ""),
                                 sig, has_recent, client.model)
            finally:
                conn.close()
        return r
    finally:
        if lock:
            lock.release()


def research_signal(domain, company, client=None):
    """Force a fresh signal search for one company and update the cache (UI refresh)."""
    client = client or AnthropicClient()
    system = ("You research a B2B company's single most recent GTM signal.\n\n"
              + RESEARCH_ONLY_TASK.format(**_today()))
    user = f"Company: {company or domain} (domain {domain}). Find the recent signal and return only the JSON."
    res = client.complete(system, user, use_web_search=True, max_web_searches=3, max_tokens=1024)
    data = extract_json(res["text"])
    signal = (data.get("signal") or "").strip()
    has_recent = bool(data.get("has_recent_signal")) and not signal.lower().startswith("no recent signal")
    # the model reconciles the CRM name against the domain; its verified name wins
    company_out = (data.get("company") or "").strip() or (company or "")
    conn = db.connect()
    try:
        db.upsert_signal(conn, domain, company_out, signal, has_recent, client.model)
    finally:
        conn.close()
    # a signal refresh also freshens the tech + hiring scans (skip-if-fresh keeps it cheap)
    _maybe_detect_tech(domain, company_out)
    _maybe_detect_hiring(domain, company_out)
    return {"domain": domain, "company_name": company_out, "signal": signal,
            "has_recent": 1 if has_recent else 0, "web_searches": res.get("web_search_count", 0)}


# ----------------------------------------------------------------------------
# "Show the product" fulfillment: when a show-arm lead says "yes, send them",
# draft 3 sample outbound emails our AI would write for THEIR outbound — i.e. to
# 3 accounts that fit the lead's company's ICP. This is the deliverable demo.
# ----------------------------------------------------------------------------
SAMPLES_TASK = """\
Today is {today}.

You are the AI SDR working FOR {company}. Show {company}'s GTM leader exactly what your outbound looks
like for THEIR business.

1. Research {company}: what they sell and, crucially, WHO they sell to (their ICP — the kind of
   companies and roles {company} prospects).
2. Pick THREE realistic target accounts that {company} would actually prospect (real companies that fit
   their ICP, not {company} itself). For each, find ONE recent ({year}) signal if you can; if not, use a
   specific true fact about that account.
3. For each, write ONE short, sharp, personalized opening line (1-2 sentences) that {company}'s AI SDR
   would send to that account, grounded in the signal/fact. Sound like a sharp human, no hype, no em
   dashes, do not invent funding numbers.

Return ONLY this JSON, no prose:
{{"company": "{company}",
  "icp_summary": "<one line: who {company} sells to>",
  "samples": [
    {{"account": "<target company>", "signal": "<recent signal or specific fact>", "opener": "<1-2 sentence opener>"}},
    {{"account": "...", "signal": "...", "opener": "..."}},
    {{"account": "...", "signal": "...", "opener": "..."}}
  ]}}
"""


def generate_samples(company, domain="", client=None):
    """Draft 3 sample outbound emails our AI would write for {company}'s own outbound."""
    client = client or AnthropicClient()
    system = ("You are an expert B2B SDR copywriter. Be specific, concrete, and human. Ground claims in "
              "what you can actually find; never invent funding figures or fake metrics.")
    user = SAMPLES_TASK.format(company=(company or domain or "the company"), **_today())
    res = client.complete(system, user, use_web_search=True, max_web_searches=4, max_tokens=2048)
    data = extract_json(res["text"])
    return {
        "company": company, "domain": domain,
        "icp_summary": (data.get("icp_summary") or "").strip(),
        "samples": data.get("samples", []) or [],
        "web_searches": res.get("web_search_count", 0),
    }


# ----------------------------------------------------------------------------
# Message Batches API path: build requests, then process results. Same prompts
# and cache logic as the real-time path, just packaged for async batch submit.
# ----------------------------------------------------------------------------
def build_request_params(contact, knowledge, client, cached_signal=None, variant=DEFAULT_VARIANT,
                         tech_signals=None, tech_playbook=None, hiring_signals=None):
    """The Messages `params` for one contact (write-only if a cached signal is
    given, else a combined research+write request with web search). 1h cache."""
    mode = "write" if cached_signal else "research"
    return client.build_body(
        build_system(knowledge, variant=variant, mode=mode),
        build_user(contact, cached_signal=cached_signal, tech_signals=tech_signals,
                   tech_playbook=tech_playbook, hiring_signals=hiring_signals),
        use_web_search=cached_signal is None, max_web_searches=3,
        max_tokens=4096, cache_ttl="1h",
    )


def prepare_batch_requests(contacts, knowledge, client=None, variant=DEFAULT_VARIANT):
    """Build {custom_id, params} for each contact + a manifest for result handling.
    Already-cached companies become cheap write-only requests (no web search);
    segment-approved trigger contacts become verdict-anchored write-only
    requests through the ERP system prompt."""
    client = client or AnthropicClient()
    requests, manifest = [], {}
    for c in contacts:
        cid = str(c["contact_id"])
        domain = c.get("domain") or db.email_domain(c.get("email"))
        seg = (c.get("segment") or "").strip()
        if c.get("variant") == ERP_VARIANT or seg in ERP_PLAYS:
            verdict = _segment_verdict(domain, seg)
            if verdict:
                tech_line, _ = _cached_tech(domain)
                requests.append({"custom_id": cid, "params": client.build_body(
                    ERP_SYSTEM, build_erp_user(c, seg, verdict, tech_line=tech_line),
                    use_web_search=False, max_tokens=4096, cache_ttl="1h")})
                # cached_signal = the verdict line, so process_batch_result stores
                # it verbatim and never re-caches it as a researched signal
                manifest[cid] = {"contact": c, "domain": domain, "variant": ERP_VARIANT,
                                 "segment": seg, "was_combined": False,
                                 "cached_signal": erp_signal_line(seg, verdict)}
                continue
            sys.stderr.write(f"[generate] no stored {seg!r} verdict for {domain} — "
                             f"batching {cid} through the default path\n")
        cached = _fresh_cached_signal(domain)
        cvariant = c.get("variant") or variant  # per-contact split wins over run-level
        if cvariant == ERP_VARIANT:
            cvariant = DEFAULT_VARIANT  # verdict gone: default rules, not the ERP linter
        tech_line, tech_playbook = _cached_tech(domain)
        requests.append({"custom_id": cid,
                         "params": build_request_params(c, knowledge, client, cached_signal=cached,
                                                        variant=cvariant, tech_signals=tech_line,
                                                        tech_playbook=tech_playbook,
                                                        hiring_signals=_cached_hiring(domain))})
        manifest[cid] = {"contact": c, "domain": domain, "variant": cvariant,
                         "was_combined": cached is None, "cached_signal": cached}
    return requests, manifest


def process_batch_result(custom_id, result, manifest):
    """Handle one batch result. On success: lint + write the file + cache the
    researched signal. Returns status linted | retry | error (retry = caller
    should re-generate this contact synchronously)."""
    entry = manifest.get(str(custom_id))
    if not entry:
        return {"status": "error", "issues": ["unknown custom_id"]}
    contact = entry["contact"]
    cid, persona = contact["contact_id"], contact.get("persona", "it-leadership")
    domain, cached_signal = entry["domain"], entry.get("cached_signal")
    variant = entry.get("variant", DEFAULT_VARIANT)

    if result.get("type") != "succeeded":
        return {"status": "retry", "issues": [f"batch result: {result.get('type')}"], "contact": contact}

    parsed = parse_message(result.get("message", {}))
    try:
        data = extract_json(parsed["text"])
    except AnthropicJSONError:
        return {"status": "retry", "issues": ["invalid JSON from batch"], "contact": contact}

    asset = {
        "contact_id": cid, "persona": persona, "variant": variant,
        "signal": (cached_signal or data.get("signal") or "").strip(),
        "email": data.get("email", {}) or {}, "linkedin": data.get("linkedin", {}) or {},
    }
    # cache the researched signal (combined requests) even if the copy fails lint
    if entry["was_combined"] and domain and asset["signal"]:
        has_recent = not asset["signal"].lower().startswith("no recent signal")
        conn = db.connect()
        try:
            db.upsert_signal(conn, domain,
                             (data.get("company") or "").strip() or contact.get("company", ""),
                             asset["signal"], has_recent)
        finally:
            conn.close()

    issues = lint_assets(asset)
    if issues:
        return {"status": "retry", "issues": issues, "contact": contact, "signal": asset["signal"]}
    _atomic_write(cid, asset)
    return {"status": "linted", "signal": asset["signal"],
            "web_searches": parsed["web_search_count"], "usage": parsed["usage"],
            "used_cache": cached_signal is not None}


def generate_batch(batch_id, progress_cb=None, cancel_event=None, max_workers=MAX_WORKERS,
                   variant=DEFAULT_VARIANT):
    """Generate all contacts in a batch concurrently. Does NOT touch the DB —
    the caller runs `sdr_batches.py ingest <batch_id>` to record results.

    progress_cb(contact_id, state, **extra) is called as each contact moves:
      state in {"researching","linted","failed","cancelled","error"}.
    """
    conn = db.connect()
    contacts = db.get_batch(conn, batch_id)
    conn.close()
    knowledge = load_knowledge()
    client = AnthropicClient()

    if progress_cb:
        for c in contacts:
            progress_cb(c["contact_id"], "queued",
                        name=f"{c.get('first_name','')} {c.get('last_name','')}".strip(),
                        company=c.get("company", ""), persona=c.get("persona", ""))

    results = {}

    def _run_one(contact):
        cid = contact["contact_id"]
        if cancel_event is not None and cancel_event.is_set():
            if progress_cb:
                progress_cb(cid, "cancelled")
            return {"status": "cancelled", "issues": ["cancelled"]}
        if progress_cb:
            progress_cb(cid, "researching")
        try:
            # a pre-assigned per-contact variant (e.g. a 3-way-split sourced list) wins;
            # otherwise use the run-level variant the caller chose.
            cvariant = contact.get("variant") or variant
            r = generate_one(contact, knowledge, client, variant=cvariant)
        except Exception as e:  # noqa: BLE001 - never let one contact kill the batch
            r = {"status": "error", "issues": [str(e)[:300]], "web_searches": 0,
                 "attempts": 0, "signal": "", "used_cache": False}
        if progress_cb:
            progress_cb(cid, r["status"], web_searches=r.get("web_searches", 0),
                        signal=r.get("signal", ""), issues=r.get("issues", []),
                        cache_read=r.get("cache_read", 0), cache_write=r.get("cache_write", 0),
                        used_cache=r.get("used_cache", False))
        return r

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_run_one, c): c["contact_id"] for c in contacts}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()

    linted = sum(1 for r in results.values() if r["status"] == "linted")
    failed = len(results) - linted
    return {"batch_id": batch_id, "total": len(contacts), "linted": linted,
            "failed": failed, "results": results}


def contact_test():
    """Generate ONE synthetic contact and print the result. No DB/Bison writes.

    usage: generate_batch.py --contact-test [Company] [Title] [persona] [FirstName] [variant]
           variant in {value-give, earn, show} (default value-give)
    """
    args = sys.argv[2:]
    variant = args[4] if len(args) > 4 and args[4] in WRITE_RULES else DEFAULT_VARIANT
    contact = {
        "contact_id": "TEST",
        "first_name": args[3] if len(args) > 3 else "Jordan",
        "last_name": "Test",
        "title": args[1] if len(args) > 1 else "VP of Sales",
        "company": args[0] if len(args) > 0 else "Ramp",
        "persona": args[2] if len(args) > 2 else "it-leadership",
        "linkedin_url": "",
    }
    print(f"generating [{variant}] test copy for {contact['first_name']} @ {contact['company']} "
          f"({contact['persona']})...\n")
    client = AnthropicClient()
    r = generate_contact(contact, load_knowledge(), client, write=False, variant=variant)
    print(f"status: {r['status']}  web_searches: {r['web_searches']}  attempts: {r['attempts']}  "
          f"cache_read: {r.get('cache_read', 0)}  cache_write: {r.get('cache_write', 0)}")
    print(f"signal: {r['signal']}\n")
    if r.get("asset"):
        print(json.dumps(r["asset"], indent=2, ensure_ascii=False))
    if r["issues"]:
        print("\nlint issues:", r["issues"])
    return 0 if r["status"] == "linted" else 1


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--contact-test":
        return contact_test()
    if len(sys.argv) < 2:
        print("usage: generate_batch.py <batch_id>  |  generate_batch.py --contact-test [company] [title] [persona]")
        return 2
    batch_id = int(sys.argv[1])

    def cb(cid, state, **extra):
        if state in ("researching", "queued"):
            return
        tag = "OK " if state == "linted" else state
        cache = extra.get("cache_read", 0)
        cinfo = f" cache:{cache/1000:.1f}k read" if cache else ""
        print(f"  [{tag}] {cid} {('('+str(extra.get('web_searches',0))+' searches)') if state=='linted' else ''}{cinfo}"
              + (f" -- {extra.get('issues')}" if extra.get("issues") else ""))

    summary = generate_batch(batch_id, progress_cb=cb)
    print(f"batch {batch_id}: {summary['linted']} linted, {summary['failed']} failed")
    # record results through the canonical ingest path
    proc = subprocess.run([sys.executable, str(SDR_BATCHES), "ingest", str(batch_id)],
                          cwd=str(SKILLS.parents[1]), capture_output=True, text=True)
    print(proc.stdout.strip() or proc.stderr.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
