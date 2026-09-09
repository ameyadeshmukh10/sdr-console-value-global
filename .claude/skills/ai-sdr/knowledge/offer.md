# Context Engine — Value Global ERP Data Retirement (the offer)

Source of truth for what we sell, the proof we may cite, and the claims discipline.
Derived from Value Global's AI-SDR intake brief (2026-09-01) and the approved message set.
Never invent claims or numbers beyond this file.

## One-liner

**"Retire decades of Oracle EBS data without paying Oracle to hold it."**

Service line: **Value Global Oracle EBS Archiving and Retirement Service, powered by InfoCorvus
ROAD.** Longer version when a sentence of context is needed: "Every Oracle EBS environment has a
data lifecycle. We manage it end to end: live archiving while you run, full retirement when you
are ready, and audit-grade historical access throughout, all on a PostgreSQL-based, license-free
archival foundation."

Value Global consults and delivers; InfoCorvus ROAD is the platform. The one cleared partner
phrase: "a premier implementation partner for the InfoCorvus ROAD platform." The company domain
is **valueglobal.net** (never valueglobal.com).

## The core story (the campaign spine — lead every message here)

A long-running Oracle EBS database keeps every year of history live on premium infrastructure.
Over 15 to 20 years it grows until only a fraction is actually used. In a typical long-running
EBS database, roughly **10% of the data is active, 20% is aging, and 70% is dormant**: retained
for tax, SOX and legal hold, and rarely touched. The sharpened insight, and the better one to
write from:

> **All data is not the same, but it is all being paid for the same way.** The 70% that is cold
> gets the same infrastructure, the same backups, the same disaster recovery and the same cost
> as the 30% the business actually uses.

Three bills come due:

1. **Cost, as a hardware treadmill.** Storage, compute, backup and DR capacity all scale with
   database size. IT keeps buying hardware just to hold response times steady. "Hardware buys
   time, not a fix. The data keeps growing. Archiving removes the cause."
2. **Performance.** Period close drags. DR windows overrun. Commit failures and table locks
   appear. Symptoms of an oversized database, not of bad configuration. "Archiving is SLA
   protection, not just cost reduction."
3. **Compliance.** Tax, SOX and litigation holds mean the history must stay accessible. Without
   a governed archive, "accessible" means "live on premium infrastructure."

**The licensing accuracy rule (hard rule).** Oracle licensing is metered on cores and users,
NOT on data volume. Never write that dormant data drives licensing cost. The cost story for a
customer staying on EBS is hardware and infrastructure. Licensing savings belong to Full
Retirement only, where the retired environment's Oracle license and support go to zero.

**Buyer psychology (get this right; the wrong version insults them).** Buyers do not act, not
because the cost is acceptable and not because nobody has measured it. They do not act because
acting feels more expensive and riskier than doing nothing. Never write "weighing you down more
than you realize" or anything that tells the reader what they do not know. Our job is to prove
acting is the safer path, with a real number from their own environment.

## How it works (five steps)

1. Connect to the source database over JDBC using a read-only credential. No agent installed.
2. Identify the historical data to move.
3. Extract it preserving complete business objects: an invoice travels with its lines, tax
   entries, accounting entries, payments and every child record (object-tree extraction).
4. Validate before anything is removed: row counts, referential integrity checks,
   subledger-to-GL reconciliation, and a customer sign-off gate. (Describe this as validation
   strength; never write "nothing is deleted without your sign-off" — see the ban list.)
5. Write to a separate archive, by default open-source PostgreSQL, which needs no Oracle
   license to hold it. Then generate 20 to 30 pre-built read-only reports over the archive.

## Three deployment patterns (all live)

| Pattern | The buyer's situation | The effect |
|---|---|---|
| **Live Archiving** | Staying on EBS. Bloated database, degraded performance, license-audit pressure. | Production footprint shrinks. Faster backups, smaller clones, fewer cores. |
| **Full Retirement** | Migrating to a new ERP, or already migrated and paying to keep EBS alive for compliance reads. | Oracle license on that environment goes to zero. Maximum savings. |
| **Database Split** | M&A divestiture or business-unit separation. | Clean isolated data with relational integrity preserved, production untouched. |

**Lead offer for cold outbound: none of the three.** Lead on the pain and offer the point of
view (see `cta-offers.md`).

## Scope

