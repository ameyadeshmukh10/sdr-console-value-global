"""Guardrail linter for generated email sequences — Value Global ERP Data Retirement.

Parses a markdown sequence file with blocks:
    ## Step N — Subject: <subject>
    <body...>
and checks each email against the rules in knowledge/icp-email.md + cta-offers.md + offer.md.
Every check below encodes a documented Value Global rule (most are the product of a failed
live message, not styling): the ban list, claim discipline, the read-first offer ladder, the
standalone-subject rule, and the no-booking-link rule.

Per step: word band 35-110 (enforced with slack at 30-120), paragraph breaks, no sign-off,
no em/en dashes, banned terms, hype, no recipient assertions ("Congrats", "70% of your..."),
no analyst or Oracle-relationship claims, no licensing percentages, no pricing, no booking
links, URL whitelist, at most two credibility stats, InfoCorvus attribution. Step 1 must open
on a question and offer only the read (no meeting, no assessment); step 3 is the only step
that may ask for a call; the final step is a breakup that leaves the asset available.
Sequence-level: every step carries its own standalone subject — each touch lands as its own
email, never a threaded "RE:" reply, and no subject is reused.

Usage:  python3 .claude/skills/ai-sdr/scripts/lint_sequence.py <sequence.md>
"""

import re
import sys
from pathlib import Path

STEP_RE = re.compile(r"^##\s*Step\s*(\d+)\s*[—\-:]+\s*Subject:\s*(.*)$", re.I)
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# The VG gives (the read): required somewhere in step 1.
GIVE = re.compile(
    r"want me to send it over|send it over|short (pov|read|write-?up|point of view)|"
    r"\bpov\b|white paper|two-minute read|glad to share|"
    r"does that match what you are seeing|reply any time and i will send", re.I)
# Undeliverable / banned offer shapes: pilots, samples not yet cleared, price shapes.
FORBIDDEN_CTA = re.compile(
    r"free pilot|pilot (program|project) at no cost|no-?cost pilot|"
    r"sample report|supplier[- ]?360|de-?anonymized|companies that visited", re.I)
# Trailing sign-off / name (the campaign appends the signature; end on the ask).
SIGNOFF_LINE = re.compile(r"^\s*[—\-]?\s*[A-Z][a-z]+\s*$|"
                          r"^\s*(best|thanks|cheers|regards|warmly|sincerely|talk soon)\b", re.I)
# Em dash / en dash: company-wide ban in copy; use commas, colons or a full stop.
DASH = re.compile(r"[—–]")
# A call ask. REQUIRED in step 3, BANNED in steps 1-2 (field-tested: meeting asks in
# touch 1 got no replies). Booking links are a separate, always-banned check.
MEETING = re.compile(
    r"\bshort call\b|\bquick (call|chat)\b|\bhop on\b|\bjump on\b|"
    r"\b(15|20|twenty|fifteen|thirty)[-\s]?min(ute)?s?\b|grab (time|\d+)|"
    r"would (monday|tuesday|wednesday|thursday|friday|next week)|"
    r"\bcall to (set|scope|walk)\b|worth a (short |quick )?call|a (short|quick) call", re.I)
BREAKUP = re.compile(
    r"close (your|the) file|closing the loop|last (note|email|one)|final note|should i close|"
    r"off your radar|move on|i'?ll stop|i will stop|stop (here|reaching out|crowding|cluttering|"
    r"emailing|bugging|nudging|chasing)|won'?t (chase|bug|nudge|keep)|door'?s open|"
    r"circle back later|leave (you|it)", re.I)
# Kept defined for the console's guardrails panel; the metric REQUIREMENT is dropped for VG
# (touch 1 deliberately can carry no number; the 10/20/70 pattern satisfies it when present).
METRIC = re.compile(r"\d+\s?%|\b\d+\s?[-]?\s?\d*\s?x\b|\$\d|\d{2,}\s?(tb|gb)\b", re.I)
PRICING = re.compile(r"/mo\b|per month|\bpricing\b|\bopt-?out\b|\bmonthly (price|fee|cost|rate)\b|"
                     r"\$\s?\d[\d.,]*\s?k?\s*/\s*mo|price (starts|from)|\bquote\b", re.I)

