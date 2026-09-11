"""Shared ICP buyer-group classifier — Value Global ERP Data Retirement.

We sell ERP data archiving / application retirement to the people who OWN the
aging Oracle estate, at $500M+ / 1,000+ employee US-CA enterprises. Per the
client's intake brief, this deal is won at director/manager level, not the
C-suite: the primary targets are the ERP/application owner and the database
owner (titles rarely say so plainly), with data-governance/records owners and
IT leadership (CIO, VP/Director IT) as the surrounding buying group.

Explicitly NOT-ICP: CEOs/founders, procurement/purchasing, every GTM/sales/
marketing title (the template's old ICP is this client's exclusion list), HR,
legal, and finance titles that carry no IT/ERP ownership.

`buyer_role(title)` -> (role, is_icp). `is_icp_buyer(title)` -> bool.
`persona_for_title(title)` -> persona id or None (skip).

Messaging is UNIFORM across personas by client decision (2026-09 kick-off) —
personas exist for ICP gating and reporting, not copy variants.
"""

import re

NOT_ICP = "NOT-ICP"

# Function signals, checked most-specific first.
# ERP / application ownership — the primary target. Named-platform titles
# ("Oracle Apps DBA") are caught by _DBA first when they are database roles.
_ERP_OWNER = re.compile(
    r"\berp\b|e-?business suite|\bebs\b|peoplesoft|jd ?edwards|\bjde\b|"
    r"oracle (apps|applications|financials|r12)|"
    r"\b(business|enterprise) (systems|applications)\b|"
    r"\bapplications? (manager|director|owner|lead|architect|analyst)\b|"
    r"\bit applications\b", re.I)
# Database ownership — the other primary target.
_DBA = re.compile(
    r"\bdba\b|\bdatabase\b|oracle database|data(base)? platform", re.I)
# Data governance / records / archiving — owns retention and compliance.
_DATA_GOV = re.compile(
    r"data governance|information governance|information management|"
    r"records (management|retention|manager)|\bretention\b|\barchiv\w*|"
    r"data (management|lifecycle|quality|steward)|master data|"
    r"\bcdo\b|chief data officer", re.I)
# IT leadership — CIO / VP-Director IT / infrastructure / technology ops.
_IT_LEAD = re.compile(
    r"\bcio\b|chief information officer|\bcto\b|chief technology officer|"
    r"\bit\b|information technology|information systems|information services|"
    r"\binfrastructure\b|technology (operations|services)|\btech ops\b", re.I)
# Seniority: leadership vs the rest (used to gate the broad IT bucket).
_LEADERSHIP = re.compile(
    r"\bchief\b|\bcio\b|\bcto\b|\bvp\b|\bevp\b|\bsvp\b|vice president|\bhead\b|"
    r"\bdirector\b|\bdir\b|\bmanager\b|\bmgr\b|\blead\b|\barchitect\b", re.I)

# Exclusions (the old template ICP is this client's NOT-ICP).
_GTM = re.compile(
    r"\bsales\b|\bmarketing\b|\bcmo\b|\bcro\b|\bgtm\b|go[\s-]?to[\s-]?market|"
    r"business development|\bbiz dev\b|\bsdr\b|\bbdr\b|revenue|demand gen|"
    r"\baccount (executive|manager)\b|customer success|\bgrowth\b|partnerships?\b|"
    r"\balliances?\b|\bchannel\b", re.I)
_EXCLUDED = re.compile(
    r"\bceo\b|chief executive|\bfounder\b|co-?founder|\bprocurement\b|"
    r"\bpurchasing\b|\bsourcing\b|\bhr\b|human resources|\blegal\b|"
    r"\bcounsel\b|\brecruit", re.I)
_FINANCE = re.compile(r"\bcfo\b|\bfinance\b|financial|\bcontroller\b|\baccounting\b", re.I)

# Operator-editable keyword layer (Orchestration view). Additive only: custom
# excludes join the hard exclusions, custom includes are checked AFTER every
# built-in bucket fails, so the code's precedence never changes. Loaded fresh
# per classification call (cheap: one small JSON read) so a saved edit applies
# to the next ingest without a restart; any problem = no overrides.
_PERSONA_ROLE = {
    "erp-owner": "ERP/application owner",
    "dba": "Database owner",
    "data-governance": "Data governance",
    "it-leadership": "IT leadership",
}


def _custom_rules():
    try:
        import instructions
        return instructions.buyer_group_overrides()
    except Exception:  # noqa: BLE001 — overrides must never break classification
        return {"include": {}, "exclude": []}


