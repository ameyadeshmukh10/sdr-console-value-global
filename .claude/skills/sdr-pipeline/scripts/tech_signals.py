"""Technographic detection runner — module + CLI.

Detects the tech a company runs (DNS + static-website fingerprinting + ERP
portal probes via the vendored `technographics/` package at the repo root) and
stores the result on the `account_signals` row for that domain. This console is
configured for Value Global: the default selection (`selection.erp.json`) scopes
detection to the four probe-enabled ERP suites — Oracle E-Business Suite,
Oracle Fusion Cloud ERP, PeopleSoft, JD Edwards — and deliberately drops the
template's marketing/sales coverage (point TECH_SELECTION_FILE at
`selection.marketing_sales.json` to get it back).

    tech_signals    "ERP: Oracle PeopleSoft" (bucketed line; or the literal
                    "No signals detected"; NULL only if the scan itself failed
                    on BOTH channels)
    tech_detail     JSON: structured detections + per-channel errors + timing
    tech_checked_at ISO-8601 Z; scans are reused for TECH_REFRESH_DAYS (90)
    tech_error      set only when the whole scan failed (fetch AND dns dead)

ERP suites never show on the marketing site: they're found by the vendored
probe-then-fetch step (`subdomain_prober.py`) — cheap static GETs against
well-known portal paths on named subdomains (e.g. erp.<domain>/OA_HTML/AppsLogin),
matching only that vendor's signature, with catch-all guards (4xx/5xx dropped;
a match whose only evidence is the URL we constructed is discarded unless the
server organically redirected off-site). TECH_PROBES=0 disables the step;
TECH_PROBE_TIMEOUT (default 4.0s) bounds each probe GET. The probes use a
stdlib urllib fetcher injected from this module (upstream's default fetcher
needs httpx, which prod doesn't install).

Scan JSON (CLI + detect_and_store) also carries a `playbook` field: the
copy-facing classification of detections into ads / intent_abm / sequencing
groups (PLAYBOOK_* sets below) — kept from the template; under the ERP-only
selection every group is empty and generation consumes the line as plain
background context. generate_batch.py reads the same groups from stored
tech_detail; agents reuse the CLI field directly.

After a successful scan the formatted line is also PATCHed to the HubSpot
company property `technographic_signals` (best-effort; TECH_HUBSPOT_WRITEBACK=0
to disable, or no matching company by domain -> recorded, never fatal).

Import contract: this module is stdlib-only at import time. dnspython and the
vendored package are imported lazily inside functions, so webui/server/app.py
(which imports this in-process, like generate_batch) still boots with zero pip
deps — `tech_available()` reports why detection is off instead of crashing.

CLI (run_script conventions: progress lines on stderr, JSON summary as the
LAST stdout line):

    python3 tech_signals.py --domain acme.com [--company "Acme"] [--force]
                            [--rendered] [--no-hubspot]
    python3 tech_signals.py --missing [--stale-days N] [--limit N]
                            [--workers N] [--force] [--no-hubspot]
    python3 tech_signals.py --self-test        # offline; no network/dnspython

--rendered uses the vendored Playwright collector (Claude sessions have
Chromium preinstalled); the Railway image deliberately does not, so prod scans
are always DNS + static HTML.

The bucket-mapping / confidence / formatting logic is ported from the
technographic-signals repo's `src/detectors/category_map.py` and
`src/orchestrator.py`; the probe orchestration mirrors its
`src/detectors/engine.py` (see technographics/VENDORED.md for provenance).
"""

import argparse
import asyncio
import gzip
import http.cookiejar
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections import defaultdict, namedtuple
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

import batch_db as db
from hubspot_client import _load_dotenv

_load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[4]  # scripts/ -> sdr-pipeline -> skills -> .claude -> project
TECH_SRC = PROJECT_ROOT / "technographics" / "src"
SIGNATURES_DIR = PROJECT_ROOT / "technographics" / "signatures"
DEFAULT_SELECTION = SIGNATURES_DIR / "selection.erp.json"
FIXTURES_DIR = PROJECT_ROOT / "technographics" / "tests" / "fixtures"

TECH_PROPERTY = "technographic_signals"
TECH_PROPERTY_LABEL = "Technographic Signals"
NO_SIGNALS = "No signals detected"

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")


def log(msg):
    sys.stderr.write(f"[tech] {msg}\n")
    sys.stderr.flush()


def _flag(name, default=True):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


# ---- availability / vendored-package plumbing ------------------------------
_AVAILABLE = None


def _ensure_path():
    if str(TECH_SRC) not in sys.path:
        sys.path.insert(0, str(TECH_SRC))