# ---- VG claim discipline and ban list ---------------------------------------
# Banned terms/phrases, each retired for a documented reason (see offer.md).
BANNED = re.compile(
    r"\bpurg(e|ed|es|ing)\b|(?<!real )(?<!real-)\bestates?\b|"
    r"no production (access|impact)|(don'?t|do not|won'?t|never) touch(ing)? (your )?production|"
    r"no access to (your )?production|nothing is deleted without|"
    r"AI[- ]powered|85\s?(-|to)\s?95\s?%", re.I)
# Hype words (shared with generate_batch's checks).
HYPE = re.compile(r"\b(revolutioniz\w*|revolutionary|game[- ]?chang\w*|cutting[- ]?edge|"
                  r"supercharg\w*|unlock\w*|transformati\w*|best[- ]in[- ]class|world[- ]class|"
                  r"seamless\w*|paradigm|synerg\w*|\brobust\b|leverag\w+)\b", re.I)
# Cold copy never cites analysts (the 70% figure is Value Global's view, not a measurement).
ANALYST_CLAIM = re.compile(r"\b(IDC|Gartner|Forrester|Meta Group|Horison)\b|"
                           r"analysts?\s+(have\s+)?(measured|found|showed|say)", re.I)
# No Oracle co-sell / partnership claims (unverified).
ORACLE_RELATIONSHIP = re.compile(r"oracle[’']?s?\s+(co[- ]?sell|partner)", re.I)
# Assertions about the recipient's environment (the documented Email-1 defect class).
RECIPIENT_ASSERTION = re.compile(
    r"\bcongrat|you ?('ve| have) been running|"
    r"\d{1,3}\s?%\s+of your\b|your (database|data|erp|system)[^.?!\n]{0,60}\b(is|are)\b"
    r"[^.?!\n]{0,25}\bdormant", re.I)
# Licensing/footprint percentages stay out of cold copy entirely (the 85% claim is
# non-Fusion-only and too easy to misapply; "license goes to zero" is the safe line).
LICENSING_NUMBER = re.compile(r"\b8[05]\s?%", re.I)
# Booking/scheduling links and tools: never.
BOOKING = re.compile(r"calendly|chili ?piper|cal\.com|savvycal|meetings\.hubspot|"
                     r"book (a )?(slot|time|meeting)|my calendar|calendar link", re.I)
# The only two links allowed in copy.
URL_CANDIDATE = re.compile(r"https?://\S+|www\.\S+", re.I)
URL_ALLOWED = re.compile(r"(www\.)?valueglobal\.net(/archiving-solutions)?/?$|"
                         r"(https?://)?vg-ebs-archiving-assessment-demo\.netlify\.app\S*", re.I)
BAD_DOMAIN = re.compile(r"valueglobal\.com", re.I)
# Credibility stats: at most TWO per email.
STATS = re.compile(r"100\+\s?clients|300\+\s?projects|1,?500\+\s?processes|"
                   r"\b20\+?\s?years\b|50\+\s?Oracle|2\s?(-|to)\s?5x", re.I)
# InfoCorvus published figures must carry the attribution in the same body.
INFOCORVUS_FIGURES = re.compile(r"\$8\.2\s?M|\$1\s?M\s?(-|to)\s?\$?6\s?M|"
                                r"40\s?(-|to)\s?60\s?%|60\s?(-|to)\s?80\s?%|13 weeks", re.I)
# The assessment must not appear in touch 1 (field-tested failure).
ASSESSMENT = re.compile(r"assessment", re.I)
# Time references that go stale between writing and sending (sequences send days
# or weeks after generation): never anchor a call window to "this week", today/
# tomorrow, or a calendar date. Weekday + time of day ("Tuesday morning") is fine.
STALE_WINDOW = re.compile(
    r"\bthis week\b|\btoday\b|\btomorrow\b|"
    r"\b(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+\d{1,2}\b|\b\d{1,2}/\d{1,2}\b", re.I)