def buyer_role(title):
    t = " ".join((title or "").lower().split())
    if not t:
        return (NOT_ICP, False)
    custom = _custom_rules()

    # 0. Operator-added exclusions (checked with the hard exclusions).
    if any(kw.lower() in t for kw in custom["exclude"]):
        return ("Excluded (custom rule)", False)

    # 1. Hard exclusions first: CEO/founder/procurement/HR/legal, and every
    #    GTM title unless it also carries an IT/ERP/data function (e.g. a
    #    "Director, Sales Systems / ERP" edge case routes by function below).
    if _EXCLUDED.search(t):
        return ("Excluded (CEO/procurement/HR)", False)
    has_it_function = bool(_ERP_OWNER.search(t) or _DBA.search(t)
                           or _DATA_GOV.search(t) or _IT_LEAD.search(t))
    if _GTM.search(t) and not has_it_function:
        return ("GTM (not this buyer group)", False)

    # 2. ERP / application owner — the primary target.
    if _ERP_OWNER.search(t):
        return ("ERP/application owner", True)

    # 3. Database owner.
    if _DBA.search(t):
        return ("Database owner", True)

    # 4. Data governance / records / archiving.
    if _DATA_GOV.search(t):
        return ("Data governance", True)

    # 5. Finance without IT/ERP terms: out (the CFO hears about it from IT).
    if _FINANCE.search(t):
        return ("Finance (no IT/ERP scope)", False)

    # 6. Broad IT — ICP at leadership level (CIO/VP/Director/Manager IT);
    #    an unqualified IC "IT support" title is not the buying group.
    if _IT_LEAD.search(t):
        if _LEADERSHIP.search(t):
            return ("IT leadership", True)
        return ("IT (non-leadership)", False)

    # 7. Operator-added include keywords — only after every built-in bucket
    #    failed, so custom rules broaden the net without changing precedence.
    for pid in ("erp-owner", "dba", "data-governance", "it-leadership"):
        if any(kw.lower() in t for kw in custom["include"].get(pid, [])):
            return (_PERSONA_ROLE[pid], True)

    return (NOT_ICP, False)


def is_icp_buyer(title):
    return buyer_role(title)[1]


# Role -> outreach persona. Messaging is uniform (client decision); personas
# gate ICP entry and drive reporting only. None = skip (no persona).
_PERSONA_BY_ROLE = {
    "ERP/application owner": "erp-owner",
    "Database owner": "dba",
    "Data governance": "data-governance",
    "IT leadership": "it-leadership",
    "IT (non-leadership)": None,
    "Finance (no IT/ERP scope)": None,
    "GTM (not this buyer group)": None,
    "Excluded (CEO/procurement/HR)": None,
    NOT_ICP: None,
}


def persona_for_title(title):
    """Return the outreach persona for a title, or None to skip this contact."""
    return _PERSONA_BY_ROLE.get(buyer_role(title)[0])


def self_test():
    """Offline classifier checks against the titles this client actually buys
    from (and the ones it must never touch)."""
    icp = {
        "CIO": "it-leadership",
        "Chief Information Officer": "it-leadership",
        "VP Information Technology": "it-leadership",
        "IT Director": "it-leadership",
        "Director of Infrastructure": "it-leadership",
        "ERP Program Manager": "erp-owner",
        "Oracle EBS Applications Manager": "erp-owner",
        "Director, Business Systems": "erp-owner",
        "Manager JD Edwards": "erp-owner",
        "PeopleSoft Administrator": "erp-owner",
        "Enterprise Applications Architect": "erp-owner",
        "Oracle Apps DBA": "erp-owner",   # ERP terms win over the DBA bucket
        "Senior Database Administrator": "dba",
        "Database Manager": "dba",
        "Director of Data Governance": "data-governance",
        "Records Retention Manager": "data-governance",
        "Chief Data Officer": "data-governance",
        "Head of Data Management": "data-governance",
    }
    not_icp = ["CEO", "Founder & CEO", "VP of Sales", "Chief Revenue Officer",
               "Procurement Manager", "CMO", "SDR Manager", "Account Executive",
               "Head of Partnerships", "CFO", "Controller", "HR Director",
               "IT Support Technician", "Recruiter", ""]
    for title, want in icp.items():
        role, ok = buyer_role(title)
        got = persona_for_title(title)
        assert ok and got == want, f"{title!r}: role={role!r} persona={got!r}, want {want!r}"
    for title in not_icp:
        role, ok = buyer_role(title)
        assert not ok and persona_for_title(title) is None, f"{title!r}: role={role!r} should be NOT-ICP"
    print("buyer_group self-test: OK")
    return 0


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    for line in sys.stdin:
        title = line.rstrip("\n")
        role, icp = buyer_role(title)
        persona = persona_for_title(title) or "-"
        print(f"{'ICP ' if icp else '    '} {role:28s} persona={persona:16s} | {title}")
