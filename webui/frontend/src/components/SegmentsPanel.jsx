import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'

// Stage 2-4 of the gated flow: after a list pull / CSV upload, signal
// intelligence (tech + hiring + the 5 ERP news triggers) researches the pulled
// ACCOUNTS, this panel shows them grouped by the signals found, and the user
// approves segments (or everything) — which batches the contacts and
// auto-starts copy generation. SLA-sourced contacts never appear here.
// Self-contained: loads /api/segments, polls while intel runs, and hands the
// parent the generation job id via onGeneration(jobId).
const POLL_MS = 4000

const STAGE_LABELS = { tech: 'Tech', hiring: 'Hiring', news: 'ERP news' }

export default function SegmentsPanel({ onChanged, onGeneration }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(null) // segment id | 'all' | 'intel'
  const [selected, setSelected] = useState({}) // segment id -> true
  const [openSeg, setOpenSeg] = useState(null)
  const [result, setResult] = useState(null)

  const load = useCallback(() => {
    api.segments().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }, [])
  useEffect(() => { load() }, [load])

  const intelRunning = data?.intel?.running
  useEffect(() => {
    if (!intelRunning) return
    const t = setInterval(load, POLL_MS)
    return () => clearInterval(t)
  }, [intelRunning, load])

  async function approve(body, key) {
    setBusy(key); setError(null); setResult(null)
    try {
      const r = await api.approveSegments(body)
      if (r.ok === false) setError(r.error || 'approval failed')
      else {
        setResult(r)
        setSelected({})
        if (r.generation?.job_id && onGeneration) onGeneration(r.generation.job_id)
        onChanged?.()
      }
      load()
    } catch (e) { setError(e.message) }
    finally { setBusy(null) }
  }

  async function runIntel() {
    setBusy('intel'); setError(null)
    try {
      const r = await api.intelRun()
      if (r.ok === false) setError(r.error || 'could not start signal intelligence')
      load()
    } catch (e) { setError(e.message) }
    finally { setBusy(null) }
  }

  if (!data) return null
  const segs = (data.segments || []).filter((s) => s.accounts.length > 0)
  const pending = data.pending || []
  const nothing = data.accounts === 0
  if (nothing && !intelRunning && !result) return null

  const selIds = Object.keys(selected).filter((k) => selected[k])
  const selAccounts = new Set()
  segs.filter((s) => selected[s.id]).forEach((s) => s.accounts.forEach((a) => selAccounts.add(a.domain)))
  const job = data.intel?.job
  const stage = job?.stage ? job.stages?.[job.stage] : null

  return (
    <div className="panel feature" style={{ marginBottom: 22 }}>
      <div className="row between" style={{ marginBottom: 8 }}>
        <span className="section-h" style={{ margin: 0 }}>1 · Signal segments — approve accounts for outreach</span>
        <span className="muted" style={{ fontSize: 13 }}>
          {num(data.accounts)} accounts · {num(data.contacts)} contacts awaiting approval
        </span>
      </div>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        Signal intelligence researched these accounts first. Approve the segments worth pursuing —
        their contacts are batched and copy generation starts automatically (ERP-trigger segments
        write trigger-anchored copy). Unapproved accounts are never generated or billed.
      </p>

      <ErrorBanner error={error} />

      {intelRunning && (
        <div className="banner info" style={{ marginBottom: 12 }}>
          <span className="row" style={{ gap: 8 }}>
            <span className="spinner" />
            Researching signals{job?.stage ? ` — ${STAGE_LABELS[job.stage] || job.stage}` : ''}
            {stage ? ` ${stage.done}/${stage.total}` : ''} · {num(data.researched)}/{num(data.accounts)} accounts researched
          </span>
        </div>
      )}
      {!intelRunning && pending.length > 0 && (
        <div className="row" style={{ gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
          <span className="muted" style={{ fontSize: 13 }}>
            {num(pending.length)} account{pending.length === 1 ? '' : 's'} not researched yet
            {pending.some((p) => p.news_error) ? ' (some scans failed — re-run retries them)' : ''}.
          </span>
          <button className="ghost sm" disabled={busy === 'intel'} onClick={runIntel}>
            {busy === 'intel' ? <Spinner /> : '⌕ Run signal intelligence'}
          </button>
        </div>
      )}

      {result && (
        <div className="banner info" style={{ marginBottom: 12 }}>
          Approved <b>{num(result.approved_accounts)}</b> accounts ({num(result.approved_contacts)} contacts)
          into <b>{num(result.new_batches)}</b> batch{result.new_batches === 1 ? '' : 'es'}.
          {result.generation?.job_id
            ? <> Copy generation started (job <span className="mono">{result.generation.job_id}</span>).</>
            : result.generation?.error
              ? <> Generation not auto-started: {result.generation.error} — use the batch buttons below.</>
              : null}
        </div>
      )}

      {segs.length > 0 && (
        <>
          <table className="dense" style={{ marginBottom: 12 }}>
            <thead><tr>
              <th style={{ width: 30 }}></th><th>Segment</th><th>Accounts</th><th>Contacts</th><th></th><th></th>
            </tr></thead>
            <tbody>
              {segs.map((s) => (
                <tr key={s.id}>
                  <td onClick={(e) => e.stopPropagation()}>
                    {s.id !== 'suppressed' && (
                      <input type="checkbox" checked={!!selected[s.id]} style={{ width: 'auto' }}
                        onChange={(e) => setSelected((m) => ({ ...m, [s.id]: e.target.checked }))} />
                    )}
                  </td>
                  <td>
                    <b>{s.label}</b>
                    {s.kind === 'trigger' && <span className="badge cta" style={{ marginLeft: 8 }}>trigger copy</span>}
                    {s.id === 'suppressed' && <span className="badge danger" style={{ marginLeft: 8 }}>do not contact</span>}
                  </td>
                  <td>{num(s.accounts.length)}</td>
                  <td className="muted">{num(s.contact_total)}</td>
                  <td>
                    <button className="ghost sm" onClick={() => setOpenSeg(openSeg === s.id ? null : s.id)}>
                      {openSeg === s.id ? 'Hide accounts' : 'Show accounts'}
                    </button>
                  </td>
                  <td>
                    {s.id !== 'suppressed' && (
                      <button className="sm" disabled={!!busy} onClick={() => approve({ segments: [s.id] }, s.id)}>
                        {busy === s.id ? <Spinner /> : 'Approve segment'}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {openSeg && (() => {
            const s = segs.find((x) => x.id === openSeg)
            if (!s) return null
            return (
              <div className="panel" style={{ padding: 0, marginBottom: 12, maxHeight: 300, overflow: 'auto' }}>
                <table className="dense">
                  <thead><tr><th>Account</th><th>Contacts</th>{s.kind === 'trigger' && <th>Score</th>}<th>Signal</th></tr></thead>
                  <tbody>
                    {s.accounts.map((a) => (
                      <tr key={a.domain}>
                        <td>
                          <span className="mono">{a.domain}</span>
                          {a.company && <span className="muted" style={{ marginLeft: 8 }}>{a.company}</span>}
                          {a.flags && Object.entries(a.flags).map(([f, n]) => (
                            <span key={f} className="badge muted" style={{ marginLeft: 6, fontSize: 10 }}
                              title="import-time quality flag">{n > 1 ? `${n}× ` : ''}{f.replace(/_/g, ' ')}</span>
                          ))}
                          {a.suppression_review && (
                            <span className="badge danger" style={{ marginLeft: 6, fontSize: 10 }}
                              title="Close to a do-not-contact rule — check before approving">
                              near-miss: {a.suppression_review.rule}
                            </span>
                          )}
                        </td>
                        <td>{num(a.contacts)}</td>
                        {s.kind === 'trigger' && <td>{a.score ?? '—'}</td>}
                        <td className="muted" style={{ maxWidth: 420, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                          title={a.headline}>{a.headline || a.tech || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          })()}

          <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
            {selIds.length > 0 && (
              <button disabled={!!busy} onClick={() => approve({ segments: selIds }, 'sel')}>
                {busy === 'sel' ? <Spinner /> : `Approve ${selIds.length} selected segment${selIds.length === 1 ? '' : 's'} (${num(selAccounts.size)} accounts)`}
              </button>
            )}
            <button className="ghost" disabled={!!busy} onClick={() => approve({ all: true }, 'all')}
              title="Approves every awaiting account, researched or not (unresearched ones use the default copy path). Suppressed accounts are always excluded.">
              {busy === 'all' ? <Spinner /> : `Approve all (${num(Math.max(0, data.accounts - (segs.find((x) => x.id === 'suppressed')?.accounts.length || 0)))} accounts)`}
            </button>
          </div>
        </>
      )}
      {segs.length === 0 && !intelRunning && pending.length === 0 && !nothing && (
        <div className="empty">No researched accounts to segment yet.</div>
      )}
    </div>
  )
}
