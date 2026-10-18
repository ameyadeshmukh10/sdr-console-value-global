import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num } from '../components/ui.jsx'
import { SortTh, useSort } from '../components/sortable.jsx'
import SignalDetail from '../components/SignalDetail.jsx'
import { buildSignalReport } from '../components/signalReport.js'

// Signal cache — per-company research reused for 90 days so a company is searched
// once instead of once per contact / per re-run. Force-refresh re-searches one.
// Tech = the technographic scan (website + DNS fingerprinting + ERP portal probes) stored alongside:
// Refresh / Detect act on the checked rows (one domain at a time); "Detect missing" backfills the rest.
// Hiring = the Prospeo job-postings scan (open roles + sales subset), same cache;
// single-domain detect lives in the row drawer, "Detect hiring" backfills the rest.
// News = the ERP-trigger web research (M&A carve-out, ERP migration, license audit,
// EBS-on-OCI, EBS performance), same cache on a 30-day window; single-domain
// research lives in the row drawer, "Research news" backfills the rest.
const NO_TECH = 'No signals detected'
const NO_HIRING = 'No open roles detected'
const NO_NEWS = 'No ERP news signals detected'
const SEARCH_MIN = 3

// The text each table column displays for a row (mirrors the cell rendering
// below), so the search matches what the operator actually sees.
function rowSearchText(s) {
  const cell = (val, none, failed, err) =>
    val && val !== none ? val : val === none ? failed[0] : err ? failed[1] : ''
  return [
    s.domain,
    s.company_name,
    s.trigger_label || (s.news_age_days != null ? 'none' : 'not researched'),
    s.signal,
    cell(s.tech_signals, NO_TECH, ['no ERP detected', 'scan failed'], s.tech_error),
    cell(s.hiring_signals, NO_HIRING, ['none detected', 'scan failed'], s.hiring_error),
    cell(s.news_signals, NO_NEWS, ['none found', 'research failed'], s.news_error),
    s.age_days == null ? '' : `${s.age_days}d`,
  ].join('\n').toLowerCase()
}

