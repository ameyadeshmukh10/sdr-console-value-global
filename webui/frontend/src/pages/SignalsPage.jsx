import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num } from '../components/ui.jsx'
import SignalDetail from '../components/SignalDetail.jsx'

// Signal cache — per-company research reused for 90 days so a company is searched
// once instead of once per contact / per re-run. Force-refresh re-searches one.
// Tech = the technographic scan (website + DNS fingerprinting + ERP portal probes) stored alongside:
// per-row Detect re-scans one company, "Detect missing" backfills the rest.
// Hiring = the Prospeo job-postings scan (open roles + sales subset), same cache;
// single-domain detect lives in the row drawer, "Detect hiring" backfills the rest.
// News = the ERP-trigger web research (M&A carve-out, ERP migration, license audit,
// EBS-on-OCI, EBS performance), same cache on a 30-day window; single-domain
// research lives in the row drawer, "Research news" backfills the rest.
const NO_TECH = 'No signals detected'
const NO_HIRING = 'No open roles detected'
const NO_NEWS = 'No ERP news signals detected'

export default function SignalsPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(null)
  const [detecting, setDetecting] = useState(null)
  const [bulkJob, setBulkJob] = useState(null)
  const [hiringJob, setHiringJob] = useState(null)
  const [newsJob, setNewsJob] = useState(null)
  const [openDomain, setOpenDomain] = useState(null)

  function load() {
    api.signals().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }
  useEffect(() => { load() }, [])

  async function refresh(domain) {
    setRefreshing(domain); setError(null)
    try {
      const d = await api.refreshSignal(domain)
      if (d.ok === false) setError(d.error || 'refresh failed')
      else setData(d)
    } catch (e) { setError(e.message) }
    finally { setRefreshing(null) }
  }

  async function detect(domain) {
    setDetecting(domain); setError(null)
    try {
      const d = await api.detectTech(domain, true)
      if (d.ok === false) setError(d.error || 'tech detect failed')
      else setData(d)
    } catch (e) { setError(e.message) }
    finally { setDetecting(null) }
  }

  async function startBulk() {
    setError(null)
    try {
      const d = await api.techBackfill({})
      setBulkJob({ job_id: d.job_id, status: 'running', total: d.total, done: 0 })
    } catch (e) { setError(e.message) }
  }

  async function startHiringBulk() {
    setError(null)
    try {
      const d = await api.hiringBackfill({})
      setHiringJob({ job_id: d.job_id, status: 'running', total: d.total, done: 0 })
    } catch (e) { setError(e.message) }
  }

  async function startNewsBulk() {
    setError(null)
    try {
      const d = await api.newsBackfill({})
      setNewsJob({ job_id: d.job_id, status: 'running', total: d.total, done: 0 })
    } catch (e) { setError(e.message) }
  }

  // Poll the bulk job while it runs; reload the table when it lands.
  useEffect(() => {
    if (!bulkJob || bulkJob.status !== 'running') return
    const t = setInterval(async () => {
      try {
        const j = await api.techBackfillStatus(bulkJob.job_id)
        setBulkJob(j)
        if (j.status !== 'running') load()
      } catch (e) { setBulkJob(null); setError(e.message) }
    }, 2500)
    return () => clearInterval(t)
  }, [bulkJob?.job_id, bulkJob?.status])

  // Same for the hiring backfill (independent job registry server-side).
  useEffect(() => {
    if (!hiringJob || hiringJob.status !== 'running') return
    const t = setInterval(async () => {
      try {
        const j = await api.hiringBackfillStatus(hiringJob.job_id)
        setHiringJob(j)
        if (j.status !== 'running') load()
      } catch (e) { setHiringJob(null); setError(e.message) }
    }, 2500)
    return () => clearInterval(t)
  }, [hiringJob?.job_id, hiringJob?.status])

  // And the news backfill (its own registry; scans take minutes each).
  useEffect(() => {
    if (!newsJob || newsJob.status !== 'running') return
    const t = setInterval(async () => {
      try {
        const j = await api.newsBackfillStatus(newsJob.job_id)
        setNewsJob(j)
        if (j.status !== 'running') load()
      } catch (e) { setNewsJob(null); setError(e.message) }
    }, 4000)
    return () => clearInterval(t)
  }, [newsJob?.job_id, newsJob?.status])

  const signals = data?.signals || []
  const fresh = signals.filter((s) => s.fresh).length
  const recent = signals.filter((s) => s.has_recent).length
  const scanned = signals.filter((s) => s.tech_age_days != null).length
  const withTech = signals.filter((s) => s.tech_signals && s.tech_signals !== NO_TECH).length
  const missing = signals.filter((s) => s.tech_age_days == null).length
  const techOff = data ? data.tech_available === false : false
  const bulkRunning = bulkJob?.status === 'running'
  const hiringScanned = signals.filter((s) => s.hiring_age_days != null).length
  const withHiring = signals.filter((s) => s.hiring_signals && s.hiring_signals !== NO_HIRING).length
  const missingHiring = signals.filter((s) => s.hiring_age_days == null).length
  const hiringOff = data ? data.hiring_available === false : false
  const hiringRunning = hiringJob?.status === 'running'
  const newsScanned = signals.filter((s) => s.news_age_days != null).length
  const withNews = signals.filter((s) => s.news_signals && s.news_signals !== NO_NEWS).length
  const missingNews = signals.filter((s) => s.news_age_days == null).length
  const newsOff = data ? data.news_available === false : false
  const newsRunning = newsJob?.status === 'running'

  return (
    <div>
      <h1 className="page-title">Signals</h1>
      <p className="page-sub">Per-company research cache. Fresh entries (&lt;90 days) are reused, so the AI SDR skips the web search. Tech = detected stack from a website + DNS scan. Hiring = open roles from a live job-postings lookup (sales roles feed email 2). News = web-researched ERP triggers (M&amp;A carve-out, ERP migration, license audit, EBS on OCI, EBS performance; 30-day cache).</p>

      <ErrorBanner error={error} />

      <div className="grid stat-grid" style={{ marginBottom: 20 }}>
        <Stat label="Cached accounts" value={num(data?.count || 0)} />
        <Stat label="Fresh (<90d)" value={num(fresh)} sub="reused, no re-search" />
        <Stat label="Real signal" value={num(recent)} tone="good" />
        <Stat label="Fallback (no signal)" value={num(signals.length - recent)} sub="product/GTM anchor" tone="warn" />
        <Stat label="Tech scanned" value={techOff ? '—' : num(scanned)}
          sub={techOff ? (data?.tech_reason || 'detection unavailable') : `${num(withTech)} with detections`}
          tone={techOff ? 'warn' : undefined} />
        <Stat label="Hiring scanned" value={hiringOff ? '—' : num(hiringScanned)}
          sub={hiringOff ? (data?.hiring_reason || 'detection unavailable') : `${num(withHiring)} with open roles`}
          tone={hiringOff ? 'warn' : undefined} />
        <Stat label="News researched" value={newsOff ? '—' : num(newsScanned)}
          sub={newsOff ? (data?.news_reason || 'research unavailable') : `${num(withNews)} with ERP triggers`}
          tone={newsOff ? 'warn' : undefined} />
      </div>

      {data && ((!techOff && (missing > 0 || bulkJob)) || (!hiringOff && (missingHiring > 0 || hiringJob)) || (!newsOff && (missingNews > 0 || newsJob))) && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 10, flexWrap: 'wrap' }}>
          {!techOff && missing > 0 && !bulkRunning && (
            <button className="ghost sm" onClick={startBulk}>Detect missing ({num(missing)})</button>
          )}
          {!techOff && bulkJob && (
            <span className="muted" style={{ fontSize: 13 }}>
              {bulkRunning
                ? <>Detecting tech… {bulkJob.done}/{bulkJob.total}{bulkJob.current ? ` (${bulkJob.current})` : ''}{bulkJob.errors ? ` · ${bulkJob.errors} errors` : ''}</>
                : bulkJob.status === 'done'
                  ? <>Tech backfill done: {bulkJob.detected} detected, {bulkJob.skipped} skipped{bulkJob.errors ? `, ${bulkJob.errors} errors` : ''}</>
                  : <>Tech backfill failed: {bulkJob.error || 'unknown error'}</>}
            </span>
          )}
          {!hiringOff && missingHiring > 0 && !hiringRunning && (
            <button className="ghost sm" onClick={startHiringBulk}
              title="One Prospeo credit per company scanned">Detect hiring ({num(missingHiring)})</button>
          )}
          {!hiringOff && hiringJob && (
            <span className="muted" style={{ fontSize: 13 }}>
              {hiringRunning
                ? <>Detecting hiring… {hiringJob.done}/{hiringJob.total}{hiringJob.current ? ` (${hiringJob.current})` : ''}{hiringJob.errors ? ` · ${hiringJob.errors} errors` : ''}</>
                : hiringJob.status === 'done'
                  ? <>Hiring backfill done: {hiringJob.detected} detected, {hiringJob.skipped} skipped{hiringJob.errors ? `, ${hiringJob.errors} errors` : ''}</>
                  : <>Hiring backfill failed: {hiringJob.error || 'unknown error'}</>}
            </span>
          )}
          {!newsOff && missingNews > 0 && !newsRunning && (
            <button className="ghost sm" onClick={startNewsBulk}
              title="Web-research the 5 ERP triggers per company (up to 5 web-search calls each; minutes per company)">Research news ({num(missingNews)})</button>
          )}
          {!newsOff && newsJob && (
            <span className="muted" style={{ fontSize: 13 }}>
              {newsRunning
                ? <>Researching news… {newsJob.done}/{newsJob.total}{newsJob.current ? ` (${newsJob.current})` : ''}{newsJob.errors ? ` · ${newsJob.errors} errors` : ''}</>
                : newsJob.status === 'done'
                  ? <>News backfill done: {newsJob.detected} researched, {newsJob.skipped} skipped{newsJob.errors ? `, ${newsJob.errors} errors` : ''}</>
                  : <>News backfill failed: {newsJob.error || 'unknown error'}</>}
            </span>
          )}
        </div>
      )}

      {!data ? <Spinner label="Loading…" /> : signals.length === 0 ? (
        <div className="empty">No cached signals yet. They populate as you generate batches.</div>
      ) : (
        <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
          {/* table-layout:fixed makes these 9 widths (summing to 100%) authoritative,
              so the actions column keeps room for both buttons and the long
              signal/tech/hiring/news text truncates instead of blowing the table
              wide. minWidth floors it so buttons never clip; the panel scrolls on
              a narrow window. */}
          <table className="dense" style={{ tableLayout: 'fixed', width: '100%', minWidth: 1220 }}>
            <thead><tr>
              <th style={{ width: '12%' }}>Domain</th>
              <th style={{ width: '9%' }}>Company</th>
              <th style={{ width: '5%' }}>Type</th>
              <th style={{ width: '15%' }}>Signal</th>
              <th style={{ width: '12%' }}>Tech</th>
              <th style={{ width: '13%' }}>Hiring</th>
              <th style={{ width: '15%' }}>News</th>
              <th style={{ width: '4%' }}>Age</th>
              <th style={{ width: '15%' }}></th>
            </tr></thead>
            <tbody>
              {signals.map((s) => (
                <tr key={s.domain} className="clickable" onClick={() => setOpenDomain(s.domain)}>
                  <td className="mono">
                    <span title={s.domain} style={{ display: 'inline-block', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom' }}>{s.domain}</span>
                  </td>
                  <td>
                    {s.company_name
                      ? <span title={s.company_name} style={{ display: 'inline-block', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom' }}>{s.company_name}</span>
                      : <span className="muted">—</span>}
                  </td>
                  <td>
                    {s.has_recent
                      ? <span className="badge" style={{ color: 'var(--green)', borderColor: 'var(--green)' }}>recent</span>
                      : <span className="badge" style={{ color: 'var(--amber)', borderColor: 'var(--amber)' }}>fallback</span>}
                  </td>
                  <td className="muted">
                    <span className="clamp2" title={s.signal}>{s.signal}</span>
                  </td>
                  <td>
                    {s.tech_signals && s.tech_signals !== NO_TECH ? (
                      <span className="muted" title={`${s.tech_signals}${s.tech_age_days != null ? ` (scanned ${s.tech_age_days}d ago)` : ''}`}
                        style={{ display: 'inline-block', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom' }}>
                        {s.tech_signals}
                      </span>
                    ) : s.tech_signals === NO_TECH ? (
                      <span className="muted" title={s.tech_age_days != null ? `scanned ${s.tech_age_days}d ago` : undefined}>none detected</span>
                    ) : s.tech_error ? (
                      <span className="badge" style={{ color: 'var(--red)', borderColor: 'var(--red)' }} title={s.tech_error}>scan failed</span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    {s.hiring_signals && s.hiring_signals !== NO_HIRING ? (
                      <span className="muted" title={`${s.hiring_signals}${s.hiring_age_days != null ? ` (checked ${s.hiring_age_days}d ago)` : ''}`}
                        style={{ display: 'inline-block', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom' }}>
                        {s.hiring_signals}
                      </span>
                    ) : s.hiring_signals === NO_HIRING ? (
                      <span className="muted" title={s.hiring_age_days != null ? `checked ${s.hiring_age_days}d ago` : undefined}>none detected</span>
                    ) : s.hiring_error ? (
                      <span className="badge" style={{ color: 'var(--red)', borderColor: 'var(--red)' }} title={s.hiring_error}>scan failed</span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    {s.news_signals && s.news_signals !== NO_NEWS ? (
                      <span className="muted" title={`${s.news_signals}${s.news_age_days != null ? ` (researched ${s.news_age_days}d ago)` : ''}`}
                        style={{ display: 'inline-block', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom' }}>
                        {s.news_signals}
                      </span>
                    ) : s.news_signals === NO_NEWS ? (
                      <span className="muted" title={s.news_age_days != null ? `researched ${s.news_age_days}d ago` : undefined}>none found</span>
                    ) : s.news_error ? (
                      <span className="badge" style={{ color: 'var(--red)', borderColor: 'var(--red)' }} title={s.news_error}>research failed</span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    <span style={{ color: s.fresh ? 'var(--muted)' : 'var(--red)' }}>
                      {s.age_days == null ? '—' : `${s.age_days}d`}
                    </span>
                  </td>
                  <td style={{ whiteSpace: 'nowrap' }} onClick={(e) => e.stopPropagation()}>
                    <button className="ghost sm" disabled={refreshing === s.domain} onClick={() => refresh(s.domain)}>
                      {refreshing === s.domain ? <Spinner /> : '↻ Refresh'}
                    </button>{' '}
                    <button className="ghost sm" disabled={techOff || detecting === s.domain} onClick={() => detect(s.domain)}
                      title={techOff ? (data?.tech_reason || 'detection unavailable') : 'Re-scan this company’s website + DNS'}>
                      {detecting === s.domain ? <Spinner /> : '⌁ Detect'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {openDomain && (
        <SignalDetail
          domain={openDomain}
          onClose={() => setOpenDomain(null)}
          onChanged={(d) => setData(d)}
        />
      )}
    </div>
  )
}
