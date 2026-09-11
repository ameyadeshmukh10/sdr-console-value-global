import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Badge, Spinner, ErrorBanner, num } from '../components/ui.jsx'
import OutreachDetail from '../components/OutreachDetail.jsx'

// Pillar 4 — Transparency + the outreach approval gate: browse/search the
// generated outreach, open any lead's full copy (now editable), and approve
// gated copy for enrollment — per row, in bulk for the selection, or all.
const PAGE_SIZE = 50

// Display names for the trigger segments (facet values are the raw ids).
const SEGMENT_LABELS = {
  ma_carveout: 'M&A carve-out', erp_migration: 'ERP migration',
  license_audit: 'License audit', ebs_oci: 'EBS on OCI',
  ebs_performance: 'EBS performance', hiring: 'Hiring (sales roles)',
  no_signals: 'No signals found', suppressed: 'Suppressed',
}

// Sortable column header (same affordance as the Signals table). The sort is
// applied server-side — the list is server-paginated, so a client sort would
// only reorder one page.
function SortTh({ label, k, sort, onSort }) {
  const active = sort.key === k
  return (
    <th style={{ whiteSpace: 'nowrap' }}
      aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : undefined}>
      <button type="button" className="th-sort" onClick={() => onSort(k)}
        title={`Sort by ${label.toLowerCase()}`}>
        {label}{active ? (sort.dir === 'asc' ? ' ▲' : ' ▼') : ''}
      </button>
    </th>
  )
}

