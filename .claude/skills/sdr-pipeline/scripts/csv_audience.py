"""Ingest an uploaded CSV of contacts into the pipeline as a named audience.

Stdlib-only (csv + batch_db). The web console's Use view uploads a CSV (first
name, last name, job title, email, LinkedIn URL, country, company name, company
website, industry, employee count — flexible header spellings), the server saves
it under data/outreach/csv-uploads/<audience_id>.csv and shells out here; this
script parses/normalizes the rows, dedups (in-file + against pipeline.db by
email), assigns a persona per job title (unmatched titles default to
sales-leadership — a hand-picked upload is trusted, unlike a HubSpot list pull),
inserts the contacts with synthetic ids `csv-<suffix>-<n>` (NOT HubSpot ids —
every HubSpot write-back skips or best-effort-fails them by design), and batches
them exactly like `sdr_batches.py init`. Downstream — generation, enrollment
into Bison/HeyReach, the Outreach index — treats them like any other contact.

The audience registry (data/outreach/csv_audiences.json) is written by the web
server, which is its single writer; this script only prints the JSON summary the
server records (last stdout line, per the run_script() contract).

CLI:
  python3 csv_audience.py ingest --file contacts.csv --name "Q3 event leads"
                                 [--id aud-xxxxxxxx] [--by who] [--batch-size 25]
                                 [--dry-run]
  python3 csv_audience.py --self-test        # offline: parser + mapping checks
"""

import argparse
import csv
import io
import json
import re
import secrets
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[1] / "ai-sdr" / "scripts"))  # buyer_group
import batch_db as db                            # noqa: E402
from buyer_group import persona_for_title        # noqa: E402

MAX_ROWS = 50_000          # sanity cap — an accidental giant export, not a DoS guard
DEFAULT_PERSONA = "sales-leadership"  # broadest persona; used when a title matches none

# Normalized header -> canonical field. Headers are lowercased and squeezed to
# single spaces over [a-z0-9] before lookup, so "First_Name", "first-name" and
# "First Name" all land on first_name.
HEADER_ALIASES = {
    "first_name": {"first name", "firstname", "first", "given name"},
    "last_name": {"last name", "lastname", "last", "surname", "family name"},
    "title": {"job title", "jobtitle", "title", "position", "role"},
    "email": {"email", "email address", "work email", "e mail", "e mail address",
              "business email"},
    "linkedin_url": {"linkedin profile url", "linkedin url", "linkedin", "linkedin profile",
                     "li url", "person linkedin url"},
    "country": {"country", "country region", "location country"},
    "company": {"company name", "company", "companyname", "account name", "organization",
                "organisation", "employer"},
    "website": {"company website", "website", "company url", "company domain", "domain",
                "website url", "company website url"},
    "industry": {"company industry", "industry"},
    "employees": {"company number of employees", "number of employees", "employees",
                  "employee count", "headcount", "num employees", "company size",
                  "of employees"},
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _norm_header(h):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (h or "").lower()).split())


def map_headers(fieldnames):
    """Map the CSV's header row to canonical fields. Returns {index: field}."""
    lookup = {}
    for field, aliases in HEADER_ALIASES.items():
        for a in aliases:
            lookup[a] = field
    mapped, seen = {}, set()
    for i, h in enumerate(fieldnames or []):
        field = lookup.get(_norm_header(h))
        if field and field not in seen:   # first matching column wins
            mapped[i] = field
            seen.add(field)
    return mapped


def domain_from_website(url):
    """'https://www.Acme.com/about' -> 'acme.com' (best-effort; '' if unusable)."""
    u = (url or "").strip().lower()
    if not u:
        return ""
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u)          # scheme
    u = u.split("/")[0].split("?")[0].split("#")[0]      # path/query
    u = u.split("@")[-1].split(":")[0]                   # creds/port
    if u.startswith("www."):
        u = u[4:]
    return u if ("." in u and " " not in u) else ""


def _norm_linkedin(url):
    u = (url or "").strip()
    if u and "linkedin.com" in u.lower() and not re.match(r"^https?://", u, re.I):
        u = "https://" + u.lstrip("/")
    return u