def tech_available():
    """(ok, reason). Cached. False when the vendored package or dnspython is
    missing — callers degrade instead of crashing (mongo_store pattern)."""
    global _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE
    if not TECH_SRC.is_dir():
        _AVAILABLE = (False, f"vendored technographics package missing at {TECH_SRC}")
        return _AVAILABLE
    _ensure_path()
    try:
        import dns.resolver  # noqa: F401  (dnspython — requirements.txt)
    except Exception as exc:  # noqa: BLE001
        _AVAILABLE = (False, f"dnspython not installed ({exc.__class__.__name__}) — pip install -r requirements.txt")
        return _AVAILABLE
    _AVAILABLE = (True, "")
    return _AVAILABLE


# ---- ported from technographic-signals src/ (see VENDORED.md) ---------------
# _clean_domain: src/detectors/engine.py — reduce a website/domain field to a
# bare apex-ish host for DNS + fetching.
def _clean_domain(domain):
    if not domain:
        return None
    d = str(domain).strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/", 1)[0].split("?", 1)[0]
    if d.startswith("www."):
        d = d[4:]
    return d or None


# Bucket maps: src/detectors/category_map.py. The library's own taxonomy is
# richer; the output string only has 4 buckets. Vendor override wins, then
# category map, else the detection is dropped from the formatted string
# (it stays in tech_detail).
CATEGORY_TO_BUCKET = {
    "crm": "crm",
    "marketing_automation": "martech",
    "cdp": "martech",
    "customer_data_platform": "martech",
    "analytics": "martech",
    "product_analytics": "martech",
    "a_b_testing": "martech",
    "ab_testing": "martech",
    "email": "martech",
    "email_productivity": "martech",
    "form_builders": "martech",
    "surveys": "martech",
    "tag_managers": "ad_pixel",
    "advertising": "ad_pixel",
    "retargeting": "ad_pixel",
    "live_chat": "salestech",
    "customer_support": "salestech",
    "appointment_scheduling": "salestech",
    "sales_engagement": "salestech",
    # ERP suites (found by the subdomain probe step, not the marketing site)
    "erp": "erp",
}

VENDOR_BUCKET_OVERRIDE = {
    "facebook_pixel": "ad_pixel",
    "google_ads": "ad_pixel",
    "google_ads_conversion_tracking": "ad_pixel",
    "google_tag_manager": "ad_pixel",
    "linkedin_insight_tag": "ad_pixel",
    "linkedin_ads": "ad_pixel",
    "tiktok_pixel": "ad_pixel",
    "twitter_ads": "ad_pixel",
    "reddit_ads": "ad_pixel",
    "pinterest_conversion_tag": "ad_pixel",
    "microsoft_advertising": "ad_pixel",
    "snap_pixel": "ad_pixel",
    "quora_pixel": "ad_pixel",
    "6sense": "salestech",
    "demandbase": "salestech",
    "zoominfo": "salestech",
    "leadfeeder": "salestech",
    "clearbit_reveal": "salestech",
    "albacross": "salestech",
    "warmly": "salestech",
    "koala": "salestech",
    "gong": "salestech",
    "salesloft": "salestech",
    "apollo": "salestech",
    "outreach": "salestech",
    "qualified": "salestech",
    "drift": "salestech",
    "intercom": "salestech",
    "chili_piper": "salestech",
    "calendly": "salestech",
    "g2": "salestech",
    "factors_ai": "salestech",
    "reo": "salestech",
    "aisdr": "salestech",
}

# ---- copy playbook groups ----------------------------------------------------
# Finer-grained than the storage buckets (intent/ABM, sequencing, and chat all
# land in `salestech` above): these vendor-id sets drive which outreach play the
# generation prompts run. sequencing -> email 2 (no-disruption + run-rate);
# intent_abm / ads -> email 3 (signal activation). Chat/scheduling tools
# (PLAYBOOK_NEVER_MENTION) must never appear in copy at all; CRM/martech/the
# rest stay background-only under the ONE-tool rule.
PLAYBOOK_INTENT_ABM = {"6sense", "demandbase", "zoominfo", "clearbit_reveal",
                       "albacross", "warmly", "koala", "leadfeeder",
                       "factors_ai", "reo", "g2"}
PLAYBOOK_SEQUENCING = {"outreach", "salesloft", "apollo"}
# ads = the ad_pixel bucket minus this: GTM alone doesn't prove ad spend.
PLAYBOOK_ADS_EXCLUDE = {"google_tag_manager"}
PLAYBOOK_NEVER_MENTION = {"qualified", "drift", "intercom", "chili_piper", "calendly"}