# Case-study / reference offers: no case studies exist for this offering yet.
CASE_STUDY = re.compile(
    r"case stud(y|ies)|success stor(y|ies)|customer stor(y|ies)|reference customers?|"
    r"how (other|similar) (companies|acquirers|teams|firms|organizations)[^.?!\n]{0,40}"
    r"(handled|did|approached|managed|solved)|"
    r"share how similar|similar (acquirers|companies|teams) handled", re.I)
# Open-ended discovery questions as the touch-1 close (documented failure: cold
# touch 1 never ends on "how are you thinking about X"; the ask is a closed
# permission question).
OPEN_ENDED = re.compile(
    r"how are you (thinking|planning|approaching)|how do you (plan|intend|think)|"
    r"what('s| is) your (plan|approach|thinking|strategy)|"
    r"curious (how|what|where|whether)|how are you handling", re.I)


def sentences(text):
    return [s.strip() for s in SENT_SPLIT.split(text or "") if s.strip()]


def parse_steps(md):
    steps, cur = [], None
    for line in md.splitlines():
        m = STEP_RE.match(line.strip())
        if m:
            if cur:
                steps.append(cur)
            cur = {"n": int(m.group(1)), "subject": m.group(2).strip(), "body": []}
        elif cur is not None:
            if line.strip().startswith("## "):  # next non-step heading ends the block
                steps.append(cur)
                cur = None
                continue
            cur["body"].append(line)
    if cur:
        steps.append(cur)
    for s in steps:
        s["body"] = "\n".join(s["body"]).strip()
    return steps


def sequence_issues(steps):
    """Sequence-level checks: every touch is its own standalone email with its
    own subject — never a threaded 'RE:' reply, and no subject reused."""
    issues = []
    if not steps:
        return ["no steps"]
    seen = {}
    for s in steps:
        n = s.get("n", "?")
        subj = (s.get("subject") or "").strip()
        if not subj:
            issues.append(f"step{n}: missing subject (every touch needs its own subject line)")
            continue
        if re.match(r"^\s*(re|fwd?)\s*:", subj, re.I):
            issues.append(f"step{n}: subject starts with 'RE:' (each touch is its own email, "
                          "never a threaded reply; write a standalone subject)")
        key = subj.lower()
        if key in seen:
            issues.append(f"step{n}: subject duplicates step{seen[key]}'s "
                          "(each touch needs a distinct subject line)")
        else:
            seen[key] = n
    return issues


