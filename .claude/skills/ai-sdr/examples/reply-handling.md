# Reply Handling (worked examples, Value Global ERP Data Retirement)

How to respond to replies. Classify intent, then draft a grounded reply. All claims trace to
`knowledge/offer.md`. Rules that apply to every reply: at most two credibility stats, every
InfoCorvus figure attributed to the vendor, no dollars on the IRR range, no em dashes, no
booking links, and **a human (Value Global) runs discovery**: the AI SDR carries the
conversation to the booked call and no further. Pricing appears only on a direct pricing ask.

---
### 1. "Send it over." / "Sure, share it."
The win condition for the touch-1 ask. Deliver immediately, point at the chart, tee the assessment.
> Here it is, Dan. It is a two-minute read, and the chart on the first page is the whole point. In most long-running EBS systems the dormant history is around 70% of the database, at the same cost as the data you use every day. If that looks like your environment, the natural next step is a free Data Lifecycle Assessment. It puts a real number against your own system rather than a general pattern. Want me to set that up?

---
### 2. Assessment yes — *"OK, what does that involve?"*
Propose the short scoping call and hand off to a human. Two windows in prose, never a link.
> Good to hear, Dan. Scoping takes about twenty minutes: which modules, roughly how much history, and what you want the roadmap to cover. Nirmiti runs these and will walk you through what the report shows. Would Tuesday morning or Thursday afternoon work? If another window is better, name it and we will work around you.

---
### 3. Pricing ask — *"What does this cost?"*
Never quote figures. The assessment produces the number; the call is the next step.
> Fair question. It depends on database size, module scope and the deployment pattern, so a generic figure would mislead you in one direction or the other. The free assessment produces the real number for your environment, and one useful shape up front: a one-time retirement does not require a perpetual software subscription. Worth a short call to scope the assessment?

---
### 4. Objection — *"Our DBA can script this."*
The most common objection. Business context is the answer.
> A DBA can extract rows, and for a one-off table that is the right call. Two things are hard to reproduce with scripts: governed access with retention and legal-hold policies applied consistently, and business-context reporting, where a user searches a supplier and gets the whole picture: orders, receipts, invoices, payments, approvals and audit history. If it only exposed tables, you could build that yourself. It preserves the business context, which is the hard part. Want the short read on how that works?

---
### 5. Objection — *"We already have Snowflake."*
The governance-layer answer.
> Snowflake is a fine warehouse, and this is not one. Flat-file ETL strips the relational context out of an ERP, which is exactly what breaks audit trails and legal hold. The test worth applying: when an auditor asks, can it reconstruct an invoice with all its lines, tax entries, payments and GL accounting entries? A governed archive keeps that context intact. Happy to send the two-minute read on the difference.

---
### 6. *"Does this touch our production system?"*
Answer ONLY when asked, briefly, then return to value. Never volunteer this reassurance anywhere.
> The assessment reads metadata from a non-production system: table and module sizes only, no business data, nothing extracted or changed. What it gives you back is the real picture: your growth profile, archive candidates by module, and a quantified cost-reduction opportunity. Want me to set it up?

---
### 7. Referral — *"Talk to our DBA lead, Priya."*
Thank, ask for the intro, carry the read to the new contact.
> Appreciate it, Dan. Want to intro us, or should I reach out and mention you pointed me her way? Either way I will bring the short POV so Priya has the argument in front of her from the first note.