def playbook_groups(detections):
    """Classify a tech_detail `detections` list into the copy playbook groups.

    Returns {"ads": [names], "intent_abm": [names], "sequencing": [names]} —
    keys always present, vendor display names sorted. Same trust bar as the
    formatted line: a low-confidence detection counts only when the vendor is
    corroborated high/medium elsewhere."""
    strong = {d.get("vendor_id") for d in detections
              if confidence_bucket(d.get("confidence") or 0.0) in ("high", "medium")}
    groups = {"ads": set(), "intent_abm": set(), "sequencing": set()}
    for d in detections:
        vid = d.get("vendor_id")
        if not vid:
            continue
        if confidence_bucket(d.get("confidence") or 0.0) == "low" and vid not in strong:
            continue
        name = d.get("vendor_name") or vid
        if vid in PLAYBOOK_SEQUENCING:
            groups["sequencing"].add(name)
        elif vid in PLAYBOOK_INTENT_ABM:
            groups["intent_abm"].add(name)
        elif d.get("bucket") == "ad_pixel" and vid not in PLAYBOOK_ADS_EXCLUDE:
            groups["ads"].add(name)
    return {k: sorted(v) for k, v in groups.items()}


def playbook_from_detail(tech_detail):
    """playbook_groups() from a stored tech_detail JSON string (or parsed dict).
    None when the row has no parseable detail (legacy rows -> callers keep the
    line-only background behavior)."""
    if not tech_detail:
        return None
    try:
        detail = json.loads(tech_detail) if isinstance(tech_detail, str) else tech_detail
        detections = detail.get("detections")
        if detections is None:
            return None
        return playbook_groups(detections)
    except (ValueError, TypeError, AttributeError):
        return None


_CATEGORY_DISPLAY = [
    ("crm", "CRM"),
    ("ad_pixel", "Ad Pixels"),
    ("martech", "Martech"),
    ("salestech", "Salestech"),
    ("erp", "ERP"),
]

Hit = namedtuple("Hit", "name category confidence evidence")


def bucket_for(vendor_id, category):
    if vendor_id in VENDOR_BUCKET_OVERRIDE:
        return VENDOR_BUCKET_OVERRIDE[vendor_id]
    return CATEGORY_TO_BUCKET.get(category)


def confidence_bucket(confidence):
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.6:
        return "medium"
    return "low"


# filter_low_confidence / format_signals: src/orchestrator.py — a low hit
# survives only when the same vendor is corroborated high/medium elsewhere;
# the formatted string uses a fixed bucket order, alphabetized vendors, empty
# buckets omitted, and the NO_SIGNALS literal so "we ran" is distinguishable
# from "we never ran".
def filter_low_confidence(hits):
    strong = {h.name for h in hits if h.confidence in ("high", "medium")}
    return [h for h in hits if h.confidence != "low" or h.name in strong]


def format_signals(hits):
    if not hits:
        return NO_SIGNALS
    by_cat = defaultdict(set)
    for h in hits:
        by_cat[h.category].add(h.name)
    parts = []
    for key, label in _CATEGORY_DISPLAY:
        vendors = sorted(by_cat.get(key, set()))
        if vendors:
            parts.append(f"{label}: {', '.join(vendors)}")
    return " | ".join(parts) if parts else NO_SIGNALS


# ---- engine singleton (matchers are read-only after construction) ----------
_ENGINE = None
_ENGINE_LOCK = threading.Lock()


class _Engine:
    def __init__(self):
        _ensure_path()
        from technographics.dns_matcher import DNSMatcher
        from technographics.loader import load_library, load_selection
        from technographics.web_matcher import WebMatcher

        sel_path = Path(os.environ.get("TECH_SELECTION_FILE") or DEFAULT_SELECTION)
        if not sel_path.is_absolute():
            sel_path = PROJECT_ROOT / sel_path
        selection = load_selection(sel_path) if sel_path.is_file() else None
        self.selection_name = sel_path.name if selection is not None else "full-catalogue"
        self.library = load_library(SIGNATURES_DIR, selection=selection)
        self.web_matcher = WebMatcher(self.library.web_signatures, self.library.vendors)
        self.dns_matcher = DNSMatcher(self.library.dns_signatures, self.library.vendors)
        # Union of every signature's CNAME probes (src/detectors/engine.py).
        self.subdomains = sorted({
            sub
            for sig in self.library.dns_signatures.values()
            for sub in sig.subdomains_to_probe
        })
        # Portal probes (ERP): vendors that declare BOTH subdomains_to_probe
        # (DNS side) and probe_paths (web side) opt into probe-then-fetch.
        from technographics.subdomain_prober import probe_specs
        self.has_probe_specs = bool(
            probe_specs(self.library.dns_signatures, self.library.web_signatures))


def _engine():
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                _ENGINE = _Engine()
    return _ENGINE


# ---- stdlib static fetcher --------------------------------------------------
# Port of src/site_fetcher.py's static mode (requests/bs4 -> urllib/html.parser
# to honor this repo's zero-pip-deps boot rule): same UA/Accept headers, same
# extracted surface (script srcs, cookie NAMES only, lowercased headers, first-
# wins meta map, js_globals empty — those need a JS engine).
class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.script_srcs = []
        self.meta_tags = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            src = (a.get("src") or "").strip()
            if src:
                self.script_srcs.append(src)
        elif tag == "meta":
            name = a.get("name") or a.get("property")
            if name:
                key = name.strip().lower()
                if key not in self.meta_tags:
                    self.meta_tags[key] = (a.get("content") or "").strip()


