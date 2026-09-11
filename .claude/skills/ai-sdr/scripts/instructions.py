"""Editable agent instructions — the volume-backed override layer.

The Orchestration view lets a non-technical operator edit how the AI SDR
thinks: the ICP filter's custom keywords, per-persona framing, the five ERP
trigger plays, and the knowledge-base markdown. Those edits live as files
under `data/outreach/instructions/` (the Railway Volume in prod), so they
survive redeploys and never touch the repo sources.

Contract (load-bearing):
  - Every reader here NEVER raises and falls back to "no override" on any
    problem (missing dir, malformed JSON, wrong types) — the pipeline must
    behave exactly as the committed defaults whenever this layer is absent
    or broken.
  - Writers are used ONLY by the web server (atomic replace under its lock);
    pipeline scripts never write here.
  - The guardrail linter is deliberately NOT editable: every generated email
    still passes `lint_sequence.py` regardless of what is edited here.

Layout:
  instructions/
    knowledge/offer.md        full-document override of the knowledge file
    knowledge/cta-offers.md
    knowledge/icp-email.md
    personas.json             {persona_id: {"pain": str, "outcome": str}}
    erp_plays.json            {segment: {"problem","solution","opener"}}
    buyer_group.json          {"include": {persona: [keyword]}, "exclude": [keyword]}
    meta.json                 {item_key: {"updated_at","updated_by"}}

Stdlib only — importable by buyer_group.py (which must stay dependency-free).
"""

import json
import os
import re
import tempfile
from pathlib import Path

# .claude/skills/ai-sdr/scripts/instructions.py -> project root is parents[4]
_PROJECT_ROOT = Path(__file__).resolve().parents[4]

KNOWLEDGE_FILES = ("offer.md", "cta-offers.md", "icp-email.md")
PERSONA_IDS = ("erp-owner", "dba", "data-governance", "it-leadership")
PLAY_SEGMENTS = ("ma_carveout", "erp_migration", "license_audit", "ebs_oci", "ebs_performance")
PERSONA_FIELDS = ("pain", "outcome")
PLAY_FIELDS = ("problem", "solution", "opener")

MAX_FIELD_CHARS = 2500       # persona/play text fields
MAX_KNOWLEDGE_CHARS = 64000  # a full knowledge markdown document
MAX_KEYWORDS = 40            # per include/exclude list
KEYWORD_RE = re.compile(r"^[\w&/+.'’-]+(?: [\w&/+.'’-]+){0,5}$")  # 1-6 plain words


def instructions_dir():
    override = (os.environ.get("INSTRUCTIONS_DIR") or "").strip()
    if override:
        return Path(override)
    return _PROJECT_ROOT / "data" / "outreach" / "instructions"


# ---- readers (never raise) ---------------------------------------------------
def load_json(name):
    """Parsed JSON object from <dir>/<name>, or {} on any problem."""
    try:
        p = instructions_dir() / name
        if not p.is_file():
            return {}
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — the override layer must never break callers
        return {}


def knowledge_override(fname):
    """Override text for a knowledge file, or None (missing/empty/invalid)."""
    if fname not in KNOWLEDGE_FILES:
        return None
    try:
        p = instructions_dir() / "knowledge" / fname
        if not p.is_file():
            return None
        text = p.read_text()
        return text if text.strip() else None
    except Exception:  # noqa: BLE001
        return None


def _clean_str(value, cap):
    return value.strip()[:cap] if isinstance(value, str) else ""


def persona_overrides():
    """{persona_id: {pain, outcome}} — only valid, non-empty fields survive."""
    out = {}
    for pid, fields in load_json("personas.json").items():
        if pid not in PERSONA_IDS or not isinstance(fields, dict):
            continue
        clean = {k: _clean_str(fields.get(k), MAX_FIELD_CHARS)
                 for k in PERSONA_FIELDS if _clean_str(fields.get(k), MAX_FIELD_CHARS)}
        if clean:
            out[pid] = clean
    return out


def play_overrides():
    """{segment: {problem, solution, opener}} — only valid fields survive."""
    out = {}
    for seg, fields in load_json("erp_plays.json").items():
        if seg not in PLAY_SEGMENTS or not isinstance(fields, dict):
            continue
        clean = {k: _clean_str(fields.get(k), MAX_FIELD_CHARS)
                 for k in PLAY_FIELDS if _clean_str(fields.get(k), MAX_FIELD_CHARS)}
        if clean:
            out[seg] = clean
    return out


def _clean_keywords(values):
    out = []
    for v in values if isinstance(values, list) else []:
        v = " ".join(str(v).split()).strip()
        if v and len(v) <= 48 and KEYWORD_RE.match(v) and v.lower() not in [k.lower() for k in out]:
            out.append(v)
        if len(out) >= MAX_KEYWORDS:
            break
    return out


def buyer_group_overrides():
    """{"include": {persona: [keyword]}, "exclude": [keyword]} — cleaned."""
    raw = load_json("buyer_group.json")
    inc_raw = raw.get("include")
    include = {}
    for pid, kws in (inc_raw.items() if isinstance(inc_raw, dict) else []):
        if pid in PERSONA_IDS:
            kws = _clean_keywords(kws)
            if kws:
                include[pid] = kws
    return {"include": include, "exclude": _clean_keywords(raw.get("exclude"))}


def meta():
    return load_json("meta.json")


# ---- writers (web server only; atomic) ---------------------------------------
def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(name, data):
    _atomic_write(instructions_dir() / name, json.dumps(data, indent=2, ensure_ascii=False))


def write_knowledge(fname, text):
    if fname not in KNOWLEDGE_FILES:
        raise ValueError(f"unknown knowledge file {fname!r}")
    _atomic_write(instructions_dir() / "knowledge" / fname, text)


def remove_knowledge(fname):
    try:
        (instructions_dir() / "knowledge" / fname).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def stamp(item_key, by=None):
    """Record who/when for one edited item in meta.json (best-effort)."""
    from datetime import datetime, timezone
    try:
        m = meta()
        m[item_key] = {"updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "updated_by": (by or "").strip()[:120]}
        write_json("meta.json", m)
    except Exception:  # noqa: BLE001
        pass


def clear_stamp(item_key):
    try:
        m = meta()
        if item_key in m:
            del m[item_key]
            write_json("meta.json", m)
    except Exception:  # noqa: BLE001
        pass
