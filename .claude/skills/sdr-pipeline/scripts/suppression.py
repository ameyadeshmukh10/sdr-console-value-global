"""Account-level suppression for the SDR pipeline (Value Global).

Two suppression sources, both enforced at CSV ingest, the segment gate, and
enrollment (defense in depth — the everworker_tag HubSpot rule in
unenrollment_check.py only covers digit-id HubSpot contacts, which this
pipeline has none of):

1. The client's account suppression list (suppression_accounts table). VG's
   list is account NAMES only — no domains — so matching is name-based:
   normalized-exact, domain-exact, and rule-name-contained-in-company are HARD
   matches (blocked); company-contained-in-rule and close fuzzy matches are
   SOFT (surfaced for human review at the segment gate, never auto-blocked —
   "Target" must not hard-match a "Target Hospitality" rule).

2. The technographic Fusion rule: an account whose ERP scan found Oracle
   Fusion and NO on-prem suite (EBS / PeopleSoft / JD Edwards) is already on
   a cloud ERP that ROAD cannot archive out of — not a prospect. Fusion
   co-detected WITH an on-prem suite is kept: that is a mid-migration estate,
   exactly the erp_migration trigger's sweet spot.

CLI:
  python3 suppression.py load --file names.csv [--replace] [--by who]
  python3 suppression.py list
  python3 suppression.py check --name "Acme Corp" [--domain acme.com]
  python3 suppression.py remove --id 3
  python3 suppression.py --self-test        # offline: matcher + Fusion rule
"""

import argparse
import csv
import difflib
import io
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import batch_db as db  # noqa: E402

FUZZY_THRESHOLD = 0.90

# Trailing corporate suffixes dropped during normalization ("TPC Group" and
# "TPC" must land on the same key). Deliberately short — distinguishing words
# like "Industries" or "Energy" stay.
_SUFFIXES = {"inc", "incorporated", "llc", "llp", "lp", "plc", "ltd", "limited",
             "co", "corp", "corporation", "company", "companies", "holdings",
             "holding", "group"}


def normalize_name(s):
    """'Expand Energy (SWN), Inc.' -> 'expand energy'. Lowercase, drop
    parentheticals and punctuation, strip trailing corporate suffixes."""
    s = re.sub(r"\([^)]*\)", " ", (s or "").lower())
    tokens = re.sub(r"[^a-z0-9]+", " ", s).split()
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _contains_tokens(inner, outer):
    """True when `inner`'s tokens appear as a contiguous run inside `outer`'s
    (whole-word: 'genesis' is in 'genesis energy', not in 'regenesis')."""
    it, ot = inner.split(), outer.split()
    if not it or len(it) > len(ot):
        return False
    return any(ot[i:i + len(it)] == it for i in range(len(ot) - len(it) + 1))


def load_rules(conn):
    """Active suppression rules as dicts (id, name, name_norm, domain, reason)."""
    return [dict(r) for r in conn.execute(
        "SELECT id, name, name_norm, domain, reason FROM suppression_accounts "
        "WHERE active=1 ORDER BY name_norm")]


def match(company, domain, rules):
    """Match one account against the rules. Returns (rule, kind) where kind is
    'exact' | 'domain' | 'containment' (hard — block) or 'fuzzy' | 'reverse'
    (soft — flag for human review), or (None, '') for no match."""
    norm = normalize_name(company)
    dom = (domain or "").strip().lower()
    if not norm and not dom:
        return None, ""
    soft = None
    for r in rules:
        rn = r["name_norm"]
        if dom and r.get("domain") and dom == r["domain"].strip().lower():
            return r, "domain"
        if not norm or not rn:
            continue
        if norm == rn:
            return r, "exact"
        if _contains_tokens(rn, norm):          # rule name inside company name
            return r, "containment"
        if soft is None and _contains_tokens(norm, rn):  # company inside rule
            soft = (r, "reverse")
        if soft is None and difflib.SequenceMatcher(None, norm, rn).ratio() >= FUZZY_THRESHOLD:
            soft = (r, "fuzzy")
    return soft if soft else (None, "")


def match_map(conn, pairs):
    """Bulk helper: [(company, domain)] -> list of (rule, kind) per pair,
    loading the rules once."""
    rules = load_rules(conn)
    return [match(c, d, rules) for c, d in pairs]


# ---- technographic Fusion rule ----------------------------------------------
_ONPREM = {"oracle_ebs", "peoplesoft", "jd_edwards"}
_FUSION = "oracle_fusion_cloud_erp"