def _decode_body(raw, headers):
    enc = (headers.get("content-encoding") or "").lower()
    try:
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001 — serve the bytes we got
        pass
    return raw


def fetch_static(host, timeout=None):
    """GET https://<host> and extract detection surface. Returns
    (PageData, fetch_error) — a 4xx/5xx still yields PageData (its headers and
    error page are legitimate match candidates); (None, error) only when no
    response was obtained at all."""
    _ensure_path()
    from technographics.web_matcher import PageData

    url = f"https://{host}"
    timeout = timeout or _env_float("TECH_FETCH_TIMEOUT", 10.0)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    })

    resp, last_err = None, None
    for attempt in range(3):
        try:
            resp = opener.open(req, timeout=timeout)
            break
        except urllib.error.HTTPError as exc:
            resp = exc  # a real (4xx/5xx) response — inspect it
            break
        except Exception as exc:  # noqa: BLE001 — URLError, timeout, SSL, DNS
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                time.sleep(1 + attempt)
    if resp is None:
        return None, last_err

    try:
        raw = resp.read(3_000_000)  # cap pathological pages
        status = getattr(resp, "status", None) or getattr(resp, "code", 0) or 0
        final_url = resp.geturl() or url
        headers = {k.lower(): v for k, v in resp.headers.items()}
        charset = resp.headers.get_content_charset() or "utf-8"
    except Exception as exc:  # noqa: BLE001
        return None, f"read failed: {type(exc).__name__}: {exc}"
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass

    html = _decode_body(raw, headers).decode(charset, "replace")
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — keep whatever parsed before the choke
        pass

    fetch_error = f"HTTP {status}" if status >= 400 else None
    page = PageData(
        final_url=final_url,
        js_globals=[],  # needs a JS engine — see --rendered
        script_srcs=parser.script_srcs,
        cookies={c.name: "" for c in jar},
        headers=headers,
        html=html,
        meta_tags=parser.meta_tags,
    )
    return page, fetch_error


# ---- stdlib portal-probe fetcher --------------------------------------------
# Port of subdomain_prober._default_fetch (httpx -> urllib, same semantics):
# https then plain-http fallback, redirects followed, TLS UNVERIFIED (on-prem
# portals routinely run self-signed certs; only the response shape is
# fingerprinted), any 4xx/5xx dropped, cookie NAMES captured across the whole
# redirect chain (the CookieJar processes every hop). Injected into
# probe_subdomains() as its `fetcher` so the vendored default — which lazily
# imports httpx — never runs; prod stays at zero new pip deps.
_PROBE_MAX_BODY = 512_000  # chars; fingerprints sit in the first chunk of HTML


def _probe_fetch(url, timeout):
    """GET one probe URL -> PageData, or None on any transport failure or a
    >=400 status (upstream drops those outright — a portal that errors is not
    evidence). Probing is best-effort by design; never raises."""
    _ensure_path()
    from technographics.web_matcher import PageData

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(jar))

    for attempt_url in (url, url.replace("https://", "http://", 1)):
        req = urllib.request.Request(attempt_url, headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        })
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError:
            return None  # a real 4xx/5xx answer — never match against it
        except Exception:  # noqa: BLE001 — NXDOMAIN, TLS, timeout: try plain http
            continue
        try:
            raw = resp.read(3_000_000)
            headers = {k.lower(): v for k, v in resp.headers.items()}
            final_url = resp.geturl() or attempt_url
            charset = resp.headers.get_content_charset() or "utf-8"
        except Exception:  # noqa: BLE001
            return None
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        html = _decode_body(raw, headers).decode(charset, "replace")[:_PROBE_MAX_BODY]
        parser = _PageParser()
        try:
            parser.feed(html)
            parser.close()
        except Exception:  # noqa: BLE001 — keep whatever parsed before the choke
            pass
        return PageData(
            final_url=final_url,
            js_globals=[],
            script_srcs=parser.script_srcs,
            cookies={c.name: "" for c in jar},
            headers=headers,
            html=html,
            meta_tags=parser.meta_tags,
        )
    return None


def probe_domain(host, eng=None):
    """Run the vendored probe-then-fetch step (ERP portals) with the stdlib
    fetcher. Returns (detections, probe_error). An empty list with no error is
    the normal case: no portal answered with that vendor's fingerprint."""
    eng = eng or _engine()
    if not eng.has_probe_specs:
        return [], None
    timeout = _env_float("TECH_PROBE_TIMEOUT", 4.0)

    async def fetcher(u):
        return await asyncio.to_thread(_probe_fetch, u, timeout)

    try:
        from technographics.subdomain_prober import probe_subdomains
        dets = probe_subdomains(
            host, eng.library.dns_signatures, eng.library.web_signatures,
            eng.library.vendors, timeout=timeout, fetcher=fetcher)
        return dets, None
    except Exception as exc:  # noqa: BLE001 — probes must never block the scan
        return [], f"{type(exc).__name__}: {exc}"


