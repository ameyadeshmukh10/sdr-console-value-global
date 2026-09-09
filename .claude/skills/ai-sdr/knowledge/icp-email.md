# ICP Email Generation Rules (email channel, Value Global buyer group)

How to write a 4-touch outbound email sequence for a Value Global ICP lead. Grounded in
`offer.md` (the offer, proof, voice) + `cta-offers.md` (the offer ladder). Scope: **email only,
ICP buyer group only** (ERP/application owners, database owners, data governance, IT
leadership; verify with `scripts/buyer_group.py`). Messaging is uniform across seniority by
client decision: write to the role, not a seniority script.

## Inputs the generator needs
- Lead: first name, **title** (must pass `is_icp_buyer`), company.
- Optional: a **detected on-prem ERP** (Oracle EBS, PeopleSoft, JD Edwards) from the tech scan,
  and/or a **verified trigger verdict** (M&A carve-out, ERP migration, license audit, EBS on
  OCI, EBS performance). No cold congrats-research: the anchor is the role + the pattern +
  whatever verified context is provided. Industry is a filter, never a copy variant.

## Subject rule
- Subject 1 is the approved line, verbatim: **What's it costing to keep data nobody touches?**
  (A trigger play may supply its own subject 1.) It is field-tested; do not replace it casually.
- Subjects 2-4 are `RE: ` + subject 1, verbatim. The thread is the asset.

## The 4-touch structure
| Step | Job | CTA |
|---|---|---|
| 1 | Question opener: the cost-of-cold-data question, the 10/20/70 pattern stated about long-running EBS systems in general (never the recipient's), anchored to the person's verifiable role; name the detected ERP naturally when one is provided | The POV send-over ask only: "Want me to send it over?" or "Does that match what you are seeing?" |
| 2 | The cost of doing nothing: the hardware treadmill, close and DR windows stretching, "hardware buys time, not a fix"; then the free Data Lifecycle Assessment described by its five deliverables | Propose the assessment + the link www.valueglobal.net/archiving-solutions |
| 3 | No new argument, no new proof; short and specific | A 20-minute call to set the assessment up, two windows named in prose, never a booking link |
| 4 | The breakup: concede timing, no pressing; leave the read on the table for them "or on someone else's desk" | "Reply any time and I will send it" |

## Per-email recipe — short paragraphs separated by BLANK LINES
1. **Paragraph 1 — the question (1-2 sentences):** open with the question, never a claim.
   "A question I keep putting to [role] running [platform / an ERP that has been in place for
   many years]..." Anchor to the person's role, which is verifiable, not their systems, which
   are not.
2. **Paragraph 2 — the pattern (1-2 sentences):** state it generally: "In most long-running
   EBS systems, that rarely-used history is the bigger share of the database. It costs the same
   as the data your team uses daily."
3. **Paragraph 3 — the ask (1 sentence):** exactly one ask, from the offer ladder for that
   touch. End on the ask.
4. When a detected ERP is provided, name it once, naturally, with the 15-to-20-years
   data-volume angle as a question. Never mention scanning, detection, or how we know.

## Formatting
- **Separate paragraphs with a blank line** (`\n\n` in the JSON). No dense blocks.
- **NO sign-off and NO name at the end.** The Email Bison campaign appends the sender's
  signature (sender identity: Value Global business development; being finalized). End on the ask.
- **NEVER use em dashes (—) or en dashes (–).** Company-wide standard: commas, colons, or a
  full stop. (Regular hyphens in words like "month-end" are fine.)
- Plain text only. No markdown, no bullet lists, no images (the campaign template carries the
  10/20/70 infographic).

## Hard guardrails (enforced by `lint_sequence.py`)
- **35-110 words** per email (the approved set runs about 45-105). **One** ask per email.
- Step 1 must open on a question and must NOT ask for a meeting, the assessment, a pilot, or
  anything beyond permission to send the read.
- Never assert a fact about the recipient's environment: no "70% of your database", no "you
  have been running the same ERP for a decade", no "Congrats" opener.
- Step 3 is the only step that may ask for a call. Step 4 must be a breakup that leaves the
  asset available.
- The only links allowed: www.valueglobal.net/archiving-solutions and the assessment demo
  (https://vg-ebs-archiving-assessment-demo.netlify.app/index.html). Never valueglobal.com,
  never a booking link.
- At most TWO credibility stats per email. Every InfoCorvus figure carries its attribution.
- The licensing rule: never claim archiving cuts the Oracle license for a customer staying on
  EBS; license-to-zero belongs to Full Retirement only. No "85%" or "80%" figures in cold copy.
- No pricing in any cold step. No production-access reassurance, ever.
- Banned terms per `offer.md`: purge, estate, hype words, "AI-powered", "85-95% dormant",
  analyst citations, Oracle co-sell claims.
- Every product claim traceable to `offer.md`.

## Tone
A consultant who has done this work for twenty years talking to a peer. Short sentences,
concrete nouns, no hype. Emulate the three sample openers in `offer.md` and the gold example
in `examples/icp-email-sequence.md`: emulate the shape, never copy a real prospect's specifics.

## Output format (so the linter can read it)
Markdown, one block per step:
```
## Step N — Subject: <subject>
<body>
```