def fusion_only_detected(row):
    """Did the tech scan detect Oracle Fusion with NO on-prem ERP suite?
    True  -> pure-Fusion estate: suppress (ROAD cannot archive out of Fusion).
    False -> completed scan, not Fusion-only (incl. Fusion + EBS mid-migration).
    None  -> no tech scan yet (or the signal row is missing) — undecided."""
    if not row or not row.get("tech_checked_at"):
        return None
    try:
        detections = (json.loads(row.get("tech_detail") or "{}") or {}).get("detections") or []
    except (ValueError, TypeError):
        detections = []
    vendors = {(d or {}).get("vendor_id") for d in detections}
    if _FUSION in vendors:
        return not (vendors & _ONPREM)
    # legacy rows with no parseable detail: fall back to the formatted line
    if not detections:
        line = (row.get("tech_signals") or "").lower()
        if "fusion" in line:
            return not any(k in line for k in ("e-business", "peoplesoft", "jd edwards"))
    return False


# ---- loading -----------------------------------------------------------------
def parse_rules_csv(text):
    """Suppression CSV: 'name[,domain[,reason]]' with or without a header row."""
    rows = []
    for raw in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if not raw or not (raw[0] or "").strip():
            continue
        name = raw[0].strip()
        if name.lower() in ("name", "account name", "company", "company name"):
            continue
        rows.append({"name": name,
                     "domain": (raw[1].strip().lower() if len(raw) > 1 else ""),
                     "reason": (raw[2].strip() if len(raw) > 2 else "")})
    return rows


def load_rules_rows(rows, replace=False, source=None, by=None):
    """Insert parsed rules (dedup by normalized name). Returns (added, total).
    Opens its own read-write connection; retry-safe."""
    def _run():
        conn = db.connect()
        try:
            db.init_schema(conn)
            if replace:
                conn.execute("UPDATE suppression_accounts SET active=0 WHERE active=1")
            existing = {r["name_norm"] for r in load_rules(conn)}
            added = 0
            for r in rows:
                norm = normalize_name(r["name"])
                if not norm or norm in existing:
                    continue
                conn.execute(
                    "INSERT INTO suppression_accounts (name, name_norm, domain, reason, "
                    "source, added_by, added_at, active) VALUES (?,?,?,?,?,?,?,1)",
                    (r["name"], norm, r["domain"] or None, r["reason"] or None,
                     source, by, db.now()))
                existing.add(norm)
                added += 1
            conn.commit()
            return added, len(load_rules(conn))
        finally:
            conn.close()

    return db.retry_locked(_run)