# ---- detection ---------------------------------------------------------------
def detect_domain(domain, rendered=False):
    """Pure detection (no DB): fetch + DNS + portal probes -> match -> fuse ->
    bucket/format. Returns {formatted, detections, fetch_error, dns_error,
    probe_error, error, duration_ms, rendered}. `error` is set — and formatted
    is None — ONLY when both primary channels (fetch AND dns) failed: a
    network-dead run must never be stored as "No signals detected". Probes are
    best-effort on top (upstream engine.py semantics); their detections carry
    source="probe"."""
    ok, reason = tech_available()
    if not ok:
        raise RuntimeError(f"technographic detection unavailable: {reason}")
    eng = _engine()

    host = _clean_domain(domain)
    if not host:
        return {"formatted": None, "detections": [], "fetch_error": None, "dns_error": None,
                "error": f"invalid domain: {domain!r}", "duration_ms": 0, "rendered": False}

    t0 = time.monotonic()
    page, fetch_error = None, None
    if rendered:
        from technographics.web_collector import collect_web  # lazy: playwright
        page = collect_web(host)
        if page.error:
            fetch_error = page.error
            if not (page.html or page.script_srcs):
                page = None
    else:
        page, fetch_error = fetch_static(host)
    web_dets = eng.web_matcher.match(page) if page is not None else []

    dns_dets, dns_error, dns_records = [], None, 0
    try:
        from technographics.dns_collector import collect_dns  # lazy: dnspython
        records = collect_dns(host, subdomains=eng.subdomains,
                              timeout=_env_float("TECH_DNS_TIMEOUT", 3.0))
        dns_error = records.error
        # The collector swallows per-lookup failures, so blocked egress (or a
        # dead domain) looks like "no records", not an error — count them.
        dns_records = (len(records.a) + len(records.mx) + len(records.txt)
                       + len(records.ns) + len(records.soa) + len(records.cname))
        dns_dets = eng.dns_matcher.match(records)
    except Exception as exc:  # noqa: BLE001 — DNS is best-effort
        dns_error = f"{type(exc).__name__}: {exc}"

    probe_dets, probe_error = [], None
    if _flag("TECH_PROBES", True):
        probe_dets, probe_error = probe_domain(host, eng)

    from technographics.fusion import fuse
    fused = fuse(dns_dets, [*web_dets, *probe_dets])

    detections, hits = [], []
    for det in fused:
        bucket = bucket_for(det.vendor_id, det.category)
        detections.append({
            "vendor_id": det.vendor_id, "vendor_name": det.vendor_name,
            "category": det.category, "bucket": bucket, "source": det.source,
            "confidence": round(det.confidence, 4), "tier_signal": det.tier_signal,
            "evidence": list(det.evidence[:3]),
        })
        if bucket:
            hits.append(Hit(det.vendor_name, bucket, confidence_bucket(det.confidence),
                            list(det.evidence[:3])))

    formatted = format_signals(filter_low_confidence(hits))
    error = None
    # A live domain always answers SOMETHING (NS/SOA at minimum) — no response
    # on either channel means the scan failed (dead domain or blocked egress),
    # and must not be stored as a confident "No signals detected".
    if page is None and dns_error is None and dns_records == 0:
        dns_error = "no records (resolver unreachable or domain does not resolve)"
    # A probe detection is positive evidence the network worked — it rescues a
    # scan whose apex fetch + DNS both came up empty (marketing site down or
    # unresolvable, ERP portal alive on a subdomain).
    if page is None and (dns_error is not None and not dns_dets) and not probe_dets:
        error = f"fetch: {fetch_error or 'failed'}; dns: {dns_error}"
        formatted = None

    return {"formatted": formatted, "detections": detections, "fetch_error": fetch_error,
            "dns_error": dns_error, "dns_records": dns_records,
            "probe_error": probe_error, "error": error,
            "duration_ms": int((time.monotonic() - t0) * 1000), "rendered": bool(rendered)}


# ---- store + orchestrate -----------------------------------------------------
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


def _db():
    global _SCHEMA_READY
    conn = db.connect()
    if not _SCHEMA_READY:
        with _SCHEMA_LOCK:
            if not _SCHEMA_READY:
                db.init_schema(conn)  # guarantees the tech_* columns exist
                _SCHEMA_READY = True
    return conn