export default function OutreachPage() {
  const [filters, setFilters] = useState({ persona: '', cta: '', status: '', segment: '', q: '', group_by: '', approved: '' })
  const [sort, setSort] = useState({ key: '', dir: 'asc' })
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [openId, setOpenId] = useState(null)
  const [selected, setSelected] = useState({})   // contact_id -> true
  const [approving, setApproving] = useState(null)
  const [approveNote, setApproveNote] = useState(null)
  const [reloadTick, setReloadTick] = useState(0)

  useEffect(() => {
    setLoading(true)
    const params = { ...filters, page, page_size: PAGE_SIZE }
    if (sort.key) { params.sort = sort.key; params.dir = sort.dir }
    Object.keys(params).forEach((k) => params[k] === '' && delete params[k])
    api.outreach(params)
      .then((d) => { setData(d); setError(null) })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [filters, sort, page, reloadTick])

  function setF(key, val) { setPage(1); setFilters((f) => ({ ...f, [key]: val })) }
  function toggleSort(k) {
    setPage(1)
    setSort((s) => (s.key === k ? { key: k, dir: s.dir === 'asc' ? 'desc' : 'asc' }
      : { key: k, dir: 'asc' }))
  }
  const reload = () => setReloadTick((t) => t + 1)

  const approvable = (r) => r.gated && r.status === 'generated' && !r.approved
  const pageApprovable = (data?.items || []).filter(approvable)
  const selIds = Object.keys(selected).filter((k) => selected[k])

  async function approve(body, key) {
    setApproving(key); setError(null); setApproveNote(null)
    try {
      const r = await api.approveOutreach(body)
      if (r.ok === false) setError(r.error || 'approval failed')
      else {
        setApproveNote(`Approved ${r.approved} contact${r.approved === 1 ? '' : 's'} for enrollment` +
          (r.review?.enroll_ready != null ? ` — ${r.review.enroll_ready} now ready to enroll (Pipeline tab).` : '.'))
        setSelected({})
        reload()
      }
    } catch (e) { setError(e.message) }
    finally { setApproving(null) }
  }

  const facets = data?.facets || {}
  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1

  const facetOptions = (name) =>
    Object.entries(facets[name] || {}).map(([k, n]) => (
      <option key={k} value={k}>{k} ({n})</option>
    ))

  return (
    <div>
      <h1 className="page-title">Outreach</h1>
      <p className="page-sub">Every generated sequence, joined with contact + enrollment status. Click a row for the full copy.</p>

      <div className="toolbar">
        <label className="field grow">Search
          <input placeholder="name, email, company, signal…" value={filters.q}
            onChange={(e) => setF('q', e.target.value)} />
        </label>
        <label className="field">Persona
          <select value={filters.persona} onChange={(e) => setF('persona', e.target.value)}>
            <option value="">All</option>{facetOptions('persona')}
          </select>
        </label>
        <label className="field">CTA play
          <select value={filters.cta} onChange={(e) => setF('cta', e.target.value)}>
            <option value="">All</option>{facetOptions('cta_type')}
          </select>
        </label>
        <label className="field">Trigger
          <select value={filters.segment} onChange={(e) => setF('segment', e.target.value)}>
            <option value="">All</option>
            {Object.entries(facets.segment || {}).filter(([k]) => k).map(([k, n]) => (
              <option key={k} value={k}>{SEGMENT_LABELS[k] || k} ({n})</option>
            ))}
          </select>
        </label>
        <label className="field">Status
          <select value={filters.status} onChange={(e) => setF('status', e.target.value)}>
            <option value="">All</option>{facetOptions('status')}
          </select>
        </label>
        <label className="field">Approval
          <select value={filters.approved} onChange={(e) => setF('approved', e.target.value)}>
            <option value="">All</option>
            <option value="no">Awaiting approval</option>
            <option value="yes">Approved</option>
          </select>
        </label>
        <label className="field">Group by
          <select value={filters.group_by} onChange={(e) => setF('group_by', e.target.value)}>
            <option value="">— none —</option>
            <option value="persona">Persona</option>
            <option value="cta_type">CTA play</option>
            <option value="segment">Trigger</option>
            <option value="status">Status</option>
            <option value="company">Company</option>
          </select>
        </label>
      </div>

      <ErrorBanner error={error} />
      {approveNote && <div className="banner info" style={{ marginBottom: 12 }}>{approveNote}</div>}

      {(selIds.length > 0 || pageApprovable.length > 0) && (
        <div className="row" style={{ gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
          {selIds.length > 0 && (
            <button className="sm" disabled={!!approving}
              onClick={() => approve({ contact_ids: selIds }, 'sel')}>
              {approving === 'sel' ? <Spinner /> : `✓ Approve selected (${selIds.length})`}
            </button>
          )}
          <button className="ghost sm" disabled={!!approving}
            title="Approves every gated generated contact still awaiting approval"
            onClick={() => approve({ all: true }, 'all')}>
            {approving === 'all' ? <Spinner /> : '✓ Approve all awaiting'}
          </button>
        </div>
      )}

      {data?.groups && (
        <div className="panel" style={{ marginBottom: 18 }}>
          <div className="section-h" style={{ margin: '0 0 12px' }}>Grouped by {filters.group_by} — {Object.keys(data.groups).length} groups</div>
          <div className="row" style={{ flexWrap: 'wrap', gap: 8 }}>
            {Object.entries(data.groups).slice(0, 40).map(([k, n]) => (
              <span key={k} className="badge">{k || '—'}: {n}</span>
            ))}
          </div>
        </div>
      )}

      <div className="row between" style={{ marginBottom: 10 }}>
        <span className="muted">{loading ? <Spinner /> : `${num(data?.total || 0)} sequences`}</span>
      </div>

      <div className="panel" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: 34 }}>
                {pageApprovable.length > 0 && (
                  <input type="checkbox" style={{ width: 'auto' }}
                    checked={pageApprovable.every((r) => selected[r.contact_id])}
                    onChange={(e) => {
                      const on = e.target.checked
                      setSelected((m) => {
                        const next = { ...m }
                        pageApprovable.forEach((r) => { next[r.contact_id] = on })
                        return next
                      })
                    }} />
                )}
              </th>
              <SortTh label="Name" k="name" sort={sort} onSort={toggleSort} />
              <SortTh label="Company" k="company" sort={sort} onSort={toggleSort} />
              <SortTh label="Persona" k="persona" sort={sort} onSort={toggleSort} />
              <SortTh label="CTA play" k="cta_type" sort={sort} onSort={toggleSort} />
              <SortTh label="Trigger" k="segment" sort={sort} onSort={toggleSort} />
              <SortTh label="Status" k="status" sort={sort} onSort={toggleSort} />
              <th>Approval</th><th>Signal</th>
            </tr>
          </thead>
          <tbody>
            {data?.items?.map((r) => (
              <tr key={r.contact_id} className="clickable" onClick={() => setOpenId(r.contact_id)}>
                <td onClick={(e) => e.stopPropagation()}>
                  {approvable(r) && (
                    <input type="checkbox" style={{ width: 'auto' }} checked={!!selected[r.contact_id]}
                      onChange={(e) => setSelected((m) => ({ ...m, [r.contact_id]: e.target.checked }))} />
                  )}
                </td>
                <td>{r.first_name} {r.last_name}{r.edited && <span className="badge muted" style={{ marginLeft: 6 }}>edited</span>}</td>
                <td>{r.company || <span className="muted">—</span>}</td>
                <td><Badge kind="persona" value={r.persona} /></td>
                <td><span className="badge cta">{r.cta_type}</span></td>
                <td>
                  {r.segment
                    ? <span className="badge" style={{ color: 'var(--jade)', borderColor: 'var(--jade)' }}>{r.segment_label || r.segment}</span>
                    : <span className="muted">—</span>}
                </td>
                <td><Badge kind="status" value={r.status} /></td>
                <td>
                  {!r.gated ? <span className="muted" title="autonomous (SLA) — no approval gate">auto</span>
                    : r.approved ? <span className="badge" style={{ color: 'var(--green)', borderColor: 'var(--green)' }}>✓ approved</span>
                      : r.status === 'generated' ? <span className="badge" style={{ color: 'var(--amber)', borderColor: 'var(--amber)' }}>awaiting</span>
                        : <span className="muted">—</span>}
                </td>
                <td className="muted" style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.signal}</td>
              </tr>
            ))}
            {data && data.items.length === 0 && !loading && (
              <tr><td colSpan={9}><div className="empty">No matching sequences.</div></td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="pager">
        <button className="ghost sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>← Prev</button>
        <span className="muted">Page {page} / {totalPages}</span>
        <button className="ghost sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>Next →</button>
      </div>

      {openId && <OutreachDetail id={openId} onClose={() => setOpenId(null)} onChanged={reload} />}
    </div>
  )
}
