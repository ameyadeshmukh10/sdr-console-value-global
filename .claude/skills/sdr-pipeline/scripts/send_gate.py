#!/usr/bin/env python3
"""Pre-send gates from Value Global's signal-messaging feedback (2026-10-09).

Stdlib-only (the server imports it at the segment gate; the boot rule applies).

- **Send floor (issue 10):** a found trigger verdict below the floor never
  drives trigger-anchored copy. `send_floor(trigger_id)` reads
  `TRIGGER_SEND_FLOOR_<ID>` → `TRIGGER_SEND_FLOOR` → the per-trigger default.
  ebs_performance defaults lower because its bands are deliberately low
  (size+industry proxies 30-50, capped at 75) and the client wants that track kept.
- **Name check (issue 9):** the greeting name must agree with the email local
  part or the LinkedIn profile ("Cspa" vs stewart.bobbitt@ was a credential in
  the first-name field). `name_check()` returns ok / a safe repair / a hold.

    python3 send_gate.py --self-test     # offline
"""

from __future__ import annotations

import os
import re
import sys
from typing import TypedDict

DEFAULT_SEND_FLOOR: int = 60
_FLOOR_DEFAULTS: dict[str, int] = {"ebs_performance": 45}


class NameCheck(TypedDict):
    """name_check() result: whether to send, the name to greet with, whether
    it differs from the CRM first name, and why (empty when nothing to say)."""
    ok: bool
    name: str
    fixed: bool
    reason: str