- **Oracle E-Business Suite is the sweet spot and the lead platform**: roughly 18 to 20
  pre-packaged EBS module accelerators (GL, AP, AR, XLA, Order Management, Inventory, Cash
  Management, Fixed Assets and others) compress a year-long custom build into weeks.
- **JD Edwards and PeopleSoft are in scope but secondary** (no accelerators, weaker economics).
  Lead every message on EBS; the ERP-agnostic point of view carries the rest.
- **ROAD does NOT archive out of Oracle Fusion or any cloud ERP.** Never imply it can.
  Fusion-only accounts are suppressed upstream and never messaged. A prospect moving TO Fusion
  is an ERP-migration play: the archive takes the legacy history the new platform will not load.
- Geography: United States and Canada only. Target: $500M+ revenue, 1,000+ employees.
  Healthcare accounts are out of ICP.

## Proof

- **~10% active / ~20% aging / ~70% dormant** — the lead proof. Value Global's experienced view
  of the TYPICAL long-running EBS database; write it as "in most long-running EBS systems"
  paired with a question, NEVER as "70% of your database is dormant." If asked where it comes
  from: Value Global's delivery experience, plus retention logic (the active window stays flat
  while history compounds). Never claim an analyst measured it.
- **A 2 TB EBS environment retired in one month, data-preparation time cut roughly 90% versus
  custom scripts** — a Value Global client engagement; name withheld, keep it anonymous.
- **11.3 TB reduction from the top ten tables alone in a 34 TB database** under a 10-year
  retention rule — "a 34 TB manufacturer"; never name the account.
- **20 to 30 pre-built read-only reports** per typical EBS retirement — deliberately modest;
  never inflate.
- **Roughly 18 to 20 EBS module accelerators** — Value Global.
- **Oracle's own EBS environment saved 68 TB** through data optimization — public Oracle example.
- **$1M to $6M annually per retired legacy application** — InfoCorvus, the platform vendor's
  published estimate; must carry the attribution.
- **$8.2M annual costs eliminated at a Fortune 500 financial services firm** (three legacy ERP
  retirements) — InfoCorvus published case; must carry the attribution.
- **40-60% reduction in cloud storage costs; 60-80% reduction in application retirement
  timelines; 13 weeks to retire source systems versus 12 to 24 months** — InfoCorvus; each must
  carry the attribution.
- **Credibility stats** (company-level): 20 years in business, 100+ clients, 300+ projects,
  1,500+ processes, 50+ Oracle engagements, 2-5x typical ROI — use at most TWO per message.
- **IRR 30% to 45%** on hard savings for a mid-size ERP, payback under three years —
  illustrative modeled range only; never exceed it, never attach dollar figures in outreach.
- WITHHELD, never use: any "85-95% dormant" figure; named customers; "analysts measured 70%";
  the "up to 85% Oracle licensing reduction" figure stays out of cold copy entirely (it is
  non-Fusion-only and too easy to misapply; "the retired environment's license goes to zero"
  is the safe, stronger line).

## Objection → response

| Objection | Response |
|---|---|
| "Why not archive into tables in the same database?" | Solves nothing structural: the data stays on production-tier storage, backup and recovery windows do not improve, and hot and cold tables are co-mingled in the production schema. A separate PostgreSQL archive shrinks production, every DEV/TEST/UAT clone, and the Oracle exposure at once. |
| "Why not have our DBA write scripts?" (the most common) | A DBA can extract rows. Two things are hard to reproduce: secure governed access and business-context reporting. Flat files give tables, not retention/legal-hold/role policies, and not a supplier-360 view. The line that lands: **"If it only exposed tables, you could build that yourself. It preserves the business context, which is the hard part."** |
| "We have Snowflake / Databricks. Same thing?" | No: this is the governance layer, not a warehouse. Flat-file ETL strips relational context, which breaks audit trails and legal hold. Snowflake may have the data, but can it reconstruct an invoice with all its lines, tax entries, payments and GL entries when an auditor asks? |
| "We won't sign a perpetual SaaS licence." | You do not have to: a one-time retirement is a services accelerator with a fixed nominal platform fee for the project duration, no ongoing licensing after close. |
| "If we archive it, do we lose access?" | Archiving is not deletion. History moves into a governed, low-cost, license-free archive that stays queryable: searchable, reportable, role-controlled, full referential context intact. The archive is a live asset for reporting and audit. |
| "Capital cost of the archive?" | Modest, typically offset by the primary-hardware reduction in year one. Deploys on-prem or any major cloud; targets open-source PostgreSQL, so no Oracle licence to hold it. |
| "Does the assessment touch production?" | **Answer ONLY when asked; never volunteer it.** It reads metadata from a non-production system, table and module sizes only, nothing extracted or changed. Then return to what the assessment will show them. |