def parse_csv(text):
    """Parse CSV text into normalized contact dicts. Returns (rows, mapped_fields).
    Handles BOMs and comma/semicolon/tab delimiters. Raises ValueError when no
    usable header (at minimum an email column) is found."""
    text = text.lstrip("﻿")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("the CSV is empty")
    mapped = map_headers(header)
    if "email" not in mapped.values():
        raise ValueError("no email column found — expected a header like "
                         "'Email' or 'Email Address' (got: "
                         + ", ".join(h.strip() for h in header[:12]) + ")")
    rows = []
    for raw in reader:
        if not any((c or "").strip() for c in raw):
            continue
        rec = {f: "" for f in HEADER_ALIASES}
        for i, field in mapped.items():
            if i < len(raw):
                rec[field] = (raw[i] or "").strip()
        rec["email"] = rec["email"].strip()
        rec["linkedin_url"] = _norm_linkedin(rec["linkedin_url"])
        rows.append(rec)
        if len(rows) > MAX_ROWS:
            raise ValueError(f"more than {MAX_ROWS} rows — split the file")
    return rows, sorted(set(mapped.values()))


def build_contacts(rows, id_suffix):
    """Normalize parsed rows into pipeline contact dicts + skip counts.
    Dedup here is in-file only; the DB dedup happens in ingest()."""
    contacts, seen = [], set()
    counts = {"rows": len(rows), "no_email": 0, "duplicate_in_file": 0,
              "persona_defaulted": 0}
    skipped_examples = []
    for rec in rows:
        email = rec["email"]
        if not _EMAIL_RE.match(email):
            counts["no_email"] += 1
            if len(skipped_examples) < 20:
                skipped_examples.append({"email": email or "(blank)", "reason": "no valid email"})
            continue
        key = email.lower()
        if key in seen:
            counts["duplicate_in_file"] += 1
            continue
        seen.add(key)
        persona = persona_for_title(rec["title"])
        if not persona:
            persona = DEFAULT_PERSONA
            counts["persona_defaulted"] += 1
        contacts.append({
            "contact_id": f"csv-{id_suffix}-{len(contacts) + 1}",
            "first_name": rec["first_name"],
            "last_name": rec["last_name"],
            "email": email,
            "title": rec["title"],
            "company": rec["company"],
            "linkedin_url": rec["linkedin_url"],
            "persona": persona,
            # Prefer the company website's domain so signal research/tech/hiring
            # scans hit the company site even for personal-mailbox contacts;
            # upsert_contacts falls back to the email domain when this is ''.
            "domain": domain_from_website(rec["website"]),
        })
    return contacts, counts, skipped_examples


def ingest(csv_path, name, audience_id=None, by=None, batch_size=25, dry_run=False,
           gated=False):
    """Parse + insert + batch. Returns the summary dict (also what the CLI prints)."""
    audience_id = audience_id or f"aud-{secrets.token_hex(4)}"
    suffix = audience_id.split("-", 1)[-1] or secrets.token_hex(4)
    text = Path(csv_path).read_text(encoding="utf-8-sig", errors="replace")
    rows, mapped_fields = parse_csv(text)
    contacts, counts, skipped_examples = build_contacts(rows, suffix)

    # Dedup against contacts already in the pipeline (any source), by email.
    def _existing_emails():
        conn = db.connect()
        try:
            db.init_schema(conn)
            return {r[0].lower() for r in conn.execute(
                "SELECT email FROM contacts WHERE email IS NOT NULL AND email != ''")}
        finally:
            conn.close()

    existing = db.retry_locked(_existing_emails)
    fresh = [c for c in contacts if c["email"].lower() not in existing]
    counts["already_in_pipeline"] = len(contacts) - len(fresh)

    summary = {
        "ok": True, "audience_id": audience_id, "name": name, "by": by,
        "dry_run": bool(dry_run), "gated": bool(gated), "mapped_fields": mapped_fields,
        "counts": counts, "skipped_examples": skipped_examples,
        "added": 0, "new_batches": 0, "batch_ids": [],
        "contact_prefix": f"csv-{suffix}-",
    }
    if dry_run or not fresh:
        summary["added"] = len(fresh) if dry_run else 0
        return summary

    # Insert + batch, same idempotent shape as sdr_batches cmd_init (INSERT OR
    # IGNORE; assign_batches only touches batch_id IS NULL), so a lock loss to
    # the server's background writers safely retries on a fresh connection.
    def _run():
        conn = db.connect()
        try:
            db.init_schema(conn)
            added = db.upsert_contacts(conn, fresh, gated=gated)
            made = db.assign_batches(conn, batch_size)
            bids = sorted({r[0] for r in conn.execute(
                "SELECT DISTINCT batch_id FROM contacts WHERE contact_id LIKE ? "
                "AND batch_id IS NOT NULL", (f"csv-{suffix}-%",))})
            return added, made, bids
        finally:
            conn.close()

    added, made, batch_ids = db.retry_locked(_run)
    summary.update({"added": added, "new_batches": made, "batch_ids": batch_ids})
    return summary


