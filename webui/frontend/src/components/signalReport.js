// Shared interpretation of one /api/signals/detail payload — the same wording the
// SignalDetail drawer renders, as plain data. The Signals page Export builds its
// JSON from buildSignalReport() so the file reads section-by-section like the
// drawer (status badges → signal → technographics → hiring → ERP news triggers →
// contacts), with the untouched DB row kept under `raw`.
export const NO_TECH = 'No signals detected'
export const NO_HIRING = 'No open roles detected'
export const NO_NEWS = 'No ERP news signals detected'

export function pct(x) { return x == null ? '—' : `${Math.round(x * 100)}%` }

export function hubspotLine(hs, envVar = 'TECH_HUBSPOT_WRITEBACK') {
  if (!hs) return 'not attempted'
  if (hs.ok) return `updated company ${hs.company_id}`
  if (hs.reason === 'disabled') return `disabled (${envVar}=0)`
  if (hs.reason === 'not_matched') return 'skipped (company not matched by Prospeo)'
  if (hs.reason === 'no_company') return 'no matching company in HubSpot'
  if (hs.reason === 'no_token') return 'no HubSpot token configured'
  return hs.error ? `error: ${hs.error}` : 'not written'
}

function ago(at, days) {
  return at ? `${at}${days != null ? ` (${days}d ago)` : ''}` : null
}

export function triggerStatus(r) {
  return r.found ? 'found' : r.skipped ? 'skipped' : r.error ? 'error' : 'not found'
}

export function buildSignalReport(detail) {
  const s = detail?.signal
  if (!s) return { domain: detail?.domain, error: detail?.error || 'no cached signal', contacts: detail?.contacts || [] }
  const td = s.tech_detail
  const dets = td?.detections || []
  const hd = s.hiring_detail
  const salesTitles = hd?.sales_titles || []
  const allTitles = hd?.active_titles || []
  const nd = s.news_detail
  const triggers = Object.entries(nd?.triggers || {})
  const found = triggers.filter(([, r]) => r.found)
  const win = found[0]

  const techStatus = s.tech_signals && s.tech_signals !== NO_TECH ? `${dets.length} tech`
    : s.tech_signals === NO_TECH ? 'no ERP detected'
      : s.tech_error ? 'scan failed' : 'not scanned'
  const hiringStatus = s.hiring_signals && s.hiring_signals !== NO_HIRING
    ? (salesTitles.length ? `${salesTitles.length} sales roles open` : 'open roles')
    : s.hiring_signals === NO_HIRING ? 'no open roles'
      : s.hiring_error ? 'hiring scan failed' : 'hiring not scanned'
  const newsCount = found.length || 1
  const newsStatus = s.news_signals && s.news_signals !== NO_NEWS
    ? `${newsCount} ERP trigger${newsCount === 1 ? '' : 's'}`
    : s.news_signals === NO_NEWS ? 'no ERP triggers'
      : s.news_error ? 'news research failed' : 'news not researched'

  return {
    domain: s.domain,
    company_name: s.company_name || null,
    status: {
      trigger: win ? (win[1].label || win[0]) : s.news_checked_at ? 'no ERP trigger' : 'not researched',
      research: s.age_days != null ? `researched ${s.age_days}d ago${s.fresh ? '' : ' · stale'}` : null,
      tech: techStatus,
      hiring: hiringStatus,
      news: newsStatus,
    },
    signal: {
      text: s.signal || null,
      researched: ago(s.researched_at, s.age_days),
      researched_at: s.researched_at || null,
      age_days: s.age_days,
      fresh: s.fresh,
      model: s.model || null,
    },
    technographics: {
      status: techStatus,
      summary: s.tech_signals && s.tech_signals !== NO_TECH ? s.tech_signals
        : s.tech_signals === NO_TECH ? 'Scan ran — no Oracle ERP suite detected (EBS, Fusion Cloud ERP, PeopleSoft, JD Edwards).'
          : s.tech_error ? `Scan failed: ${s.tech_error}` : 'Not scanned yet.',
      detections: dets.map((x) => ({
        vendor: x.vendor_name,
        vendor_id: x.vendor_id,
        tier_signal: x.tier_signal || null,
        bucket: x.bucket || null,
        source: x.source,
        confidence: pct(x.confidence),
        evidence: x.evidence || [],
      })),
      scanned: ago(s.tech_checked_at, s.tech_age_days),
      dns_records: td?.dns_records ?? null,
      duration: td?.duration_ms != null ? `${td.duration_ms} ms` : null,
      selection: td?.selection || null,
      mode: td?.rendered ? 'rendered (Playwright)' : td ? 'static' : null,
      fetch_error: td?.fetch_error || null,
      dns_error: td?.dns_error || null,
      hubspot: td ? hubspotLine(td.hubspot) : null,
    },
    hiring: {
      status: hiringStatus,
      summary: s.hiring_signals && s.hiring_signals !== NO_HIRING ? s.hiring_signals
        : s.hiring_signals === NO_HIRING
          ? (hd?.error_code ? `Not matched by Prospeo (${hd.error_code}) — treated as no data.` : 'Scan ran, no open job postings found.')
          : s.hiring_error ? `Scan failed: ${s.hiring_error}` : 'Not scanned yet.',
      sales_titles: salesTitles,
      all_titles: allTitles,
      checked: ago(s.hiring_checked_at, s.hiring_age_days),
      active_roles: hd?.active_count ?? null,
      sales_matches: hd ? salesTitles.length : null,
      error_code: hd?.error_code || null,
      duration: hd?.duration_ms != null ? `${hd.duration_ms} ms` : null,
      hubspot: hd ? hubspotLine(hd.hubspot, 'HIRING_HUBSPOT_WRITEBACK') : null,
    },
    news: {
      status: newsStatus,
      summary: s.news_error ? `Research failed: ${s.news_error}`
        : !s.news_signals && triggers.length === 0 ? 'Not researched yet.' : (s.news_signals || null),
      triggers: triggers.map(([id, r]) => ({
        id,
        label: r.label || id,
        status: triggerStatus(r),
        score: r.score ?? null,
        date: r.date || null,
        headline: r.headline || null,
        summary: r.summary || null,
        skipped_reason: r.skipped || null,
        error: r.error || null,
        source_url: r.source_url || null,
        details: r.details ?? null,
      })),
      researched: ago(s.news_checked_at, s.news_age_days),
      triggers_found: nd ? found.length : null,
      web_searches: nd?.web_searches ?? null,
      duration: nd?.duration_ms != null ? `${Math.round(nd.duration_ms / 1000)} s` : null,
      model: nd?.model || null,
      hubspot: nd ? hubspotLine(nd.hubspot, 'NEWS_HUBSPOT_WRITEBACK') : null,
    },
    contacts: (detail.contacts || []).map((c) => ({
      name: `${c.first_name || ''} ${c.last_name || ''}`.trim() || null,
      title: c.title || null,
      persona: c.persona || null,
      status: c.status || null,
      batch: c.batch_id != null ? `#${c.batch_id}` : null,
      contact_id: c.contact_id,
    })),
    raw: s,
  }
}