def deactivate_rule(rule_id):
    """Soft-delete one rule. Returns rows changed."""
    def _run():
        conn = db.connect()
        try:
            db.init_schema(conn)
            cur = conn.execute("UPDATE suppression_accounts SET active=0 WHERE id=? AND active=1",
                               (rule_id,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    return db.retry_locked(_run)


# ---- CLI ---------------------------------------------------------------------
def cmd_load(args):
    rows = parse_rules_csv(Path(args.file).read_text(encoding="utf-8-sig", errors="replace"))
    if not rows:
        print(json.dumps({"ok": False, "error": "no account names found in the file"}))
        return 1
    added, total = load_rules_rows(rows, replace=args.replace, source=args.file, by=args.by)
    print(json.dumps({"ok": True, "added": added, "active_rules": total,
                      "rows_in_file": len(rows)}))
    return 0


def cmd_list(args):
    conn = db.connect()
    try:
        db.init_schema(conn)
        rules = load_rules(conn)
    finally:
        conn.close()
    print(json.dumps({"ok": True, "active_rules": len(rules), "rules": rules}))
    return 0


def cmd_check(args):
    conn = db.connect()
    try:
        db.init_schema(conn)
        rule, kind = match(args.name, args.domain, load_rules(conn))
    finally:
        conn.close()
    print(json.dumps({"ok": True, "matched": bool(rule), "kind": kind,
                      "rule": rule, "hard": kind in ("exact", "domain", "containment")}))
    return 0


def cmd_remove(args):
    n = deactivate_rule(args.id)
    print(json.dumps({"ok": bool(n), "removed": n}))
    return 0 if n else 1


def self_test():
    """Offline matcher + Fusion-rule checks. No DB, no network."""
    # normalization
    assert normalize_name("Expand Energy (SWN), Inc.") == "expand energy"
    assert normalize_name("TPC Group") == "tpc" == normalize_name("TPC")
    assert normalize_name("Coterra Oil and Gas") == "coterra oil and gas"
    assert normalize_name("Darling Industries") == "darling industries"  # keep 'Industries'
    # the real VG list must all survive normalization non-empty
    vg = ["Coterra Oil and Gas", "CVR Energy", "Enervest", "Flowserve",
          "Magnolia Oil and Gas", "Puffer Sweiven", "Robertet", "Genesis", "TPC",
          "Aera Energy", "Darling Industries", "Merichem", "Emerson",
          "Mueller Balenger", "Boardwalk Pipeline", "Expand Energy (SWN)",
          "Overhead Door", "Smith-Blair", "Cantex", "Ennis", "Diamond Offshore",
          "Patmate", "AZZ", "Core Laboratories", "Hercules Offshore", "ST Genetics",
          "SRS", "JC Penny", "Hudson Advisors", "Anchor Hocking", "McGraw Hill",
          "Waste Management", "Archrock", "Invesco", "Live Nation",
          "Target Hospitality", "Wesco"]
    rules = [{"id": i, "name": n, "name_norm": normalize_name(n), "domain": None}
             for i, n in enumerate(vg)]
    assert all(r["name_norm"] for r in rules)
    # exact + suffix-stripped exact
    assert match("Waste Management Inc", "", rules)[1] == "exact"
    assert match("waste management", "wm.com", rules)[1] == "exact"
    # rule-in-company containment is hard
    r, kind = match("Genesis Energy LP", "", rules)
    assert kind == "containment" and r["name"] == "Genesis", (r, kind)
    assert match("Emerson Electric", "", rules)[1] == "containment"
    # company-in-rule is SOFT — "Target" must not hard-block on "Target Hospitality"
    r, kind = match("Target", "", rules)
    assert kind == "reverse" and r["name"] == "Target Hospitality", (r, kind)
    # fuzzy is soft
    r, kind = match("JC Penney", "", rules)
    assert kind in ("fuzzy", "containment") and r["name"] == "JC Penny", (r, kind)
    # clean companies don't match
    assert match("Meridian Foods", "meridianfoods.com", rules) == (None, "")
    assert match("Regenesis Bioremediation", "", rules)[1] in ("", "fuzzy", "reverse")
    # domain match when a rule carries one
    rules2 = rules + [{"id": 99, "name": "Acme", "name_norm": "acme", "domain": "acme.com"}]
    assert match("Totally Different Name", "acme.com", rules2)[1] == "domain"
    # Fusion rule (fixture shapes match news_signals._FIXTURE_TECH_*)
    fus = {"tech_checked_at": "2026-09-01T00:00:00Z",
           "tech_detail": json.dumps({"detections": [{"vendor_id": "oracle_fusion_cloud_erp"}]})}
    mig = {"tech_checked_at": "2026-09-01T00:00:00Z",
           "tech_detail": json.dumps({"detections": [{"vendor_id": "oracle_fusion_cloud_erp"},
                                                     {"vendor_id": "oracle_ebs"}]})}
    ebs = {"tech_checked_at": "2026-09-01T00:00:00Z",
           "tech_detail": json.dumps({"detections": [{"vendor_id": "oracle_ebs"}]})}
    assert fusion_only_detected(fus) is True
    assert fusion_only_detected(mig) is False
    assert fusion_only_detected(ebs) is False
    assert fusion_only_detected({}) is None and fusion_only_detected(None) is None
    assert fusion_only_detected({"tech_checked_at": "x", "tech_detail": "",
                                 "tech_signals": "ERP: Oracle Fusion Cloud ERP"}) is True
    assert fusion_only_detected({"tech_checked_at": "x", "tech_detail": "",
                                 "tech_signals": "No signals detected"}) is False
    # CSV parse: header + name-only + name,domain,reason
    rows = parse_rules_csv("Account name\nWaste Management\nAcme,acme.com,client\n")
    assert rows == [{"name": "Waste Management", "domain": "", "reason": ""},
                    {"name": "Acme", "domain": "acme.com", "reason": "client"}]
    print("suppression self-test: OK")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Account suppression list for the SDR pipeline")
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("load")
    p.add_argument("--file", required=True)
    p.add_argument("--replace", action="store_true",
                   help="deactivate all existing rules first (full reload)")
    p.add_argument("--by", default=None)
    p.set_defaults(func=cmd_load)
    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("check")
    p.add_argument("--name", required=True)
    p.add_argument("--domain", default="")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("remove")
    p.add_argument("--id", type=int, required=True)
    p.set_defaults(func=cmd_remove)
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