## Competition

Two lanes; identify which one a prospect is in. *Archival/retirement lane:* OpenText
InfoArchive, IBM InfoSphere Optim, Informatica Data Archive, Solix (perceived as heavy,
consulting-intensive, months long). *Pipeline lane:* GoldenGate, ODI, Informatica, generic ETL
(moves data without audit-defensible ERP structure). *The one that actually wins deals:* a
custom in-house or offshore build (cheaper on paper; no governance, no referential integrity,
no reconciliation, no compliance artifacts, one person who understands it).

Why we win, three and only three: (1) speed from the EBS accelerators; (2) business context,
not just tables (object-tree extraction + prebuilt reports — the strongest differentiator);
(3) no lock-in on the archive (open-source PostgreSQL destination). Do NOT differentiate on
storage cost, backup windows, performance, masking, legal hold, eDiscovery, or connectors:
table stakes, reads as noise.

## Pricing rules

Say nothing about price in cold outreach; hold all of it for the call. If a prospect asks in a
reply: it depends on database size, module scope and deployment pattern, and the free
assessment produces the number; then propose the call. One shape citable without figures: a
one-time retirement does not require a perpetual software subscription. No tiers or rates
exist in this file by design.

## Voice, banned and preferred lexicon

**Tone:** plain, technical, senior, direct. A consultant who has done this work for twenty
years talking to a peer. Short sentences. Concrete nouns. No adjectives doing work a number
should do. Open with a question, never a claim. State patterns about EBS systems generally,
never about the recipient's environment. Anchor to something verifiable about the person's
role. The touch-1 ask is only permission to send a short read.

**Banned outright:** "purge" (sounds like destruction); "no production impact/access" and every
production-safety reassurance including "nothing is deleted without your sign-off" (reactive
answer only — messages that volunteered it got zero replies); "estate" (insider jargon);
"revolutionary", "cutting-edge", "game-changing", "seamless", "robust", "leverage" as a verb
(hype); "AI-powered" for this offer; "85-95% dormant"; analyst-measured-70% claims; Oracle
co-sell claims; "Congrats" openers and any assertion of the prospect's ERP tenure, size or
spend; em and en dashes in generated copy (company-wide standard: commas, colons or a full
stop); booking links; price.

**Preferred lexicon:** dormant data · cold data · archive candidates · data lifecycle ·
defensible retention · decommission · retire the legacy system · governed archive · referential
integrity · business context · audit-ready · the hardware treadmill · "dormant, not disposable"
· "archiving is SLA protection, not just cost reduction" · "hardware buys time, not a fix".
Use the buyer's own words for their systems: "your ERP system," "your Oracle environment,"
"a large, complex environment."

**Three sample openers in the ideal voice** (real Value Global outreach; match the style, do
not copy the specifics):

> "A question I keep putting to IT leaders on long-standing Oracle EBS. How much of your
> database is history you have to keep but rarely touch, still on production infrastructure and
> backed up like your active data? In most EBS systems past 10 years, that rarely-used history
> is the bigger share of the database. It costs the same as the data your team uses daily. If
> that is a live issue, I have written a short POV on it. It covers how companies move that cold
> data off the running system while keeping it searchable for audit and tax. Want me to send it
> over?"

> "A question for someone who owns Oracle data governance across a business in 13 countries.
> How much of the historical GL and investment-accounting data you keep for audit still lives
> on full Oracle infrastructure, long after it is operationally needed? For firms that have run
> Oracle for years, that retained history is usually the larger part of the footprint and cost,
> with little daily use. If that is on your radar, I have a short write-up on it. Glad to share
> it. Does that match what you are seeing?"

> "Thanks, here it is. It is a two-minute read. The chart on the first page is the whole point.
> In most long-running EBS systems the dormant history is around 70% of the database. It costs
> the same as the data you use every day. If that looks like your environment, the natural next
> step is a free Data Lifecycle Assessment. It puts a real number against your own system rather
> than a general pattern. Want me to set that up?"