def detect_and_store(domain, company=None, force=False, hubspot=None, rendered=False):
    """Cache-aware detect for one domain: skip when the last SUCCESSFUL scan is
    younger than TECH_REFRESH_DAYS (failed scans always retry), else scan,
    upsert the account_signals row, and best-effort write the formatted line to
    HubSpot. hubspot=None follows TECH_HUBSPOT_WRITEBACK; True/False overrides."""
    ok, reason = tech_available()
    if not ok:
        raise RuntimeError(f"technographic detection unavailable: {reason}")
    host = _clean_domain(domain)
    if not host:
        raise ValueError(f"invalid domain: {domain!r}")

    conn = _db()
    try:
        row = db.get_signal(conn, host)
        refresh_days = _env_float("TECH_REFRESH_DAYS", 90)
        if (row and not force and row.get("tech_signals") is not None
                and db.tech_fresh(row, days=refresh_days)):
            return {"domain": host, "skipped": True,
                    "tech_signals": row.get("tech_signals"),
                    "tech_error": row.get("tech_error"),
                    "tech_checked_at": row.get("tech_checked_at"),
                    "detections": _detection_count(row.get("tech_detail")),
                    "playbook": playbook_from_detail(row.get("tech_detail")),
                    "hubspot": None}
    finally:
        conn.close()

    res = detect_domain(host, rendered=rendered)

    # HubSpot write-back BEFORE the upsert (no DB connection held across the
    # HTTP call) so its outcome can be persisted into tech_detail and shown in
    # the Signals drawer on a later page load — not just returned to the caller.
    hs = None
    if res["formatted"] is not None:
        enabled = _flag("TECH_HUBSPOT_WRITEBACK", True) if hubspot is None else bool(hubspot)
        hs = hubspot_writeback(host, res["formatted"]) if enabled else {"ok": False, "reason": "disabled"}

    detail = {
        "detections": res["detections"],
        "fetch_error": res["fetch_error"],
        "dns_error": res["dns_error"],
        "dns_records": res["dns_records"],
        "probe_error": res["probe_error"],
        "selection": _engine().selection_name,
        "duration_ms": res["duration_ms"],
        "rendered": res["rendered"],
        "hubspot": hs,
    }
    conn = _db()
    try:
        db.upsert_tech_signals(conn, host, res["formatted"],
                               tech_detail=json.dumps(detail, ensure_ascii=False),
                               tech_error=res["error"], company_name=company)
        stored = db.get_signal(conn, host) or {}
    finally:
        conn.close()

    return {"domain": host, "skipped": False, "tech_signals": res["formatted"],
            "tech_error": res["error"], "tech_checked_at": stored.get("tech_checked_at"),
            "detections": len(res["detections"]),
            "playbook": playbook_groups(res["detections"]), "hubspot": hs}


def _detection_count(tech_detail):
    try:
        return len(json.loads(tech_detail or "{}").get("detections") or [])
    except (ValueError, TypeError):
        return 0


# ---- HubSpot write-back (best-effort; never raises) --------------------------
_HS_LOCK = threading.Lock()
_HS_CLIENT = None
_HS_PROPERTY_READY = False


def hubspot_writeback(domain, formatted):
    global _HS_CLIENT, _HS_PROPERTY_READY
    try:
        if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
            return {"ok": False, "reason": "no_token"}
        import hubspot_client as hc
        with _HS_LOCK:
            if _HS_CLIENT is None:
                _HS_CLIENT = hc.HubSpotClient()
            client = _HS_CLIENT
            if not _HS_PROPERTY_READY:
                client.ensure_company_property(TECH_PROPERTY, TECH_PROPERTY_LABEL)
                _HS_PROPERTY_READY = True
        company_id = client.find_company_id_by_domain(domain)
        if not company_id:
            return {"ok": False, "reason": "no_company"}
        client.update_company(company_id, {TECH_PROPERTY: formatted})
        return {"ok": True, "company_id": company_id}
    except Exception as exc:  # noqa: BLE001 — write-back must never fail a scan
        log(f"hubspot writeback failed for {domain}: {exc}")
        return {"ok": False, "error": str(exc)[:300]}


# ---- bulk ---------------------------------------------------------------------
def backfill(domains=None, stale_days=None, limit=None, workers=4, force=False,
             hubspot=None, progress=None):
    """Scan many domains (ThreadPoolExecutor). domains=None pulls the never-
    scanned (plus stale, when stale_days is set) from account_signals. Returns
    {total, detected, skipped, errors, hubspot_ok, hubspot_missing}."""
    ok, reason = tech_available()
    if not ok:
        raise RuntimeError(f"technographic detection unavailable: {reason}")

    if domains is None:
        conn = _db()
        try:
            rows = db.domains_missing_tech(conn, stale_days=stale_days, limit=limit)
        finally:
            conn.close()
        items = [(r["domain"], r.get("company_name")) for r in rows]
    else:
        items = [(d, None) for d in (_clean_domain(x) for x in domains) if d]
        if limit:
            items = items[:int(limit)]

    summary = {"total": len(items), "detected": 0, "skipped": 0, "errors": 0,
               "hubspot_ok": 0, "hubspot_missing": 0}
    if not items:
        return summary
    _engine()  # build the library once before the pool fans out

    def work(item):
        d, c = item
        try:
            return d, detect_and_store(d, company=c, force=force, hubspot=hubspot)
        except Exception as exc:  # noqa: BLE001 — one bad domain never kills the run
            return d, {"domain": d, "error_exc": str(exc)[:300]}

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        for d, res in ex.map(work, items):
            done += 1
            if res.get("error_exc") or res.get("tech_error"):
                summary["errors"] += 1
            elif res.get("skipped"):
                summary["skipped"] += 1
            else:
                summary["detected"] += 1
            hs = res.get("hubspot") or {}
            if hs.get("ok"):
                summary["hubspot_ok"] += 1
            elif hs.get("reason") == "no_company":
                summary["hubspot_missing"] += 1
            if progress:
                try:
                    progress(done, len(items), d, res)
                except Exception:  # noqa: BLE001 — progress is cosmetic
                    pass
    return summary


