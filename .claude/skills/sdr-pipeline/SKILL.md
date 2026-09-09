---
name: sdr-pipeline
description: Autonomous outbound pipeline — ingest contacts (CSV audiences or a HubSpot list), run signal intelligence (ERP technographics + the five ROAD triggers), route each contact to a persona agent that writes Value Global ERP Data Retirement copy, then enroll into an Email Bison email campaign (subject1-4/body1-4 custom variables) and a HeyReach LinkedIn campaign. Sells Value Global's ERP archiving/retirement service to the owners of aging Oracle ERP systems at US/Canada enterprises. Use to run or operate this pipeline.
---

# SDR Pipeline — CSV/HubSpot → signal intel → persona agents → Email Bison + HeyReach

Operationalizes the `ai-sdr` engine end to end. Ingest contacts (CSV audiences are the primary
source — the client has no CRM) → signal intelligence researches each account (ERP
technographics, the five ROAD news triggers, hiring) → the human approves segments → each ICP
contact routes to a **persona subagent** that writes the copy → **enroll** into one Email Bison
campaign and one HeyReach campaign. Offer: **Value Global ERP Data Retirement only**. Audience:
**US/Canada enterprise IT, ERP, database, and data-governance owners** (`buyer_group.py`
enforces this; the account suppression list and the Fusion-only rule block do-not-contact
accounts at ingest, the segment gate, and enrollment).

Bison instance: **`send.everworker.ai`** (token in `.env`).

## One-time setup
1. **`.env`:** set `BISON_CAMPAIGN_ID` to the client's Bison campaign (single-campaign routing;
   optional per-persona/per-variant envs exist for later). `HUBSPOT_LINKEDIN_PROPERTY` defaults
   to `hs_linkedin_url` (HubSpot is dormant — no CRM for this engagement).
2. **The Bison campaign** must have 4 sequence steps whose subject/body reference the custom
   variables `{{subject1}}`/`{{body1}}` … `{{subject4}}`/`{{body4}}`, plus the sender signature.
3. **LinkedIn (HeyReach) is DEFERRED** until the client's personal-profile senders are ready.
   `HEYREACH_CAMPAIGN_ID` / `…_ACCOUNT_ID` are blank, so enrollment skips LinkedIn. To enable
   later: set those, and the HeyReach campaign's message steps must reference custom fields
   `{{li_connect}}`, `{{li_msg1}}`, `{{li_msg2}}`.
4. **Load the do-not-contact list before the first send** (hard client requirement):
   `python3 .claude/skills/sdr-pipeline/scripts/suppression.py load --file data/outreach/suppression_seed.csv`
   (or the console's Do-not-contact panel on the Use view).

## Run (autonomous)
1. **Ingest:** upload a CSV audience in the console's Use view (gated: contacts wait at the
   segment gate), or `python3 .claude/skills/sdr-pipeline/scripts/csv_audience.py ingest …`.
   Import flags surface missing LinkedIn URLs, non-US/CA rows, healthcare, and sub-1,000
   headcount; hard suppression-list matches never enter the pipeline.
2. **Generate copy per contact:** for each approved contact, invoke the subagent named by its
   `persona` (Task tool) with the contact fields. Save the agent's JSON to
   `data/outreach/generated/<contact_id>.json`. Persona → agent:
   - `erp-owner` → **sdr-erp-owner**
   - `dba` → **sdr-dba**
   - `data-governance` → **sdr-data-governance**
   - `it-leadership` → **sdr-it-leadership**
   (For large lists, run this fan-out as a `Workflow` to pipeline contacts concurrently.
   Messaging is uniform across personas by client decision; the persona routes reporting.)
3. **Enroll:** `python3 .claude/skills/sdr-pipeline/scripts/enroll.py`
   - **Dry-run first:** add `--dry-run` to print the exact Bison + HeyReach payloads without sending.
   - Bison: `create_lead` with `custom_variables` `subject1-4`/`body1-4` → `attach_leads_to_campaign(BISON_CAMPAIGN_ID)`.
   - HeyReach: `AddLeadsToCampaignV2(HEYREACH_CAMPAIGN_ID)` with `customUserFields {li_connect,li_msg1,li_msg2}`
     (skipped for contacts without a LinkedIn URL — email still sends).
   - **Idempotent** via the pipeline DB; re-runs skip already-enrolled contacts.
   - Refuses to enroll email copy that fails the guardrail linter (`--no-lint` to override), and
     re-checks the suppression list + the Fusion-only rule as a backstop.

## Generated-asset schema (`data/outreach/generated/<contact_id>.json`)
```json
{
  "contact_id": "csv-ab12cd34-1", "persona": "erp-owner", "signal": "role anchor - detected Oracle EBS",
  "email": {"subject1":"…","body1":"…","subject2":"…","body2":"…",
            "subject3":"…","body3":"…","subject4":"…","body4":"…"},
  "linkedin": {"li_connect":"…","li_msg1":"…","li_msg2":"…"}
}
```

## Guardrails (enforced)
- Only ICP buyer-group titles are written/enrolled (`persona_for_title` in `buyer_group.py`).
- The account suppression list + the Fusion-only technographic rule block do-not-contact
  accounts at ingest, the segment gate, and enrollment.
- Email copy must pass `lint_sequence.py` (35-110w, question opener, read-only ask in touch 1,
  RE: subject chain, the VG ban list and claim discipline, breakup step 4, no pricing) —
  `enroll.py` blocks failures.
- All product claims trace to `ai-sdr/knowledge/offer.md`; no fabricated signals/stats.

## Batch mode (scale, parallel, low-token) — `/sdr-batches`
For the full list, use the SQLite-backed batch system instead of hand-dispatching:
- **Slash command:** `/sdr-batches [N|all] [enroll]` — batches contacts (25/batch), dispatches
  `sdr-batch-runner` sub-agents in parallel to generate copy, then dry-runs (or live-runs) enrollment.
  The recipe is encoded in `.claude/commands/sdr-batches.md`, so invoking it costs almost no thinking.
- **Terminal CLI** (`scripts/sdr_batches.py`, state in `data/outreach/pipeline.db`):
  `init` (contacts.jsonl → batches of 25) · `status` · `pending-batches` · `get-batch <id>` ·
  `ingest <id>` (lint + mark) · `enroll [--dry-run]` · `reset-batch <id>`.
- **`sdr-batch-runner`** agent processes one batch of 25 end to end (write → save → `ingest`).
  Statuses per contact: pending → generated → enrolled (or failed, with the lint reason).
- New sub-agents/commands may need a Claude Code session reload before first use.

## Components
- Clients: `scripts/hubspot_client.py`, `scripts/heyreach_client.py`,
  `email-bison/scripts/bison_client.py` (`create_lead`, `attach_leads_to_campaign`).
- Router: `ai-sdr/scripts/buyer_group.py` (`persona_for_title`).
- Suppression: `scripts/suppression.py` (do-not-contact list + the Fusion-only rule).
- Signal intelligence: `scripts/tech_signals.py` (ERP technographics), `scripts/news_signals.py`
  (the five ROAD triggers), `scripts/hiring_signals.py`.
- Agents: `.claude/agents/sdr-*.md` (one per persona).

## Notes / current state
- **Bison instance:** `send.everworker.ai` (EverWorker's sending infrastructure; token in `.env`).
- HubSpot is dormant for this engagement (the client has no CRM); the CSV audience path is primary.
- LinkedIn (HeyReach) is deferred — email-only until the client's sender profiles are activated
  with "archiving" positioning.
- The suppression list in `data/outreach/suppression_seed.csv` is the client's DRAFT — reload
  the signed-off version before the first send.
