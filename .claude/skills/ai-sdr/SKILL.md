---
name: ai-sdr
description: Generate and iterate on outbound for Value Global's ERP Data Retirement service (ROAD by InfoCorvus), sold to the owners of aging Oracle ERP systems (ERP/application owners, DBAs, data governance, IT leadership) at $500M+/1,000+ US-CA enterprises. Use to write read-first cold email sequences, test the CTA A/B arms, or draft replies to interested leads. Grounded in the client knowledge base.
---

# AI SDR — read-first outbound engine (Value Global)

Writes outbound and handles replies for **Value Global's ERP Data Retirement service** (Oracle
EBS archiving and retirement on the InfoCorvus ROAD platform), the only thing this deployment
sells, to the only buyers it targets. Everything is grounded in the knowledge base — **never
invent product claims, numbers, or signals.**

## Scope (ruthlessly narrow — by design)
- **Offer:** ERP Data Retirement only. **Channel:** email first (LinkedIn deferred until the
  client's sender profiles are activated).
- **ICP:** $500M+ revenue, 1,000+ employee US/Canada enterprises on long-running Oracle EBS
  (JDE/PeopleSoft secondary). Fusion-only accounts are suppressed. Healthcare is out.
- **Buyer group only:** ERP/application owners, database owners, data-governance/records
  owners, IT leadership (CIO/VP/Director IT). Director/manager level is the primary target —
  this deal is not won in the C-suite. Verify any lead with `scripts/buyer_group.py` — if
  `is_icp_buyer` is False, **don't write to them.** Messaging is uniform across personas by
  client decision.
- **The give is the read.** Touch 1 asks only for permission to send the POV; the assessment
  is the touch-2 ask; the 20-minute call is touch 3. No booking links, ever.

## Knowledge base (read before generating)
- `knowledge/offer.md` — the offer, the 10/20/70 story, proof with attribution rules,
  objections, competition, claim discipline, the ban list. The single source of truth.
- `knowledge/cta-offers.md` — the offer ladder (POV → assessment → call) + anti-patterns.
- `knowledge/icp-email.md` — the email generation recipe + hard guardrails.
- The approved-voice reference: the three sample openers in `offer.md` and the gold example.

## Workflows

### A. Generate an email sequence
1. Confirm the lead is ICP: `echo "<title>" | python3 .claude/skills/ai-sdr/scripts/buyer_group.py`.
2. No congrats-research. The anchor is the role, the detected ERP platform (when the tech scan
   provides one — name it per the ERP mention rule), any verified trigger verdict, and the
   general pattern.
3. Read `offer.md` + `cta-offers.md` + `icp-email.md`, then write a **4-touch** sequence
   (question+POV offer → cost-of-nothing+assessment → 20-minute ask → breakup that leaves the
   read) in the output format below.
4. **Lint and revise to pass:**
   `python3 .claude/skills/ai-sdr/scripts/lint_sequence.py <file.md>` (35-110w, question
   opener, read-only ask in touch 1, RE: subject chain, the full ban list, breakup in step 4).
5. Template + a passing reference: `examples/icp-email-sequence.md`.

### B. Test the CTA arms (the iteration loop)
The A/B is the CTA itself (the client's own guidance): POV send-over (`value-give`) vs the
resonance check (`earn`) vs the white-paper track (`show`). Variants in
`examples/cta-variants.md`. Track positive replies separately from replies; discovery calls
booked is the goal metric. Promote the winner in `cta-offers.md`.

### C. Handle an interested reply
1. Classify intent (send-it-over / assessment-yes / pricing / objection / referral).
2. Draft a grounded reply — **deliver the read first**, then the next rung of the ladder.
   Examples: `examples/reply-handling.md`. A human (Value Global) runs discovery: carry the
   conversation to the booked call and no further.
3. **Pricing only on a direct ask, and never a figure:** it depends on database size, module
   scope and pattern; the free assessment produces the number; then propose the call.

## Output format (lint-readable)
```
## Step N — Subject: <subject>
<body 35-110 words, one ask>
```

## Guardrails (non-negotiable)
- ICP buyer group only; ERP Data Retirement only; the claim discipline in `offer.md` applies
  to every asset (licensing rule, attribution rules, two-stat cap, the ban list, no em dashes).
- Read-first CTAs; never a meeting or assessment ask in touch 1; never a booking link.
- Every claim traceable to `offer.md`; no fabricated signals/stats; no pricing in cold steps.