export default function SignalsPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(() => new Set())
  // Bulk action over the checked rows: {action, total, done, current, errors, running}.
  const [selJob, setSelJob] = useState(null)
  const stopSel = useRef(false)
  const [exporting, setExporting] = useState(false)
  const [bulkJob, setBulkJob] = useState(null)
  const [hiringJob, setHiringJob] = useState(null)
  const [newsJob, setNewsJob] = useState(null)
  const [openDomain, setOpenDomain] = useState(null)
  const [query, setQuery] = useState('')

  function load() {
    api.signals().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }
  useEffect(() => { load() }, [])
  // Leaving the page stops a running selected-rows queue after its in-flight call
  // (otherwise it keeps firing live research/scans with no visible Stop control).
  useEffect(() => () => { stopSel.current = true }, [])

  // Runs the per-domain endpoint over every checked row, sequentially (each call
  // is a live research/scan — no point hammering the server in parallel). Each
  // response is the full signals payload, so the table updates as rows land.
  async function runSelected(action) {
    const domains = [...selected]
    if (!domains.length) return
    const call = action === 'refresh'
      ? (d) => api.refreshSignal(d)
      : (d) => api.detectTech(d, true)
    stopSel.current = false
    setError(null)
    let errors = 0, lastErr = null
    for (let i = 0; i < domains.length; i++) {
      if (stopSel.current) break
      setSelJob({ action, total: domains.length, done: i, current: domains[i], errors, running: true })
      try {
        const d = await call(domains[i])
        if (d.ok === false) { errors++; lastErr = `${domains[i]}: ${d.error || `${action} failed`}` }
        else {
          setData(d)
          // A failed tech scan still returns ok:true — the failure rides in detected.tech_error.
          const scanErr = action === 'detect' && !d.detected?.skipped && d.detected?.tech_error
          if (scanErr) { errors++; lastErr = `${domains[i]}: ${scanErr}` }
        }
      } catch (e) { errors++; lastErr = `${domains[i]}: ${e.message}` }
      setSelJob((j) => ({ ...j, done: i + 1, errors }))
    }
    setSelJob((j) => ({ ...j, current: null, running: false, stopped: stopSel.current }))
    if (lastErr) setError(lastErr)
  }

  // Export: per checked domain, everything the drawer shows (buildSignalReport —
  // same wording/sections) plus the raw DB row, as one JSON download. Detail reads
  // are cheap DB queries, fetched a few at a time; a failed read falls back to
  // the table row so nothing checked is silently dropped.
  async function exportSelected() {
    const domains = [...selected]
    if (!domains.length) return
    setExporting(true); setError(null)
    try {
      const byDomain = new Map(signals.map((s) => [s.domain, s]))
      const out = new Array(domains.length)
      let next = 0
      async function worker() {
        while (next < domains.length) {
          const i = next++
          const domain = domains[i]
          try {
            const d = await api.signalDetail(domain)
            out[i] = d.ok === false
              ? { domain, error: d.error, contacts: d.contacts || [], raw: byDomain.get(domain) || null }
              : buildSignalReport(d)
          } catch (e) {
            out[i] = { domain, error: e.message, contacts: [], raw: byDomain.get(domain) || null }
          }
        }
      }
      await Promise.all(Array.from({ length: Math.min(6, domains.length) }, worker))
      const payload = { exported_at: new Date().toISOString(), count: out.length, signals: out }
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `signals-${new Date().toISOString().slice(0, 10)}-${out.length}.json`
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) { setError(e.message) }
    finally { setExporting(false) }
  }

  function toggleOne(domain) {
    setSelected((prev) => {
      const next = new Set(prev)
      next.has(domain) ? next.delete(domain) : next.add(domain)
      return next
    })
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

  const signals = useMemo(() => data?.signals || [], [data])
  // Client-side search over every shown column, active from SEARCH_MIN chars.
  const q = query.trim().toLowerCase()
  const searching = q.length >= SEARCH_MIN
  const filtered = useMemo(
    () => (searching ? signals.filter((s) => rowSearchText(s).includes(q)) : signals),
    [signals, searching, q])
  // Client-side sort (the list is returned unpaginated); click cycles
  // ascending → descending → server order, blanks always last.
  const { rows: sorted, sort, onSort: toggleSort } = useSort(filtered)
  const fresh = signals.filter((s) => s.fresh).length
  const withTrigger = signals.filter((s) => s.trigger).length
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
  const selRunning = !!selJob?.running
  const allChecked = sorted.length > 0 && sorted.every((s) => selected.has(s.domain))
  const someChecked = !allChecked && sorted.some((s) => selected.has(s.domain))
  function toggleAll() {
    setSelected(allChecked ? new Set() : new Set(sorted.map((s) => s.domain)))
  }

  return (
    <div>
      <h1 className="page-title">Signals</h1>
      <p className="page-sub">Per-company research cache. Fresh entries (&lt;90 days) are reused, so the AI SDR skips the web search. Tech = detected stack from a website + DNS scan. Hiring = open roles from a live job-postings lookup (sales roles feed email 2). News = web-researched ERP triggers (M&amp;A carve-out, ERP migration, license audit, EBS on OCI, EBS performance; 30-day cache).</p>

      <ErrorBanner error={error} />

      <div className="stat-row" style={{ marginBottom: 20 }}>
        <Stat label="Cached accounts" value={num(data?.count || 0)} />
        <Stat label="Fresh (<90d)" value={num(fresh)} sub="reused, no re-search" />
        <Stat label="ERP trigger found" value={num(withTrigger)} tone="good" />
        <Stat label="No trigger" value={num(newsScanned - withTrigger)} sub="researched, none found" tone="warn" />
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
        <>
        {/* One toolbar: search on the left; counts, bulk actions on the checked
            rows and their job progress on the right. */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 10, flexWrap: 'wrap' }}>
          <input type="search" placeholder="Search all columns (3+ characters)…" value={query}
            onChange={(e) => setQuery(e.target.value)} aria-label="Search signals"
            style={{ flex: '0 1 360px', minWidth: 200 }} />
          {!searching && q.length > 0 && (
            <span className="muted" style={{ fontSize: 13 }}>Type at least {SEARCH_MIN} characters</span>
          )}
          <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 13 }}>
              <strong>{num(signals.length)}</strong> <span className="muted">signals</span>
              {searching && <> · <strong>{num(sorted.length)}</strong> <span className="muted">filtered</span></>}
              {selected.size > 0 && <> · <strong>{num(selected.size)}</strong> <span className="muted">checked</span></>}
            </span>
            {selected.size > 0 && (
              <>
                <button className="ghost sm" disabled={selRunning} onClick={() => runSelected('refresh')}
                  title="Re-research the signal for each checked company">↻ Refresh</button>
                <button className="ghost sm" disabled={selRunning || techOff} onClick={() => runSelected('detect')}
                  title={techOff ? (data?.tech_reason || 'detection unavailable') : 'Re-scan website + DNS for each checked company'}>⌁ Detect</button>
                <button className="ghost sm" disabled={exporting} onClick={exportSelected}
                  title="Download the full signal record of each checked company as JSON">
                  {exporting ? <Spinner /> : '⤓ Export'}
                </button>
                {!selRunning && <button className="ghost sm" onClick={() => setSelected(new Set())}
                  title="Uncheck all selected companies">Uncheck</button>}
              </>
            )}
            {selJob && (
              <span className="muted" style={{ fontSize: 13 }}>
                {selRunning
                  ? <><Spinner /> {selJob.action === 'refresh' ? 'Refreshing' : 'Detecting'}… {selJob.done}/{selJob.total}{selJob.current ? ` (${selJob.current})` : ''}{selJob.errors ? ` · ${selJob.errors} errors` : ''}</>
                  : <>{selJob.action === 'refresh' ? 'Refresh' : 'Detect'} {selJob.stopped ? 'stopped' : 'done'}: {selJob.done}/{selJob.total}{selJob.errors ? `, ${selJob.errors} errors` : ''}</>}
              </span>
            )}
            {selRunning && <button className="ghost sm" onClick={() => { stopSel.current = true }}>Stop</button>}
          </div>
        </div>
        <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
          {/* table-layout:fixed makes these widths authoritative, so the long
              signal/tech/hiring/news text truncates instead of blowing the table
              wide. The table fits the viewport; minWidth only floors it on very
              narrow windows, where the panel (not the page) scrolls. News has no
              width so it absorbs the leftover space — otherwise fixed layout
              spreads it across every column, the 36px checkbox one included. */}
          <table className="dense" style={{ tableLayout: 'fixed', width: '100%', minWidth: 860 }}>
            <thead><tr>
              <th style={{ width: 36 }} onClick={(e) => e.stopPropagation()}>
                <input type="checkbox" style={{ width: 'auto' }} checked={allChecked}
                  ref={(el) => { if (el) el.indeterminate = someChecked }}
                  onChange={toggleAll} aria-label="Select all" />
              </th>
              <SortTh label="Domain" k="domain" sort={sort} onSort={toggleSort} width="13%" />
              <SortTh label="Company" k="company_name" sort={sort} onSort={toggleSort} width="11%" />
              <SortTh label="Trigger" k="trigger_label" sort={sort} onSort={toggleSort} width="8%" />
              <SortTh label="Signal" k="signal" sort={sort} onSort={toggleSort} width="13%" />
              <SortTh label="Tech" k="tech_signals" sort={sort} onSort={toggleSort} width="12%" />
              <SortTh label="Hiring" k="hiring_signals" sort={sort} onSort={toggleSort} width="10%" />
              <SortTh label="News" k="news_signals" sort={sort} onSort={toggleSort} />
              <SortTh label="Age" k="age_days" sort={sort} onSort={toggleSort} width="5%" />
            </tr></thead>
            <tbody>
              {sorted.map((s) => (
                <tr key={s.domain} className="clickable" onClick={() => setOpenDomain(s.domain)}>
                  <td onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" style={{ width: 'auto' }} checked={selected.has(s.domain)}
                      onChange={() => toggleOne(s.domain)} aria-label={`Select ${s.domain}`} />
                  </td>
                  <td className="mono">
                    <span title={s.domain} style={{ display: 'inline-block', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom' }}>{s.domain}</span>
                  </td>
                  <td>
                    {s.company_name
                      ? <span title={s.company_name} style={{ display: 'inline-block', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom' }}>{s.company_name}</span>
                      : <span className="muted">—</span>}
                  </td>
                  <td>
                    {s.trigger_label
                      ? <span className="badge" title={s.trigger_label}
                          style={{ color: 'var(--green)', borderColor: 'var(--green)', display: 'inline-block', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom' }}>
                          {s.trigger_label}
                        </span>
                      : s.news_age_days != null
                        ? <span className="muted" title="News research ran, no ERP trigger found">none</span>
                        : <span className="muted" title="News research hasn't run for this account yet">not researched</span>}
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
                      <span className="muted" title={s.tech_age_days != null ? `scanned ${s.tech_age_days}d ago` : undefined}>no ERP detected</span>
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
                    <span style={{ color: s.fresh ? 'var(--muted)' : 'var(--red)' }}
                      title="Days since the last research/scan activity for this account">
                      {s.age_days == null ? '—' : `${s.age_days}d`}
                    </span>
                  </td>
                </tr>
              ))}
              {sorted.length === 0 && (
                <tr><td colSpan={9} className="muted" style={{ textAlign: 'center', padding: 24 }}>
                  No companies match “{query.trim()}”.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
        </>
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