def lint_email(step, is_last, is_first=False):
    body = step["body"]
    n = step.get("n") or (1 if is_first else (4 if is_last else 2))
    wc = len(re.findall(r"[A-Za-z0-9']+", body))
    issues = []

    # Band 35-110 with slack: word count is a style guide, not a quality signal.
    if not (30 <= wc <= 120):
        issues.append(f"word count {wc} (need 35-110)")

    if "\n\n" not in body:
        issues.append("no paragraph breaks (separate the question / pattern / ask with blank lines)")

    body_lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if body_lines and SIGNOFF_LINE.match(body_lines[-1]):
        issues.append(f"trailing sign-off/name '{body_lines[-1]}' (end on the ask, no sign-off)")

    if DASH.search(body):
        issues.append("em/en dash present (company ban; use commas, colons or a full stop)")
    if BANNED.search(body):
        issues.append(f"banned term/phrase: '{BANNED.search(body).group(0)}' (see offer.md ban list)")
    if HYPE.search(body):
        issues.append(f"hype word: '{HYPE.search(body).group(0)}'")
    if ANALYST_CLAIM.search(body):
        issues.append("analyst citation (cold copy never cites analysts for the 70% pattern)")
    if ORACLE_RELATIONSHIP.search(body):
        issues.append("Oracle co-sell/partner claim (unverified; never write it)")
    if RECIPIENT_ASSERTION.search(body):
        issues.append("asserts a fact about the recipient (no 'Congrats', no tenure claims, "
                      "no '70% of your...'; state the pattern generally, then ask)")
    if LICENSING_NUMBER.search(body):
        issues.append("licensing/footprint percentage in cold copy (use 'the retired "
                      "environment's license goes to zero' instead)")
    if BOOKING.search(body):
        issues.append("booking/scheduling link or tool (never; name two windows in prose)")
    if BAD_DOMAIN.search(body):
        issues.append("valueglobal.com (the domain is valueglobal.net)")
    for url in URL_CANDIDATE.findall(body):
        if not URL_ALLOWED.match(url.rstrip(".,)")):
            issues.append(f"link not on the whitelist: {url[:60]} (only "
                          "valueglobal.net/archiving-solutions and the assessment demo)")
    if len(STATS.findall(body)) > 2:
        issues.append("more than two credibility stats in one email (max two)")
    if INFOCORVUS_FIGURES.search(body) and "infocorvus" not in body.lower():
        issues.append("InfoCorvus published figure without the vendor attribution")
    if FORBIDDEN_CTA.search(body):
        issues.append("banned offer (no pilots; sample report not yet cleared)")
    if PRICING.search(body):
        issues.append("pricing language in a cold step (replies only, and only the "
                      "'assessment produces the number' shape)")

    cta = " ".join(sentences(body)[-3:])
    n_q = body.count("?")
    if n_q > 3:
        issues.append(f"{n_q} questions (too many asks; one ask per email)")

    if n == 1:
        opener = " ".join(sentences(body)[:2])
        if "?" not in opener:
            issues.append("step 1 must open on a question, never a claim")
        if not (GIVE.search(cta) or GIVE.search(body)):
            issues.append("step 1's only ask is permission to send the read "
                          "('want me to send it over?' / 'does that match what you are seeing?')")
        if ASSESSMENT.search(body):
            issues.append("assessment mentioned in step 1 (the read comes first; "
                          "the assessment is the step-2 ask)")
    if n in (1, 2) and MEETING.search(body):
        issues.append(f"step {n} asks for a call (only step 3 may; field-tested rule)")
    if n == 3 and not MEETING.search(body):
        issues.append("step 3 must ask for the short call (20 minutes, two windows in prose)")
    if n == 3 and STALE_WINDOW.search(body):
        issues.append(f"stale time reference '{STALE_WINDOW.search(body).group(0)}' "
                      "(the email sends days after writing; name windows as weekday + "
                      "time of day only, e.g. 'Tuesday morning or Thursday afternoon')")
    if n in (2, 3) and n_q == 0:
        issues.append("no clear ask (no question)")

    if is_last:
        if not BREAKUP.search(body):
            issues.append("final step is not a breakup")
        if not GIVE.search(body) and not re.search(r"send it|write-?up|white paper|pov|read",
                                                   body, re.I):
            issues.append("breakup must leave the asset on the table ('reply any time and "
                          "I will send it')")

    return wc, issues


def main():
    if len(sys.argv) < 2:
        print("usage: lint_sequence.py <sequence.md>")
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"ERROR: {path} not found")
        return 2

    md = path.read_text()
    steps = parse_steps(md)
    if not steps:
        print("ERROR: no '## Step N — Subject: …' blocks found")
        return 2

    all_pass = True
    print(f"Linting {path.name} — {len(steps)} emails\n")
    for i, step in enumerate(steps):
        is_last = i == len(steps) - 1
        wc, issues = lint_email(step, is_last, is_first=(i == 0))
        status = "PASS" if not issues else "FAIL"
        if issues:
            all_pass = False
        print(f"  Step {step['n']} [{status}] ({wc}w) — {step['subject']}")
        for it in issues:
            print(f"      ✗ {it}")

    for it in sequence_issues(steps):
        all_pass = False
        print(f"\n  Sequence-level ✗ {it}")
    print()
    print("ALL PASS ✅" if all_pass else "FAILURES ❌")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