def send_floor(trigger_id: str | None) -> int:
    """The minimum verdict score that may drive trigger copy for this trigger."""
    for key in (f"TRIGGER_SEND_FLOOR_{(trigger_id or '').upper()}", "TRIGGER_SEND_FLOOR"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            try:
                return max(0, min(100, int(raw)))
            except ValueError:
                pass
    return _FLOOR_DEFAULTS.get(trigger_id or "", DEFAULT_SEND_FLOOR)


def below_floor(trigger_id: str | None, score: int | float | str | None) -> bool:
    """True when a found verdict's score is under the trigger's send floor."""
    try:
        return int(score or 0) < send_floor(trigger_id)
    except (TypeError, ValueError):
        return True


# ---- name check ----------------------------------------------------------------
# No word lists: the email's owner is the person reading the email, so when the
# email (first.last, last token = the last name) or the LinkedIn slug names the
# person, that name is the evidence. The CRM first name is kept when it agrees
# (same token, a prefix either way, or the same initial — Mike / michael.,
# Rob / robert.); otherwise the greeting switches to the evidence name and the
# contact is marked for review. Worst case is formal ("Robert" for a "Bob"),
# never the wrong person.


def _tokens(s: str | None) -> list[str]:
    return [t for t in re.split(r"[^a-z]+", (s or "").lower()) if len(t) >= 2]


def _linkedin_tokens(url: str | None) -> list[str]:
    m = re.search(r"/in/([^/?#]+)", url or "", re.I)
    if not m:
        return []
    # slugs often end in a disambiguator ("stewart-bobbitt-4a1b2c3")
    return [t for t in re.split(r"[^a-z0-9]+", m.group(1).lower())
            if len(t) >= 2 and not re.search(r"\d", t)]


def _same_name(a: str, b: str) -> bool:
    """Same token, or one is a prefix of the other (3+ letters)."""
    return bool(a and b) and (a == b or (min(len(a), len(b)) >= 3
                                         and (a.startswith(b) or b.startswith(a))))


def _evidence_name(last: str, local: str, ltoks: list[str]) -> str:
    """The first name the contact's own identifiers state, or ''."""
    etoks = _tokens(local)
    if re.search(r"[._-]", local) and len(etoks) >= 2 and \
            (not last or _same_name(last, etoks[-1]) or last in etoks):
        return etoks[0]
    if len(ltoks) >= 2 and (not last or _same_name(last, ltoks[-1]) or last in ltoks):
        return ltoks[0]
    return ""


def name_check(first_name: str | None, last_name: str | None = "", email: str | None = "",
               linkedin_url: str | None = "") -> NameCheck:
    """Does the greeting name fit the contact's own identifiers?

    Returns {"ok", "name", "fixed", "reason"}:
    - ok, not fixed: greet with the CRM first name (it agrees, or there is no
      evidence either way);
    - ok, fixed: greet with the name the email / LinkedIn states (the CRM name
      disagrees or is garbage) — the caller records the change for review;
    - not ok: the name is empty / garbage and nothing states the real one — hold."""
    raw = (first_name or "").strip()
    first = re.sub(r"[^a-z]", "", raw.lower())
    last = re.sub(r"[^a-z]", "", (last_name or "").lower())
    local = (email or "").split("@")[0].lower()
    evid = _evidence_name(last, local, _linkedin_tokens(linkedin_url))
    garbage = len(first) < 2 or bool(re.search(r"[\d@]", raw))

    if not garbage:
        if not evid or _same_name(first, evid) or first[0] == evid[0]:
            return {"ok": True, "name": raw, "fixed": False, "reason": ""}
    if evid:
        fixed = evid.capitalize()
        src = "the email" if evid in _tokens(local) else "the LinkedIn profile"
        return {"ok": True, "name": fixed, "fixed": True,
                "reason": f"first name {raw!r} disagrees with {src}; greeting {fixed!r}"}
    return {"ok": False, "name": raw, "fixed": False,
            "reason": f"first name {raw!r} is unusable and nothing states the real one; "
                      "fix the contact before sending"}


def self_test() -> int:
    cases: list[tuple[tuple[str, str, str, str], tuple[bool, str, bool]]] = [
        # (first, last, email, linkedin) -> (ok, name, fixed)
        (("Cspa", "Bobbitt", "stewart.bobbitt@horacemann.com", ""), (True, "Stewart", True)),
        (("Stewart", "Bobbitt", "stewart.bobbitt@horacemann.com", ""), (True, "Stewart", False)),
        (("Mike", "Jones", "michael.jones@x.com", ""), (True, "Mike", False)),
        (("Rob", "Smith", "robert_smith@x.com", ""), (True, "Rob", False)),
        (("Bob", "Smith", "robert.smith@x.com", ""), (True, "Robert", True)),
        (("JD", "Smith", "jd.smith@x.com", ""), (True, "JD", False)),
        (("Dana", "Lee", "maria.lee@x.com", ""), (True, "Maria", True)),
        (("Dana", "Lee", "maria.garcia@x.com", ""), (True, "Dana", False)),  # not her mailbox shape
        (("Dana", "Lee", "dlee@x.com", ""), (True, "Dana", False)),
        (("Dana", "Lee", "info@x.com", ""), (True, "Dana", False)),
        (("JOHN", "Doe", "john.doe@x.com", ""), (True, "JOHN", False)),
        (("Cspa", "Bobbitt", "sbobbitt@x.com",
          "https://www.linkedin.com/in/stewart-bobbitt-4a1b2c3"), (True, "Stewart", True)),
        (("", "Bobbitt", "stewart.bobbitt@x.com", ""), (True, "Stewart", True)),
        (("", "Lee", "info@x.com", ""), (False, "", False)),
        (("J0hn", "Doe", "jd@x.com", ""), (False, "J0hn", False)),
    ]
    failed: list[object] = []
    for args, (ok, name, fixed) in cases:
        r = name_check(*args)
        got = (r["ok"], r["name"], r["fixed"])
        mark = "PASS" if got == (ok, name, fixed) else "FAIL"
        if mark == "FAIL":
            failed.append(args)
        print(f"[send_gate] {mark} name_check{args[:3]} -> {got} {r['reason']}")
    os.environ.pop("TRIGGER_SEND_FLOOR", None)
    floors = (send_floor("ma_carveout"), send_floor("ebs_performance"))
    os.environ["TRIGGER_SEND_FLOOR_MA_CARVEOUT"] = "70"
    over = send_floor("ma_carveout")
    os.environ.pop("TRIGGER_SEND_FLOOR_MA_CARVEOUT")
    ok_floor = floors == (60, 45) and over == 70 and below_floor("ma_carveout", 52) \
        and not below_floor("ma_carveout", 60)
    print(f"[send_gate] {'PASS' if ok_floor else 'FAIL'} send floor defaults/override "
          f"{floors} override={over}")
    if not ok_floor:
        failed.append("floor")
    print({"ok": not failed, "failed": [str(f) for f in failed]})
    return 0 if not failed else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    print(__doc__)
