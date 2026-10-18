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
import instructions as I                                     # noqa: E402
import lint_sequence as L                                    # noqa: E402
import buyer_group as BG                                     # noqa: E402
import send_gate as SG                                       # noqa: E402
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
        "problem": "An acquisition, divestiture or carve-out leaves an ERP that someone wants to stop "
                   "running. An acquirer folding the target in ends up with the acquired company's ERP "
                   "still on, kept mainly so finance and audit can pull old reports. A divested entity "
                   "or carve-out has its finance and operations history sitting inside the parent's "
                   "ERP and has to get it out, with transactions still linked, before the transition "
                   "period (which runs from closing) ends.",
        "solution": "Application retirement on ROAD: the old system's full history moves into a "
                    "searchable archive with the business context and the reports finance and audit "
                    "still use, so the old ERP can be switched off. For a divestiture, ROAD first "
                    "extracts only the separating entity's records as complete business objects, so it "
                    "stands up on its own data, then the leftover copy is retired.",
        "opener": "the deal itself (what was agreed or closed, and when), tied to what happens to the "
                  "old ERP once integration settles (acquirer) or to getting the entity's history out "
                  "before the transition period ends (divestiture / carve-out)",
    },
    "erp_migration": {
        "label": "ERP migration",
        "problem": "They are switching to a new ERP. After go-live the old environment usually stays "
                   "on for years: nobody transacts in it, but finance and audit still need its "
                   "history, so it keeps costing money to run for lookups.",
        "solution": "Application retirement on ROAD: retire the old system and move its history into "
                    "a searchable archive with the reports teams still use. Planned before cutover, "
                    "only active data has to migrate.",
        "opener": "their move to the new ERP (platform and stage when known), tied to what happens "
                  "to the old system after cutover",
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
  ONE ask per email. Open EVERY email with the contact's first name and a comma.
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
  for dev and test, a close that drags. Then one bridge sentence, in your own words for this
  account: the idea that the dormant data could move off the live system and stay searchable
  and audit-ready. Close with a closed permission ask to send more detail, naming their real
  company, worded fresh each time. Value Global flagged identical lines across accounts as a
  template giveaway: NEVER write "What if we showed you a way to move that dormant data off the
  live system" or "Can I send you some more details to show how <Company> could benefit?"
  verbatim. NEVER end touch 1 on an open-ended question ("how are you thinking about...",
  "what's your plan for..."): the only questions allowed are the bridge and the closed
  send-details ask. No meeting ask in touch 1.
  Touch 1 runs long easily: keep it to 70-100 words TOTAL. One or two short sentences for the
  trigger opener, ONE sentence for the cost (pick two or three symptoms, never the whole
  list), one bridge, one ask. Count the words before you answer.
- Touch 2: open with ONE fresh insight tied to the trigger event, a different angle than touch
  1's opener, never "just following up". Then offer the free Data Lifecycle Assessment by what
  they get: a data-growth profile, which modules are archive candidates, a quantified savings
  number, and a phased roadmap, with the link www.valueglobal.net/archiving-solutions. End
  with the closed offer to set it up: "Want me to set that up for <Company>?" NEVER offer a
  sample report (not cleared for use yet). NEVER offer case studies, references, success stories, or
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
- LinkedIn: li_connect is a <=280-char connection note that observes the trigger and connects,
  no pitch, never tells them what they face ("huge lift ahead", "you have a lot on your
  plate"); li_msg1/li_msg2 are short, value-first follow-ups offering the read. Vocabulary on
  this track is ARCHIVE; do not mix in "retiring and archiving".
- Set the `signal` JSON field to the trigger finding with its month/date.

""" + OUTPUT_SCHEMA


# ----------------------------------------------------------------------------
# Signal-to-track routing (Value Global "Signal Messaging Feedback", 2026-10-09).
# A company in an acquisition, a divestiture or an ERP switch is trying to STOP
# running an old system, so it gets the application-RETIREMENT pitch, not the
# live-archiving (staying-on-EBS) pain. Tracks:
#   retirement  — M&A acquirer (or acquired whole), ERP switch. Licensing claim
#                 allowed except when the new ERP is Oracle Fusion (or unknown).
#   data_split  — divestiture / spinoff / carve-out: extract the separating
#                 entity's data, then retire the leftover copy.
#   archiving   — license_audit / ebs_oci / ebs_performance: unchanged
#                 (ERP_SYSTEM above).
#   hold        — acquired company stays a separate unit: nothing to retire,
#                 never generated.
# erp_route() is deterministic; the decision is stored on the asset as
# `route` so the client can review it per account.
# ----------------------------------------------------------------------------
RETIRE_TRACKS = ("retirement", "data_split")
_ROLES = ("acquirer", "acquired", "seller", "divested_entity", "carveout_buyer")
_ACQUIRED_RX = re.compile(r"\b(to be|agreed to be|will be|is being|being) acquired by\b|"
                          r"\bacquired by\b|\bsold to\b|\bto sell itself\b", re.I)
_SPUN_FROM_RX = re.compile(r"\b(spun|spin(s|ning)?)[- ]?off\s+from\b|\bseparat\w+\s+from\b|"
                           r"\bcarved?[- ]?out\s+(of|from)\b", re.I)
_UNIT_BUY_RX = re.compile(r"\bacqui\w+\b.{0,80}\b(division|unit|business|segment|carve-?out)\b"
                          r".{0,60}\bfrom\b", re.I)
_SEPARATE_RX = re.compile(r"separate (business |operating )?unit|stand-?alone|"
                          r"independent(ly)? (subsidiary|operat\w*|brand|business)|"
                          r"operate (independently|as a separate)", re.I)
_ORACLE_LEGACY = ("e-business suite", "ebs", "peoplesoft", "jd edwards", "jde", "oracle")


def _legacy_erp(details, tech_line):
    """The old system's name when CONFIRMED (verdict legacy_erp or the
    deterministic tech scan of the prospect's own domain), else '' — never
    guessed (feedback issue 8). Callers pass tech_line=None when the system
    being retired is NOT the prospect's (acquirer / carve-out buyer)."""
    legacy = str(details.get("legacy_erp") or "").strip()
    if legacy:
        return legacy
    low = (tech_line or "").lower()
    for key, name in (("e-business suite", "Oracle E-Business Suite"),
                      ("peoplesoft", "PeopleSoft"), ("jd edwards", "JD Edwards")):
        if key in low:
            return name
    return ""


def erp_route(segment, verdict, tech_line=None):
    """Route one trigger verdict to a messaging track. Returns
    {track, side, licensing, target, legacy, entity, closed, basis, reason}.
    `basis` is 'verdict' when the research extracted the deal side / target,
    'inferred' when it came from deal_type + headline wording (verdicts
    researched before the side fields existed)."""
    d = (verdict or {}).get("details") or {}
    if not isinstance(d, dict):
        d = {}
    legacy = _legacy_erp(d, tech_line)  # re-derived below for buyer-side M&A
    route = {"track": "archiving", "side": "", "licensing": False, "target": "",
             "legacy": legacy, "entity": str(d.get("entity") or "").strip(),
             "closed": d.get("closed") if isinstance(d.get("closed"), bool) else None,
             "basis": "verdict", "reason": ""}
    if segment == "ma_carveout":
        role = str(d.get("prospect_role") or "").strip().lower()
        head = f"{verdict.get('headline', '')}"
        if role not in _ROLES:
            route["basis"] = "inferred"
            deal = str(d.get("deal_type") or "").strip().lower()
            if _ACQUIRED_RX.search(head):
                role = "acquired"
            elif deal in ("divestiture", "spinoff"):
                role = "divested_entity" if _SPUN_FROM_RX.search(head) else "seller"
            elif _UNIT_BUY_RX.search(head):
                role = "carveout_buyer"
            else:
                role = "acquirer"
        route["side"] = role
        if role in ("acquirer", "carveout_buyer"):
            # the system being retired belongs to the acquired / carved-out
            # entity: the buyer's own tech scan says nothing about it
            route["legacy"] = _legacy_erp(d, None)
        integ = str(d.get("integration") or "").strip().lower()
        separate = integ == "separate_unit" or (
            not integ and role == "acquirer"
            and _SEPARATE_RX.search(f"{head} {verdict.get('summary', '')}"))
        if role == "acquirer" and separate:
            route.update(track="hold", reason="acquired company runs as a separate unit: "
                                              "no consolidation, nothing to retire")
        elif role in ("acquirer", "acquired"):
            route.update(track="retirement", licensing=True,
                         reason=f"M&A, prospect is the {role}: consolidate and retire")
        else:
            route.update(track="data_split",
                         reason=f"M&A, prospect is the {role.replace('_', ' ')}: "
                                "extract the entity's data, then retire the leftover copy")
    elif segment == "erp_migration":
        target = str(d.get("platform") or "").strip()
        route.update(track="retirement", side="erp_switch", target=target)
        tl = target.lower()
        if "oracle" in tl or "fusion" in tl or "netsuite" in tl:
            # Oracle-owned destination (Fusion, NetSuite...): still an Oracle
            # license, so the "license goes away" claim does not hold
            route["reason"] = (f"ERP switch to {target} (an Oracle product): retirement, "
                               "no licensing claim")
        elif not target or tl == "other":
            route["reason"] = "ERP switch, target unknown: retirement, no licensing claim"
        else:
            route.update(licensing=True,
                         reason=f"ERP switch to {target} (non-Oracle): retirement")
    else:
        route["reason"] = "staying on the ERP: archiving track (unchanged)"
    return route


_SIDE_GUIDE = {
    "acquirer": (
        "They are the ACQUIRER, folding {entity} into their own operations. Frame: once the "
        "integration settles, most acquirers find the acquired company's ERP still running, kept "
        "on mainly so finance and audit can pull old reports. Retire it, keep its history and "
        "reports searchable. They are CONSOLIDATING: never use separation, carve-out, or "
        "'stand up independently' language."),
    "acquired": (
        "They are being ACQUIRED whole. Frame: once they move onto the buyer's systems, their own "
        "ERP usually stays on for lookups; its history and reports have to survive the switch off. "
        "Never use 'stand up independently' language."),
    "seller": (
        "They are the SELLER: {entity} is separating from them. Frame: {entity}'s finance and "
        "operations history sits inside their ERP and has to come out with transactions still "
        "linked; ROAD extracts only {entity}'s records as complete business objects, and the "
        "leftover copy is then retired rather than kept running."),
    "divested_entity": (
        "They ARE the separating entity, leaving the parent. Frame: their finance and operations "
        "history sits inside the parent's ERP; how do they get it out, with transactions still "
        "linked, before the transition period ends, so they stand up on their own data."),
    "carveout_buyer": (
        "They are BUYING a carve-out ({entity}) out of a parent under a transition period. Frame: "
        "{entity}'s history sits in the seller's ERP; get only its records out, transactions still "
        "linked, before the transition period ends; the leftover copy is then retired."),
    "erp_switch": (
        "They are moving to {target}. Frame: after go-live the old environment usually stays on "
        "for years; nobody transacts in it, but finance and audit still need its history. Retire "
        "it into a searchable archive with the reports teams still use; the better time to plan "
        "it is before cutover, so only active data has to migrate."),
}


# Value Global's sample sequences ("Signal Messaging Feedback" section 5,
# 2026-10-09), verbatim. They show TONE and STRUCTURE, not fixed text: the
# prompt says so, and _lint_retire rejects copy that lifts a long run of words
# from them (feedback issue 12: identical lines across accounts read as a
# template). Divestiture and ERP-switch touches 3-4 follow the acquirer pattern.
_RETIRE_T34 = """\
Touch 3. Subject: 20 minutes on the {Acquired} ERP
{First}, would 20 minutes help? I would cover three things for {Company}: which inherited systems
can be switched off, which reports need to survive, and what the old ERP costs to run until then.
{Slot 1} or {Slot 2} work on my end, and I am happy to fit your calendar.

Touch 4. Subject: Closing the loop
{First}, I will stop here. If the question of what to do with {Acquired}'s old ERP comes up later
this year, reply any time and we can pick it up. Happy to stay connected either way."""

RETIRE_EXAMPLES = {
    "acquirer": """\
Touch 1. Subject: {Acquired} systems after the close
{First}, I saw {Company} {closed / agreed to acquire} {Acquired} on {date}. Once the integration
settles, most acquirers find the acquired company's ERP still running, kept on mainly so finance
and audit can pull old reports. Has anyone decided what happens to that system once its users and
transactions have moved across?

Touch 2. Subject: Retiring the old ERP without losing the reports
{First}, a follow-up on {Acquired}. The usual options are to keep paying license, support and
hosting on the old ERP for years, or to push history into a new system that was not built to hold
it. A third option is to retire the old system and move its history into a searchable archive that
carries the reports finance and audit still use. InfoCorvus, whose ROAD platform we deliver, puts
the saving at $1M to $6M a year per retired legacy system. Would 20 minutes be worth it to see
whether that applies to {Acquired}?

""" + _RETIRE_T34,
    "data_split": """\
Touch 1. Subject: Getting {Entity}'s history out of {Parent}
{First}, with {Entity} separating from {Parent}, its finance and operations history sits inside
{Parent}'s ERP. How are you planning to get that history out, with transactions still linked to
each other, before the transition period ends?

Touch 2. Subject: A clean split, then switch it off
{First}, the common route is a scripted extract. The links between invoices, payments and the
ledger break, and someone rebuilds them later under deadline. ROAD, the InfoCorvus platform we
deliver, pulls only {Entity}'s records as complete business objects, so an invoice still carries
its lines, payments and accounting entries. {Entity} stands up on its own data, and the leftover
copy can be retired rather than kept running. Worth 20 minutes to map this against your timeline?

(Touches 3 and 4 follow the acquirer pattern, adapted to the split:)
""" + _RETIRE_T34,
    "erp_switch": """\
Touch 1. Subject: The Oracle system after {Target} goes live
{First}, I saw {Company} is moving to {Target}. After go-live, the old Oracle environment usually
stays on for years. Nobody transacts in it, but finance and audit still need its history. Is there
a plan yet for that system after cutover?

Touch 2. Subject: Paying Oracle for lookups
{First}, keeping it running means paying Oracle license and support on a system used only for
lookups. Retiring it moves that history into a searchable archive with the reports your teams
still use, and the license on that environment goes away. One Oracle EBS environment of 2 TB was
retired in a month. The better time to plan it is before cutover, so only active data has to
migrate. Would 20 minutes be useful?

(Touches 3 and 4 follow the acquirer pattern, adapted to the old system:)
""" + _RETIRE_T34,
}
_EXAMPLE_KEY = {"acquirer": "acquirer", "acquired": "acquirer", "seller": "data_split",
                "divested_entity": "data_split", "carveout_buyer": "data_split",
                "erp_switch": "erp_switch"}


def retire_example_block(route):
    """The client's sample sequence for this route, framed as tone/structure
    only, with the route-specific deltas (Fusion: no licensing sentence;
    old system not confirmed Oracle: don't say Oracle)."""
    key = _EXAMPLE_KEY.get(route.get("side") or "", "acquirer")
    notes = []
    if route.get("side") == "acquired":
        notes.append("This prospect is the company BEING acquired: keep the shape, but write "
                     "from their side (their own ERP after they move onto the buyer's systems).")
    if key == "erp_switch" and not route.get("licensing"):
        notes.append("Fusion / unknown target: use the same structure but DROP the licensing "
                     "sentence; lead on the cost of keeping the old system alive for lookups, "
                     "and on retiring it before cutover so less data migrates.")
    if key == "erp_switch" and not any(k in (route.get("legacy") or "").lower()
                                       for k in _ORACLE_LEGACY):
        notes.append("The old system is NOT confirmed as Oracle: never write 'Oracle' for it "
                     "(say 'the old system' / 'the old ERP').")
    return ("# Client sample sequence (TONE and STRUCTURE only)\n"
            "Value Global wrote this to show shape, length, and voice. It is NOT fixed text: "
            "write fresh sentences from this account's research, never copy its lines (buyers "
            "who compare notes spot a template). {Placeholders} stand for research facts.\n"
            + "".join(f"- {n}\n" for n in notes) + "\n"
            + RETIRE_EXAMPLES[key] + "\n")


_EXAMPLE_WORDS = None


# Sentences carrying a cleared proof: ERP_RETIRE_SYSTEM REQUIRES their exact
# wording, so they never count as copying a sample (PR #31 review).
_CLEARED_PROOF = re.compile(r"infocorvus|\$1\s?m\s?to\s?\$?6\s?m|\b2\s?tb\b|20 to 30", re.I)


def _drop_proof_sentences(text):
    return " | ".join(sent for sent in re.split(r"(?<=[.?!])\s+|\n+", text or "")
                      if not _CLEARED_PROOF.search(sent))


def _example_overlap(body, n=12):
    """The first run of >= n consecutive words a body shares with any client
    sample (placeholders and cleared-proof sentences never match), or ''."""
    global _EXAMPLE_WORDS
    if _EXAMPLE_WORDS is None:
        grams = set()
        for text in RETIRE_EXAMPLES.values():
            text = _drop_proof_sentences(re.sub(r"\{[^}]*\}", " | ", text))
            w = re.findall(r"[a-z0-9$'|]+", text.lower())
            grams.update(" ".join(w[i:i + n]) for i in range(len(w) - n + 1)
                         if "|" not in w[i:i + n])
        _EXAMPLE_WORDS = grams
    w = re.findall(r"[a-z0-9$'|]+", _drop_proof_sentences(body).lower())
    for i in range(len(w) - n + 1):
        if "|" in w[i:i + n]:
            continue
        g = " ".join(w[i:i + n])
        if g in _EXAMPLE_WORDS:
            return g
    return ""


def route_block(route):
    """The routing instructions for the retirement / data-split prompt."""
    side = route.get("side") or ""
    entity = route.get("entity") or ("the acquired company" if side in ("acquirer", "acquired")
                                     else "the separating entity")
    target = route.get("target") or "a new ERP"
    lines = [f"# Messaging track: {route['track'].upper().replace('_', ' ')} "
             f"(deal side / situation: {side.replace('_', ' ') or 'n/a'})",
             "- " + _SIDE_GUIDE.get(side, _SIDE_GUIDE["acquirer"]).format(entity=entity,
                                                                          target=target)]
    legacy = route.get("legacy") or ""
    if route.get("licensing"):
        oracle = any(k in legacy.lower() for k in _ORACLE_LEGACY)
        lines.append(
            "- Licensing: you MAY say the retired system's license and support costs go away "
            "(that is what retirement does). "
            + ("Since the old system is an Oracle product, 'Oracle license and support' is fine."
               if oracle else
               "The old system is NOT confirmed as Oracle: say 'license and support on the old "
               "system', never 'Oracle license'."))
    else:
        lines.append(
            "- Licensing: do NOT mention license, licensing, or support contracts at all "
            f"({route.get('reason') or 'not cleared for this track'}). Lead on the cost of keeping "
            "the old system alive just for lookups, and on retiring it before cutover so only "
            "active data moves." if side == "erp_switch" else
            "- Licensing: do NOT mention license or licensing on this track; the story is a clean "
            "split with transactions still linked, then retiring the leftover copy.")
    if legacy:
        lines.append(f"- The old system is confirmed as {legacy}: you may name it.")
    else:
        lines.append("- The old system is NOT confirmed: never name an ERP product for it "
                     "(say 'the old ERP', 'that system').")
    if side != "erp_switch":
        closed = route.get("closed")
        lines.append(
            "- Transition periods run from CLOSING, never from announcement; never write that a "
            "clock started when the deal was announced. "
            + ("The research confirms the deal has closed."
               if closed is True else
               "The research does NOT confirm the deal has closed: say it was agreed or "
               "announced, never that it closed."))
    return "\n".join(lines) + "\n"


ERP_RETIRE_SYSTEM = """\
You are an expert B2B SDR copywriter for Value Global, delivering application retirement on the
ROAD platform by InfoCorvus.

# What we sell on this track: application RETIREMENT
Core idea: retire the old ERP, keep its reports, stop paying to run it. This prospect is in an
acquisition, a divestiture, or an ERP switch: they are trying to STOP running an old system. Their
question: how do we switch the old ERP off without losing the history and reports finance, audit
and legal still need, and without paying to keep it alive?
What ROAD does, in buyer language (never invent beyond this):
- Moves the old system's full history into a searchable archive with the business context intact:
  look up a supplier and see its purchase orders, invoices, payments and approvals together.
- A typical Oracle EBS retirement includes 20 to 30 pre-built read-only reports. Never inflate it.
- Access is role-based and audit-ready, so the old application can be switched off.
- For a divestiture or carve-out: extracts one entity's records as complete business objects (an
  invoice carries its lines, tax, payments and accounting entries), so nothing is orphaned; the
  leftover copy is then retired.
- Works across Oracle EBS, PeopleSoft, JD Edwards and SAP. Lead with Oracle EBS only where the
  account is confirmed to run it. ROAD cannot archive out of Oracle Fusion or any cloud ERP.

Proof you may use, at most ONE per email and NONE in touch 1, worded exactly:
- "InfoCorvus, whose ROAD platform we deliver, puts it at $1M to $6M a year per retired legacy
  system" (always with that attribution; never as a Value Global promise).
- "One Oracle EBS environment of 2 TB was retired in a month" (anonymous; never a customer name).
- 20 to 30 pre-built reports per typical EBS retirement.
NO other percentage, savings, or case figure. Never "up to 85%".

NEVER pitch the pain of a company STAYING on its ERP: no slow queries, bloated backups, dev or test
clones, month-end close drag, database performance, or "dormant data". Vocabulary: the offer is
RETIREMENT ("retire", "switch off"); "archive" only as the place the history lives, never
"archiving" as the offer.

# Your task
You are GIVEN a VERIFIED buying trigger researched for this account (with its source and date), its
messaging track and deal side, and the sales play. Do NOT search the web. Write a 4-touch cold
EMAIL sequence plus 3 LinkedIn touches anchored on that trigger.

# Write rules
- 35-110 words per email. Two or three short paragraphs separated by a blank line. ONE idea and
  ONE ask per email. Open EVERY email with the contact's first name and a comma.
- Every touch lands as its OWN standalone email, never a threaded reply: a DISTINCT subject per
  touch, no "RE:" or "Fwd:", never reuse one. Roughly 3 to 8 words, plain and specific: subject 1
  the event and the old system, subject 2 retiring it without losing the reports (or the clean
  split), subject 3 the 20 minutes, subject 4 closing the loop.
- Touch 1 (40-80 words): open on the trigger event (what actually happened, with its month or
  date). Then one or two sentences on what TYPICALLY happens to the old system in this situation,
  phrased as a pattern ("most acquirers find...", "the old environment usually..."), never as a
  fact about them. END on ONE genuine question about their plan for that system or its history.
  No meeting ask, no offer, no proof figure, no link.
- Touch 2 (60-110 words): a different angle from touch 1. Lay out the usual options (keep paying
  to run the old ERP for years, or push history into a new system not built to hold it, or for a
  split a scripted extract that breaks the links between invoices, payments and the ledger)
  against retiring it into a searchable archive that carries the reports. At most one proof line.
  Close with a 20-minute ask ("Would 20 minutes be worth it to see whether that applies to
  <Company / the system>?").
- Touch 3 (40-80 words): the 20-minute retirement call, covering three things: which systems can
  be switched off, which reports have to survive, and what running the old system costs until
  then. Name two windows in prose as weekday plus time of day only ("Tuesday morning or Thursday
  afternoon work on my end"); never "this week", today, tomorrow, or a calendar date; never a
  booking link.
- Touch 4 (30-60 words), the breakup: "I will stop here." If the question of what to do with the
  old system comes up later this year, reply any time and we can pick it up. "Happy to stay
  connected either way." No new argument, no asset, no meeting ask.
- NO links on this track. Never offer an assessment, a sample report, a white paper, case
  studies, references, or "how similar companies handled it".
- The trigger verdict, the routing below, and a detected-ERP line (when provided) are your ONLY
  facts about this company. Never invent numbers, dates, names, systems, or internal details. If
  something is inferred, write it as typical ("most acquirers find..."), never as known. Never
  name an ERP product for the old system unless the routing says it is confirmed.
- Never open with "Congrats". No em or en dashes. NO sign-off or trailing name. No hype words
  (revolutionary, game-changing, cutting-edge, seamless, robust, supercharge, unlock, transform).
  Never write "purge", "estate", "AI-powered", or any production-access reassurance. No pricing.
  Vary the wording: let the research supply the specifics instead of stock lines.
- LinkedIn: li_connect is a <=280-char note that observes the event and connects, no pitch, never
  tells them what they face ("huge lift ahead"); li_msg1 a short question about the old system's
  plan; li_msg2 offers the 20-minute retirement call. Same vocabulary: retire, never archiving.
- Set the `signal` JSON field to the trigger finding with its month/date.

""" + OUTPUT_SCHEMA


def erp_play(segment):
    """The effective play for a segment: the committed text with any operator
    edits (Orchestration view) merged per field. Falls back to the committed
    play on any override problem."""
    play = dict(ERP_PLAYS[segment])
    try:
        play.update(I.play_overrides().get(segment, {}))
    except Exception:  # noqa: BLE001 — overrides must never break generation
        pass
    return play


def persona_framing(persona):
    """The effective persona framing line for the prompt: the uniform default,
    or one composed from the operator's per-persona pain/outcome edits."""
    base = PERSONA_FRAMING.get(persona, PERSONA_FRAMING["it-leadership"])
    try:
        o = I.persona_overrides().get(persona) or {}
    except Exception:  # noqa: BLE001
        return base
    if not o:
        return base
    pain = o.get("pain")
    outcome = o.get("outcome")
    parts = []
    if pain:
        parts.append(f"Frame the pain as: {pain}")
    if outcome:
        parts.append(f"The outcome to sell: {outcome}")
    parts.append(
        "Gives: touch 1 the short POV read (send-over ask only), touch 2 the free Data "
        "Lifecycle Assessment, touch 3 a 20-minute call to set it up, touch 4 a breakup that "
        "leaves the read on the table. Write to the role in the title, not a seniority script.")
    return " ".join(parts)


def build_erp_user(contact, segment, verdict, tech_line=None, prior_issues=None, route=None):
    play = erp_play(segment)
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
    route = route or erp_route(segment, verdict, tech_line)
    if route["track"] in RETIRE_TRACKS:
        base += "\n" + route_block(route) + "\n" + retire_example_block(route)
    else:
        mention = erp_mention_block(tech_line)
        if mention:
            base += "\n" + mention
    base += "\nUse the contact's first name in the copy. Write the sequence. Return only the JSON object."
    if prior_issues:
        base += ("\n\nYour previous attempt FAILED these checks:\n- "
                 + "\n- ".join(prior_issues)
                 + "\nFix ALL of them. Return only the corrected JSON object.")
    return base


def erp_signal_line(segment, verdict, route=None):
    """The auditable `signal` string stored on the asset (mirrors the cached-
    signal format): trigger label + headline + date."""
    play = ERP_PLAYS.get(segment) or {}
    label = play.get("label", segment)
    if route and route.get("track") in RETIRE_TRACKS:
        label += f" [{route['track'].replace('_', ' ')}"
        label += f", {route['side'].replace('_', ' ')}]" if route.get("side") else "]"
    line = f"{label}: {verdict.get('headline', '')}".strip().rstrip(":")
    if verdict.get("date"):
        line += f" ({verdict['date']})"
    return line


def lint_erp(email, route=None, linkedin=None):
    """Structural checks for the ERP trigger sequence: length band, no dashes /
    sign-off / pricing / hype, soft question in touch 1, breakup in touch 4.
    No metric requirement — the trigger verdict is the anchor, not proof stats.
    Retirement / data-split routes get the retirement structure + claim rules."""
    route = route if isinstance(route, dict) else {}
    retire = route.get("track") in RETIRE_TRACKS
    issues = _lint_retire(email, route) if retire else _lint_short(email, lo=35, hi=110)
    if issues and issues[0].startswith("missing subject"):
        return issues
    return issues + _hygiene(email, linkedin, route, retire)


# Retirement-track claim rules (Value Global feedback 2026-10-09).
_STAYING_PAIN = re.compile(r"\bslow(er)? quer|\bbackups?\b|\bclon(e|es|ing)\b|month[- ]end|"
                           r"\bdormant\b|\bperformance\b", re.I)
_ARCHIVING_OFFER = re.compile(r"\barchiving\b|\bassessment\b|\bwhite ?paper\b", re.I)
_LICENSE = re.compile(r"\blicen[cs]", re.I)
_PERCENT = re.compile(r"\d\s?%|\bpercent\b", re.I)
# The ONLY figures cleared for the retirement track (client feedback, section 3):
# $1M to $6M (InfoCorvus-attributed), one 2 TB EBS environment retired in a month,
# 20 to 30 pre-built reports. Anything else — $8.2M, 13 weeks, 11.3 TB, 68 TB,
# a different report count — is uncleared until Value Global confirms it.
_DOLLAR = re.compile(r"\$\s?\d[\d.,]*\s?(m|k|b|million|billion)?\b", re.I)
_TB = re.compile(r"\b(\d[\d.,]*)\s?tb\b", re.I)
_REPORTS = re.compile(r"\b(\d+)(?:\s?(?:to|-)\s?(\d+))?\s+(?:pre-?built\s+)?(?:read-?only\s+)?"
                      r"reports\b", re.I)
_DURATION_CLAIM = re.compile(r"\b13\s?weeks\b|\b12\s?(to|-)\s?24\s?months\b", re.I)  # InfoCorvus timelines


def _uncleared_figures(body):
    """Figures on the retirement track other than the three cleared proofs."""
    bad = [m.group(0) for m in _DOLLAR.finditer(body)
           if re.sub(r"[\s,]", "", m.group(0)).lower() not in ("$1m", "$6m")]
    bad += [m.group(0) for m in _TB.finditer(body) if m.group(1) != "2"]
    bad += [m.group(0) for m in _REPORTS.finditer(body)
            if (m.group(1), m.group(2)) != ("20", "30")]
    bad += [m.group(0) for m in _DURATION_CLAIM.finditer(body)]
    return bad


# Copy hygiene shared by every trigger track (client feedback issues 5, 12-15).
# Issue 12: the stock lines the archiving prompt used to dictate verbatim.
_STOCK_LINES = re.compile(
    r"what if we showed you a way to move that dormant data|"
    r"can i send you some more details to show how|data[- ]separation clock", re.I)
# Issue 14: presumptive openers that tell an executive what they face.
_PRESUMPTIVE = re.compile(
    r"\b(huge|big|massive|major|heavy|serious)\b[^.?!\n]{0,40}\b(lift|lot|challenge|undertaking|"
    r"task|headache|job)\b[^.?!\n]{0,20}\bahead\b|\b(lift|work|challenges?) ahead\b|"
    r"\bon your plate\b|\byou('re| are) (facing|about to face|up against)\b|"
    r"\byou('ll| will) (need|have) to\b", re.I)
# Issue 5/8: ERP products named for the prospect without confirmation. Proof /
# capability sentences ("One Oracle EBS environment of 2 TB...") are exempt.
_ERP_PRODUCT = re.compile(r"e-?business suite|\bebs\b|peoplesoft|jd ?edwards|\bjde\b", re.I)
_PROOF_SENTENCE = re.compile(r"\b2\s?tb\b|20 to 30|pre-?built|works across|typical oracle ebs",
                             re.I)
_GREETING = re.compile(r"^\s*([A-Z][\w'’-]*(?: [A-Z][\w'’-]*)?),")


def _greeting_issue(bodies):
    """Issue 13: every touch opens with the same first-name greeting."""
    greets = [(_GREETING.match(b or "") or [None, None])[1] for b in bodies]
    if not all(greets):
        missing = [str(i + 1) for i, g in enumerate(greets) if not g]
        return f"greeting: touch {', '.join(missing)} does not open with the first name"
    if len(set(greets)) > 1:
        return f"greeting: inconsistent across touches ({', '.join(dict.fromkeys(greets))})"
    return None


def _unconfirmed_erp(text, route):
    """Sentences naming an on-prem ERP product when the route has no confirmed one.
    Assets written before routing existed carry no route: not judged."""
    if not route or route.get("legacy"):
        return []
    hits = []
    for sent in re.split(r"(?<=[.?!])\s+|\n+", text or ""):
        m = _ERP_PRODUCT.search(sent)
        if m and not _PROOF_SENTENCE.search(sent):
            hits.append(m.group(0))
    return hits


def _hygiene(email, linkedin, route, retire):
    """Issues 5, 12, 13, 14, 15 across email + LinkedIn."""
    issues = []
    g = _greeting_issue([email.get(f"body{i}", "") for i in range(1, 5)])
    if g:
        issues.append(g)
    fields = [(f"step{i}", email.get(f"body{i}", "")) for i in range(1, 5)]
    fields += [(k, str((linkedin or {}).get(k) or "")) for k in ("li_connect", "li_msg1", "li_msg2")]
    for name, text in fields:
        if _STOCK_LINES.search(text):
            issues.append(f"{name}: stock line reused across accounts "
                          f"('{_STOCK_LINES.search(text).group(0)}'); write it fresh")
        erp = _unconfirmed_erp(text, route)
        if erp:
            issues.append(f"{name}: names {erp[0]!r} but no ERP is confirmed for this account "
                          "(write it as a pattern, never as known)")
    for k in ("li_connect", "li_msg1", "li_msg2"):
        text = str((linkedin or {}).get(k) or "")
        if _PRESUMPTIVE.search(text):
            issues.append(f"{k}: presumptive opener ('{_PRESUMPTIVE.search(text).group(0)}'); "
                          "observe the event and ask, never tell them what they face")
        if retire and _ARCHIVING_OFFER.search(text):
            issues.append(f"{k}: archiving vocabulary on the retirement track (say retire)")
        if retire and L.FORBIDDEN_CTA.search(text):
            issues.append(f"{k}: offers an uncleared asset (sample report)")
        if retire and _LICENSE.search(text) and not (route or {}).get("licensing"):
            issues.append(f"{k}: mentions licensing ({(route or {}).get('reason') or 'not allowed'})")
    return issues


# Transition periods run from CLOSING (client feedback issue 6).
_CLOCK_AT_ANNOUNCE = re.compile(r"\bclock\b[^.?!\n]{0,60}\bannounce", re.I)


def _lint_retire(email, route):
    """The retirement / data-split sequence: shared claim discipline, touch 1
    ends on a genuine question (open-ended allowed) with no meeting ask,
    touches 2 AND 3 ask for the 20-minute retirement call, touch 4 a plain
    breakup (no asset). Bans the staying-on-the-ERP pain, archiving offers
    (assessment / white paper / sample report / links), any percentage, and
    licensing where the route forbids it (Fusion / unknown target, splits)."""
    steps = []
    for i in range(1, 5):
        subj, body = email.get(f"subject{i}", ""), email.get(f"body{i}", "")
        if not subj or not body:
            return [f"missing subject{i}/body{i}"]
        steps.append({"n": i, "subject": subj, "body": body})
    issues = list(L.sequence_issues(steps))
    for s in steps:
        b, n = s["body"], s["n"]
        wc = _wc(b)
        if not (35 - 5 <= wc <= 110 + 10):
            issues.append(f"step{n}: word count {wc} (need 35-110)")
        if L.DASH.search(b):
            issues.append(f"step{n}: em/en dash present (use commas, colons or a full stop)")
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if lines and L.SIGNOFF_LINE.match(lines[-1]):
            issues.append(f"step{n}: trailing sign-off/name (end on the ask, no sign-off)")
        for rx, msg in ((L.PRICING, "pricing language in a cold step"),
                        (L.BANNED, "banned term/phrase (see offer.md ban list)"),
                        (_HYPE, "hype word"),
                        (L.ANALYST_CLAIM, "analyst citation"),
                        (L.ORACLE_RELATIONSHIP, "Oracle co-sell/partner claim"),
                        (L.RECIPIENT_ASSERTION, "asserts a fact about the recipient"),
                        (L.BOOKING, "booking/scheduling link or tool"),
                        (L.CASE_STUDY, "offers a case study / 'how similar companies handled it'"),
                        (L.FORBIDDEN_CTA, "offers an uncleared asset (sample report / pilot)"),
                        (_STAYING_PAIN, "staying-on-the-ERP pain (queries/backups/clones/"
                                        "month-end/dormant/performance) on the retirement track"),
                        (_ARCHIVING_OFFER, "archiving offer/vocabulary on the retirement track "
                                           "(say retire; no assessment or white paper)"),
                        (_PERCENT, "percentage figure on the retirement track"),
                        (_CLOCK_AT_ANNOUNCE, "says a clock started at announcement (transition "
                                             "periods run from closing)")):
            if rx.search(b):
                issues.append(f"step{n}: {msg}")
        if L.URL_CANDIDATE.search(b):
            issues.append(f"step{n}: link present (no links on the retirement track)")
        if L.INFOCORVUS_FIGURES.search(b) and "infocorvus" not in b.lower():
            issues.append(f"step{n}: InfoCorvus figure without the vendor attribution")
        unc = _uncleared_figures(b)
        if unc:
            issues.append(f"step{n}: uncleared figure(s) {unc[:3]} (only $1M to $6M with the "
                          "InfoCorvus attribution, the anonymous 2 TB in a month, and 20 to 30 "
                          "reports are cleared)")
        if not route.get("licensing") and _LICENSE.search(b):
            issues.append(f"step{n}: mentions licensing ({route.get('reason') or 'not allowed'})")
    for s in steps[:3]:  # touch 4's breakup shape is deliberately shared
        lifted = _example_overlap(s["body"])
        if lifted:
            issues.append(f"step{s['n']}: copies the client sample verbatim ('{lifted}...'); "
                          "write fresh wording from this account's research")
    b1 = steps[0]["body"]
    if not b1.rstrip().endswith("?"):
        issues.append("step1: must end on a question about their plan for the old system")
    if L.MEETING.search(b1):
        issues.append("step1: asks for a call (touch 1 never asks for a meeting)")
    if L.INFOCORVUS_FIGURES.search(b1) or re.search(r"\b2\s?TB\b", b1, re.I):
        issues.append("step1: proof figure in touch 1 (none until touch 2)")
    for s in steps[1:3]:
        if not L.MEETING.search(s["body"]):
            issues.append(f"step{s['n']}: must ask for the 20-minute retirement call")
    if L.STALE_WINDOW.search(steps[2]["body"]):
        issues.append(f"step3: stale time reference "
                      f"'{L.STALE_WINDOW.search(steps[2]['body']).group(0)}' (name windows as "
                      "weekday + time of day only)")
    if not L.BREAKUP.search(steps[-1]["body"]):
        issues.append("step4: final step is not a breakup")
    return issues


def _segment_hold(domain, segment):
    """Hold reason when the stored verdict for (domain, segment) says the
    acquired company stays a separate unit (downgraded by the guard, or
    returned non-found by research) — such contacts are HELD, never sent down
    the default path (feedback issue 10)."""
    if not domain or segment != "ma_carveout":
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
    if SG.separate_unit(triggers.get(segment)):
        return "held: acquired company runs as a separate unit: no consolidation, nothing to retire"
    return None


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


def _write_held(contact, segment, signal, route=None, reason=None):
    """A held contact (separate-unit acquisition, below the send floor, wrong
    persona for the track, a name that fails the check) gets a stub asset, so
    ingest records it as `skipped` with the reason instead of a bare "no
    generated file" failure — and any stale copy is overwritten."""
    _atomic_write(contact["contact_id"], {
        "contact_id": contact["contact_id"], "persona": contact.get("persona", "it-leadership"),
        "variant": ERP_VARIANT if segment in ERP_PLAYS else (contact.get("variant")
                                                             or DEFAULT_VARIANT),
        "segment": segment, "signal": signal, "held": True,
        "held_reason": reason or (route or {}).get("reason") or "held",
        # a bad name is a data fix + regenerate (failed); routing holds are final
        "held_status": "failed" if (reason or "").startswith("name check") else "skipped",
        "track": (route or {}).get("track"), "route": route, "email": {}, "linkedin": {}})


def _persist_name_fix(contact, nc):
    """Store a SAFE name repair on the contact (Bison + every later generation
    read the DB name) and keep the original in import_flags for review."""
    def _run():
        conn = db.connect()
        try:
            row = conn.execute("SELECT import_flags FROM contacts WHERE contact_id=?",
                               (contact["contact_id"],)).fetchone()
            try:
                flags = json.loads((row["import_flags"] if row else None) or "[]") or []
            except (ValueError, TypeError):
                flags = []
            flags = [f for f in flags if not str(f).startswith("name_fixed:")]
            flags.append(f"name_fixed:{contact.get('first_name') or ''}")
            conn.execute("UPDATE contacts SET first_name=?, import_flags=? WHERE contact_id=?",
                         (nc["name"], json.dumps(flags), contact["contact_id"]))
            conn.commit()
        finally:
            conn.close()
    try:
        db.retry_locked(_run)
    except Exception as e:  # noqa: BLE001 — the prompt still uses the repaired name
        sys.stderr.write(f"[generate] name fix not persisted for {contact['contact_id']}: {e}\n")
    # copy written under the old name must never survive a failed regeneration
    # (it would ingest + enroll under the repaired row) — drop it now
    try:
        (db.GEN_DIR / f"{contact['contact_id']}.json").unlink(missing_ok=True)
    except OSError as e:
        sys.stderr.write(f"[generate] stale copy not removed for {contact['contact_id']}: {e}\n")


def name_gate(contact):
    """(contact, hold_reason). Greets with the name the contact's email /
    LinkedIn states when the CRM first name disagrees ("Cspa" -> "Stewart"),
    persisting it for review; holds only an unusable name with nothing to
    repair it from (feedback issue 9)."""
    nc = SG.name_check(contact.get("first_name"), contact.get("last_name"),
                       contact.get("email"), contact.get("linkedin_url"))
    if not nc["ok"]:
        return contact, f"name check: {nc['reason']}"
    if nc["fixed"]:
        sys.stderr.write(f"[generate] {contact['contact_id']}: {nc['reason']}\n")
        _persist_name_fix(contact, nc)
        return dict(contact, first_name=nc["name"]), None
    return contact, None


def erp_gate(contact, segment, verdict, route):
    """Why this trigger contact must NOT be generated, or None:
    - route hold (acquired company stays a separate unit — issue 10);
    - verdict score under the trigger's send floor (issue 10);
    - title outside the track's buyers (issue 11): retirement / data split need
      the client's list (CIO, VP/Director ERP or Enterprise Applications, M&A
      Integration, Controller/CFO, Enterprise Architect); archiving needs the
      ERP buyer group."""
    if route["track"] == "hold":
        return f"held: {route['reason']}"
    score = (verdict or {}).get("score") or 0
    if SG.below_floor(segment, score):
        return (f"held: {ERP_PLAYS.get(segment, {}).get('label', segment)} score {score} is "
                f"below the send floor {SG.send_floor(segment)}")
    title = contact.get("title") or ""
    if route["track"] in RETIRE_TRACKS:
        ok, why = BG.retirement_buyer(title)
        if not ok:
            return (f"held: persona {title!r} does not fit the {route['track'].replace('_', ' ')} "
                    f"track ({why}); target {BG.RETIREMENT_BUYERS}")
    elif not (BG.is_icp_buyer(title) or BG.retirement_buyer(title)[0]):
        return f"held: persona {title!r} is outside the ERP buyer group"
    return None


def generate_contact_erp(contact, segment, verdict, client, write=True, tech_line=None):
    """Generate one trigger-anchored contact (write-only, no web search — the
    verdict IS the research). Same retry/lint/write contract as
    generate_contact; the asset carries variant 'erp-trigger'."""
    cid, persona = contact["contact_id"], contact.get("persona", "it-leadership")
    route = erp_route(segment, verdict, tech_line)
    signal = erp_signal_line(segment, verdict, route)
    hold = erp_gate(contact, segment, verdict, route)
    if hold:
        if write:
            _write_held(contact, segment, signal, route, reason=hold)
        return {"status": "failed", "asset": None, "signal": signal,
                "company": contact.get("company", ""), "web_searches": 0, "attempts": 0,
                "issues": [hold], "route": route,
                "cache_read": 0, "cache_write": 0}
    system = ERP_RETIRE_SYSTEM if route["track"] in RETIRE_TRACKS else ERP_SYSTEM
    issues, last_asset = ["no output"], None
    cache_read = cache_write = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            res = client.complete(
                system,
                build_erp_user(contact, segment, verdict, tech_line=tech_line,
                               prior_issues=None if attempt == 1 else issues, route=route),
                use_web_search=False, max_tokens=8192,
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
            "track": route["track"], "route": route,
            "email": data.get("email", {}) or {},
            "linkedin": data.get("linkedin", {}) or {},
        }
        issues = lint_erp(last_asset["email"], route, last_asset["linkedin"])
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
        # Operator edits (Orchestration view) override the committed file;
        # any problem with the override layer falls back to the repo copy.
        parts.append(I.knowledge_override(fname) or (KNOWLEDGE_DIR / fname).read_text())
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
    framing = persona_framing(persona)
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
                        (L.FORBIDDEN_CTA, "offers an uncleared asset (sample report / pilot)"),
                        (L.UNCONFIRMED_FIGURES, "unconfirmed InfoCorvus figure (pending Value "
                                                "Global sign-off; only $1M to $6M is cleared)"),
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
    variant = asset.get("variant", DEFAULT_VARIANT)
    if variant == ERP_VARIANT:
        return lint_erp(asset.get("email", {}), asset.get("route"), asset.get("linkedin"))
    fn = LINTERS.get(variant, lint_email)
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
                use_web_search=use_search, max_web_searches=3, max_tokens=8192,
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

    contact, name_hold = name_gate(contact)
    if name_hold:
        if write:
            _write_held(contact, (contact.get("segment") or "").strip(), "", reason=name_hold)
        return {"contact_id": contact["contact_id"], "status": "failed",
                "issues": [name_hold], "used_cache": True}

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
        held = None if verdict else _segment_hold(domain, seg)
        if held:
            if write:
                _write_held(contact, seg, "", reason=held)
            return {"contact_id": contact["contact_id"], "status": "failed",
                    "issues": [held], "used_cache": True}
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
    res = client.complete(system, user, use_web_search=True, max_web_searches=3, max_tokens=2048)
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
    res = client.complete(system, user, use_web_search=True, max_web_searches=4, max_tokens=4096)
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
        max_tokens=8192, cache_ttl="1h",
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
        c, name_hold = name_gate(c)
        if name_hold:
            sys.stderr.write(f"[generate] {cid} held: {name_hold}\n")
            _write_held(c, seg, "", reason=name_hold)
            continue
        if c.get("variant") == ERP_VARIANT or seg in ERP_PLAYS:
            verdict = _segment_verdict(domain, seg)
            held = None if verdict else _segment_hold(domain, seg)
            if held:
                sys.stderr.write(f"[generate] {cid} ({domain}): {held}\n")
                _write_held(c, seg, "", reason=held)
                continue
            if verdict:
                tech_line, _ = _cached_tech(domain)
                route = erp_route(seg, verdict, tech_line)
                hold = erp_gate(c, seg, verdict, route)
                if hold:
                    sys.stderr.write(f"[generate] {cid} ({domain}): {hold}\n")
                    _write_held(c, seg, erp_signal_line(seg, verdict, route), route, reason=hold)
                    continue
                system = ERP_RETIRE_SYSTEM if route["track"] in RETIRE_TRACKS else ERP_SYSTEM
                requests.append({"custom_id": cid, "params": client.build_body(
                    system, build_erp_user(c, seg, verdict, tech_line=tech_line, route=route),
                    use_web_search=False, max_tokens=8192, cache_ttl="1h")})
                # cached_signal = the verdict line, so process_batch_result stores
                # it verbatim and never re-caches it as a researched signal
                manifest[cid] = {"contact": c, "domain": domain, "variant": ERP_VARIANT,
                                 "segment": seg, "route": route, "was_combined": False,
                                 "cached_signal": erp_signal_line(seg, verdict, route)}
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
    if entry.get("route"):
        asset.update(segment=entry.get("segment"), track=entry["route"]["track"],
                     route=entry["route"])
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