def cmd_ingest(args):
    try:
        summary = ingest(args.file, args.name, audience_id=args.id, by=args.by,
                         batch_size=args.batch_size, dry_run=args.dry_run,
                         gated=args.gated)
    except (ValueError, OSError, csv.Error) as e:
        print(json.dumps({"ok": False, "error": str(e)[:400]}))
        return 1
    c = summary["counts"]
    print(f"csv-audience {summary['audience_id']}{' [dry-run]' if args.dry_run else ''}: "
          f"{c['rows']} rows -> {summary['added']} added, "
          f"{c['already_in_pipeline']} already in pipeline, {c['no_email']} no email, "
          f"{c['duplicate_in_file']} in-file dupes; +{summary['new_batches']} batches",
          file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def self_test():
    """Offline checks: header mapping, delimiter handling, dedup, persona routing,
    domain preference. No DB, no network."""
    sample = (
        "﻿First Name;Last Name;Job Title;Email Address;LinkedIn Profile URL;"
        "Country;Company Name;Company Website;Company Industry;Company Number of Employees\n"
        "Ada;Lovelace;VP of Sales;ada@acme.com;www.linkedin.com/in/ada;US;Acme;"
        "https://www.acme.com/about;Software;250\n"
        "Bob;Jones;Chief Fun Officer;bob@gmail.com;;DE;Globex;globex.de;Manufacturing;90\n"
        "Ada;Dup;VP of Sales;ADA@acme.com;;US;Acme;;Software;250\n"
        "NoMail;Person;CRO;;;US;Initech;;;\n"
    )
    rows, fields = parse_csv(sample)
    assert set(fields) == set(HEADER_ALIASES), f"mapped fields wrong: {fields}"
    assert len(rows) == 4, f"expected 4 rows, got {len(rows)}"
    contacts, counts, skipped = build_contacts(rows, "deadbeef")
    assert len(contacts) == 2, f"expected 2 contacts, got {len(contacts)}"
    assert counts == {"rows": 4, "no_email": 1, "duplicate_in_file": 1,
                      "persona_defaulted": 1}, counts
    ada, bob = contacts
    assert ada["contact_id"] == "csv-deadbeef-1" and ada["persona"] == "sales-leadership"
    assert ada["domain"] == "acme.com", ada["domain"]
    assert ada["linkedin_url"] == "https://www.linkedin.com/in/ada", ada["linkedin_url"]
    assert bob["persona"] == DEFAULT_PERSONA and bob["domain"] == "globex.de", bob
    assert skipped and skipped[0]["reason"] == "no valid email"
    # comma dialect + minimal headers still map
    rows2, fields2 = parse_csv("email,first name\nx@y.co,Xa\n")
    assert fields2 == ["email", "first_name"] and rows2[0]["email"] == "x@y.co"
    assert domain_from_website("http://ERP.Acme.com:8080/x?y=1") == "erp.acme.com"
    assert domain_from_website("not a url") == ""
    try:
        parse_csv("name,phone\nx,1\n")
        raise AssertionError("missing-email header should raise")
    except ValueError:
        pass
    print("csv_audience self-test: OK")
    return 0


def main():
    ap = argparse.ArgumentParser(description="CSV audience ingest for the SDR pipeline")
    ap.add_argument("--self-test", action="store_true", help="offline parser checks")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("ingest")
    p.add_argument("--file", required=True, help="path to the uploaded CSV")
    p.add_argument("--name", required=True, help="audience name (shown in the UI)")
    p.add_argument("--id", default=None, help="audience id (aud-<hex>); generated if omitted")
    p.add_argument("--by", default=None, help="who uploaded (email), for the record")
    p.add_argument("--batch-size", type=int, default=25)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--gated", action="store_true",
                   help="insert into the human-approval flow (segment gate before batching)")
    p.set_defaults(func=cmd_ingest)
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