# ---- offline self-test ---------------------------------------------------------
def self_test():
    """Offline check of the ERP configuration (no network, no dnspython, no
    DB): the default selection must load exactly the four probe-enabled ERP
    suites, each suite's fingerprints must match a synthetic portal response,
    the probe guards must drop catch-all servers, and — the Value Global scope
    rule — the template's marketing/sales fixtures must no longer match
    anything. Exit code 0/1."""
    os.environ.pop("TECH_SELECTION_FILE", None)  # test the shipped default
    _ensure_path()
    from technographics.dns_matcher import DNSRecords
    from technographics.subdomain_prober import probe_specs, probe_subdomains
    from technographics.web_matcher import PageData

    eng = _engine()
    erp_ids = {"oracle_ebs", "oracle_fusion_cloud_erp", "peoplesoft", "jd_edwards"}
    specs = probe_specs(eng.library.dns_signatures, eng.library.web_signatures)

    def page(final_url, html="", cookies=(), headers=None):
        return PageData(final_url=final_url, js_globals=[], script_srcs=[],
                        cookies={c: "" for c in cookies},
                        headers=dict(headers or {}), html=html, meta_tags={})

    def match_ids(p):
        return {d.vendor_id for d in eng.web_matcher.match(p)}

    # One synthetic portal response per suite, built from disjoint fingerprints.
    ebs_ids = match_ids(page("https://ebs.acme.com/OA_HTML/AppsLogin",
                             html='<img src="/OA_HTML/cabo/images/x.gif"> AppsLocalLogin'))
    ps_ids = match_ids(page("https://hr.acme.com/",
                            html='<a href="/psp/ps/signon.html">Sign in</a> ptStyle',
                            cookies=("PS_TOKEN", "PS_LOGINLIST")))
    jde_ids = match_ids(page("https://jde.acme.com/jde/E1Menu.maf",
                             html="E1Menu.maf com.jdedwards.web jdeLoginTitle"))
    fusion_ids = match_ids(page(
        "https://xyz.fa.ocs.oraclecloud.com/fscmUI/faces/AtkHomePageWelcome",
        html='<div id="AtkHomePageWelcome"></div>'))

    # Probe orchestration guards, via an injected offline fetcher (the same
    # seam tech_signals uses in prod for its stdlib fetcher).
    fusion_redirect = page(
        "https://fa-xyz.fa.ocs.oraclecloud.com/fscmUI/faces/AtkHomePageWelcome",
        html="<html>loading</html>")
    ps_portal = page("https://ps.acme.com/", html='<a href="/psc/ps/">portal</a>',
                     cookies=("PS_TOKEN",))

    async def fetch_acme(url):
        if url == "https://erp.acme.com/":   # fusion probe -> organic off-site redirect
            return fusion_redirect
        if url == "https://ps.acme.com/":    # peoplesoft probe -> response evidence
            return ps_portal
        return None

    acme = probe_subdomains("acme.com", eng.library.dns_signatures,
                            eng.library.web_signatures, eng.library.vendors,
                            fetcher=fetch_acme)
    acme_ids = {d.vendor_id for d in acme}

    async def fetch_catchall(url):  # SPA catch-all: 200s every path back at you
        return page(url, html="<html><body>welcome</body></html>")

    catchall = probe_subdomains("catchall.test", eng.library.dns_signatures,
                                eng.library.web_signatures, eng.library.vendors,
                                fetcher=fetch_catchall)

    async def fetch_www(url):  # wildcard DNS funneling every subdomain to www
        return page("https://www.catchall.test" + url.split(".test", 1)[1],
                    html="<html><body>welcome</body></html>")

    www_funnel = probe_subdomains("catchall.test", eng.library.dns_signatures,
                                  eng.library.web_signatures, eng.library.vendors,
                                  fetcher=fetch_www)

    # The template's marketing-stack fixtures (Intercom/Segment page + HubSpot
    # DNS records) must now be out of scope: Value Global detects ERP only.
    fx_page = PageData(**json.loads((FIXTURES_DIR / "sample_page_data.json").read_text()))
    fx_records = DNSRecords(**json.loads((FIXTURES_DIR / "sample_dns_records.json").read_text()))
    fx_web = eng.web_matcher.match(fx_page)
    fx_dns = eng.dns_matcher.match(fx_records)

    hits = []
    for det in acme:
        bucket = bucket_for(det.vendor_id, det.category)
        if bucket:
            hits.append(Hit(det.vendor_name, bucket, confidence_bucket(det.confidence), []))
    formatted = format_signals(filter_low_confidence(hits))
    pb = playbook_groups([{"vendor_id": d.vendor_id, "vendor_name": d.vendor_name,
                           "bucket": bucket_for(d.vendor_id, d.category),
                           "confidence": d.confidence} for d in acme])

    checks = [
        ("default selection is the four ERP suites", set(eng.library.vendors) == erp_ids),
        ("selection file is selection.erp.json", eng.selection_name == "selection.erp.json"),
        ("all four suites are probe-enabled", set(specs) == erp_ids),
        ("EBS page fingerprint matches", "oracle_ebs" in ebs_ids),
        ("PeopleSoft cookies/html match", "peoplesoft" in ps_ids),
        ("JD Edwards page fingerprint matches", "jd_edwards" in jde_ids),
        ("Fusion pod URL + page match", "oracle_fusion_cloud_erp" in fusion_ids),
        ("probe finds Fusion via organic off-site redirect",
         "oracle_fusion_cloud_erp" in acme_ids),
        ("probe finds PeopleSoft via response evidence", "peoplesoft" in acme_ids),
        ("no cross-vendor shadow hits on acme",
         acme_ids == {"oracle_fusion_cloud_erp", "peoplesoft"}),
        ("probe detections carry source=probe",
         bool(acme) and all(d.source == "probe" for d in acme)),
        ("catch-all server yields nothing", not catchall),
        ("www-funnel redirect yields nothing", not www_funnel),
        ("marketing fixture page out of scope (0 web matches)", not fx_web),
        ("marketing fixture DNS out of scope (0 dns matches)", not fx_dns),
        ("ERP hits land in the ERP bucket",
         formatted == "ERP: Oracle Fusion Cloud ERP, Oracle PeopleSoft"),
        ("bucket_for maps the erp category", bucket_for("peoplesoft", "erp") == "erp"),
        ("ERP stack is background-only for copy",
         pb == {"ads": [], "intent_abm": [], "sequencing": []}),
        ("playbook_from_detail None on legacy rows",
         playbook_from_detail(None) is None and playbook_from_detail("not json") is None),
    ]
    failed = [name for name, passed in checks if not passed]
    for name, passed in checks:
        log(f"{'PASS' if passed else 'FAIL'}  {name}")
    log(f"formatted: {formatted}")
    print(json.dumps({"ok": not failed, "failed": failed, "formatted": formatted,
                      "probe_detections": len(acme),
                      "probe_vendors": sorted(specs),
                      "vendors_loaded": len(eng.library.vendors)}))
    return 0 if not failed else 1


