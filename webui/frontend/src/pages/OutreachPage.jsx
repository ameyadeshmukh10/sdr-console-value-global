import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Badge, Spinner, ErrorBanner, FlagChips, flagLabel, num, Truncate } from '../components/ui.jsx'
import { SortTh, nextSort } from '../components/sortable.jsx'
import OutreachDetail from '../components/OutreachDetail.jsx'

// Pillar 4 — Transparency + the outreach approval gate: browse/search the
// generated outreach, open any lead's full copy (now editable), and approve
// gated copy for enrollment — per row, in bulk for the selection, or all.
// Lead-quality flags (outside US/CA, <1,000 employees, healthcare) are a
// column + filter so off-ICP contacts can be found and un-approved before
// enrollment.
const PAGE_SIZE = 50

// Display names for the trigger segments (facet values are the raw ids).
const SEGMENT_LABELS = {
  ma_carveout: 'M&A carve-out', erp_migration: 'ERP migration',
  license_audit: 'License audit', ebs_oci: 'EBS on OCI',
  ebs_performance: 'EBS performance', hiring: 'Hiring (sales roles)',
  no_signals: 'No signals found', suppressed: 'Suppressed',
}

export default function OutreachPage() {
  const [filters, setFilters] = useState({ persona: '', cta: '', status: '', segment: '', q: '', group_by: '', approved: '', flag: '' })
  // Sorted server-side — the list is server-paginated, so a client sort would
  // only reorder one page. null = the server's default order.
  const [sort, setSort] = useState(null)
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
    if (sort) { params.sort = sort.key; params.dir = sort.dir }
    Object.keys(params).forEach((k) => params[k] === '' && delete params[k])
    api.outreach(params)
      .then((d) => { setData(d); setError(null) })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [filters, sort, page, reloadTick])

  function setF(key, val) { setPage(1); setFilters((f) => ({ ...f, [key]: val })) }
  function toggleSort(k) {
    setPage(1)
    setSort((s) => nextSort(s, k))
  }
  const reload = () => setReloadTick((t) => t + 1)

  const approvable = (r) => r.gated && r.status === 'generated' && !r.approved
  // selectable = still at the outreach gate, approved or not (approved rows can
  // be un-approved until they enroll)
  const selectable = (r) => r.gated && r.status === 'generated'
  const pageSelectable = (data?.items || []).filter(selectable)
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

  async function unapprove(ids) {
    setApproving('unsel'); setError(null); setApproveNote(null)
    try {
      const r = await api.unapproveOutreach({ contact_ids: ids })
      if (r.ok === false) setError(r.error || 'un-approve failed')
      else {
        setApproveNote(`Un-approved ${r.unapproved} contact${r.unapproved === 1 ? '' : 's'} — they won't enroll until re-approved` +
          (r.review?.enroll_ready != null ? ` (${r.review.enroll_ready} now ready to enroll).` : '.'))
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
        <label className="field">Lead flags
          <select value={filters.flag} onChange={(e) => setF('flag', e.target.value)}>
            <option value="">All</option>
            <option value="off_icp">Off-ICP (any) ({facets.flags?.off_icp || 0})</option>
            <option value="none">No flags</option>
            {Object.entries(facets.flags || {}).filter(([k]) => k !== 'off_icp').map(([k, n]) => (
              <option key={k} value={k}>{flagLabel(k)} ({n})</option>
            ))}
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
          {selIds.length > 0 && (
            <button className="ghost sm" disabled={!!approving}
              title="Clear approval so these contacts are NOT enrolled (enrolled contacts are unaffected)"
              onClick={() => unapprove(selIds)}>
              {approving === 'unsel' ? <Spinner /> : `✕ Un-approve selected (${selIds.length})`}
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

      <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: 34 }}>
                {pageSelectable.length > 0 && (
                  <input type="checkbox" style={{ width: 'auto' }}
                    checked={pageSelectable.every((r) => selected[r.contact_id])}
                    onChange={(e) => {
                      const on = e.target.checked
                      setSelected((m) => {
                        const next = { ...m }
                        pageSelectable.forEach((r) => { next[r.contact_id] = on })
                        return next
                      })
                    }} />
                )}
              </th>
              <SortTh label="Name" k="name" sort={sort} onSort={toggleSort} />
              <SortTh label="Company" k="company" sort={sort} onSort={toggleSort} />
              {/* persona / CTA play / status stacked in one column; sorts by status
                  (persona and CTA play have their own filters above) */}
              <SortTh label="Persona · CTA · Status" k="status" sort={sort} onSort={toggleSort} />
              <SortTh label="Trigger" k="segment" sort={sort} onSort={toggleSort} />
              <SortTh label="Approval" k="approval" sort={sort} onSort={toggleSort} />
              <SortTh label="Lead flags" k="flags" sort={sort} onSort={toggleSort} />
              <SortTh label="Signal" k="signal" sort={sort} onSort={toggleSort} />
            </tr>
          </thead>
          <tbody>
            {data?.items?.map((r) => (
              <tr key={r.contact_id} className="clickable" onClick={() => setOpenId(r.contact_id)}>
                <td onClick={(e) => e.stopPropagation()}>
                  {selectable(r) && (
                    <input type="checkbox" style={{ width: 'auto' }} checked={!!selected[r.contact_id]}
                      onChange={(e) => setSelected((m) => ({ ...m, [r.contact_id]: e.target.checked }))} />
                  )}
                </td>
                <td>{r.first_name} {r.last_name}{r.edited && <span className="badge muted" style={{ marginLeft: 6 }}>edited</span>}</td>
                <td>{r.company || <span className="muted">—</span>}</td>
                <td>
                  <div className="badge-stack">
                    <Badge kind="persona" value={r.persona} />
                    <span className="badge cta">{r.cta_type}</span>
                    <Badge kind="status" value={r.status} />
                  </div>
                </td>
                <td>
                  {r.segment
                    ? <span className="badge" style={{ color: 'var(--jade)', borderColor: 'var(--jade)' }}>{r.segment_label || r.segment}</span>
                    : <span className="muted">—</span>}
                </td>
                <td>
                  {!r.gated ? <span className="muted" title="autonomous (SLA) — no approval gate">auto</span>
                    : r.approved ? <span className="badge" style={{ color: 'var(--green)', borderColor: 'var(--green)' }}>✓ approved</span>
                      : r.status === 'generated' ? <span className="badge" style={{ color: 'var(--amber)', borderColor: 'var(--amber)' }}>awaiting</span>
                        : <span className="muted">—</span>}
                </td>
                <td><FlagChips flags={r.flags} country={r.country} /></td>
                <td className="muted"><Truncate text={r.signal} max={320} lines={3} /></td>
              </tr>
            ))}
            {data && data.items.length === 0 && !loading && (
              <tr><td colSpan={8}><div className="empty">No matching sequences.</div></td></tr>
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