# ---- CLI ------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Technographic detection (DNS + static web)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--domain", help="scan one domain (cache-aware; see --force)")
    mode.add_argument("--missing", action="store_true",
                      help="scan every account_signals domain with no tech scan yet")
    mode.add_argument("--self-test", action="store_true", dest="self_test",
                      help="offline fixture check (no network, no dnspython)")
    ap.add_argument("--company", help="company name to fill on a blank row (--domain only)")
    ap.add_argument("--force", action="store_true", help="re-scan even if fresh")
    ap.add_argument("--rendered", action="store_true",
                    help="use the Playwright collector (Claude sessions only; never prod)")
    ap.add_argument("--no-hubspot", action="store_true", help="skip the HubSpot write-back")
    ap.add_argument("--stale-days", type=int, default=None,
                    help="with --missing: also re-scan scans older than N days")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    hubspot = False if args.no_hubspot else None
    if args.domain:
        log(f"scanning {args.domain} ...")
        res = detect_and_store(args.domain, company=args.company, force=args.force,
                               hubspot=hubspot, rendered=args.rendered)
        log("skipped (fresh)" if res.get("skipped") else f"-> {res.get('tech_signals') or res.get('tech_error')}")
        print(json.dumps(res, ensure_ascii=False))
        return

    def progress(done, total, domain, res):
        status = ("error" if (res.get("error_exc") or res.get("tech_error"))
                  else "skip" if res.get("skipped") else "ok")
        log(f"[{done}/{total}] {domain}: {status}")

    summary = backfill(stale_days=args.stale_days, limit=args.limit, workers=args.workers,
                       force=args.force, hubspot=hubspot, progress=progress)
    log(f"done: {summary}")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
