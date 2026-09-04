import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'

// CSV Upload — the third way to feed the AI SDR (next to a CRM list and SLAs):
// upload a CSV of contacts (first/last name, job title, email, LinkedIn URL,
// country, company name/website/industry/size — flexible header spellings), name
// the audience, and it lands in the pipeline as ready-to-generate batches. The
// table below lists every uploaded audience with live pipeline status; expanding
// one shows its contacts. Generation + enrollment then work exactly as for
// pulled contacts (Pipeline tab).

const fmtWhen = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—')
const STATUS_ORDER = ['pending', 'generated', 'enrolled', 'skipped', 'failed']

function StatusChips({ counts }) {
  const entries = STATUS_ORDER.filter((s) => counts?.[s]).map((s) => [s, counts[s]])
  if (!entries.length) return <span className="muted">—</span>
  return (
    <span className="row" style={{ gap: 4, flexWrap: 'wrap' }}>
      {entries.map(([s, n]) => (
        <span key={s} className={`badge status-${s}`} style={{ fontSize: 10.5 }}>{num(n)} {s}</span>
      ))}
    </span>
  )
}

function SkippedNote({ counts }) {
  if (!counts) return null
  const bits = []
  if (counts.already_in_pipeline) bits.push(`${num(counts.already_in_pipeline)} already in the pipeline`)
  if (counts.no_email) bits.push(`${num(counts.no_email)} without a valid email`)
  if (counts.duplicate_in_file) bits.push(`${num(counts.duplicate_in_file)} duplicated in the file`)
  if (counts.persona_defaulted) bits.push(`${num(counts.persona_defaulted)} with an unmatched title routed to sales-leadership`)
  if (!bits.length) return null
  return <span className="muted"> Skipped/adjusted: {bits.join(' · ')}.</span>
}

function AudienceContacts({ audienceId }) {
  const [detail, setDetail] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => {
    let live = true
    api.audienceDetail(audienceId)
      .then((d) => { if (live) setDetail(d) })
      .catch((e) => { if (live) setError(e.message) })
    return () => { live = false }
  }, [audienceId])
  if (error) return <div className="banner error" style={{ margin: 8 }}>{error}</div>
  if (!detail) return <div style={{ padding: 10 }}><Spinner label="Loading contacts…" /></div>
  const rows = detail.contacts || []
  return (
    <div style={{ maxHeight: 320, overflow: 'auto' }}>
      <table className="dense">
        <thead><tr><th>Name</th><th>Title</th><th>Company</th><th>Email</th><th>LinkedIn</th><th>Persona</th><th>Batch</th><th>Status</th></tr></thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.contact_id}>
              <td>{[c.first_name, c.last_name].filter(Boolean).join(' ') || '—'}</td>
              <td className="muted">{c.title || '—'}</td>
              <td>{c.company || c.domain || '—'}</td>
              <td className="mono muted" style={{ fontSize: 11.5 }}>{c.email}</td>
              <td className="muted">{c.linkedin_url ? <a href={c.linkedin_url} target="_blank" rel="noreferrer">profile</a> : '—'}</td>
              <td><span className={`badge persona-${c.persona}`} style={{ fontSize: 10.5 }}>{c.persona}</span></td>
              <td className="mono muted">{c.batch_id ?? '—'}</td>
              <td><span className={`badge status-${c.status || 'pending'}`} style={{ fontSize: 10.5 }}>{c.status || 'pending'}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
      {detail.total > rows.length && (
        <div className="muted" style={{ padding: 8, fontSize: 12 }}>Showing {num(rows.length)} of {num(detail.total)} contacts.</div>
      )}
      {!rows.length && <div className="empty" style={{ padding: 14 }}>No contacts were added from this upload.</div>}
    </div>
  )
}

function AudienceRow({ aud, onRenamed }) {
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(aud.name)
  const [saving, setSaving] = useState(false)

  async function saveName() {
    const clean = name.trim()
    if (!clean || clean === aud.name) { setEditing(false); setName(aud.name); return }
    setSaving(true)
    try { await api.renameAudience(aud.id, clean); onRenamed(aud.id, clean); setEditing(false) }
    catch (e) { window.alert(`Rename failed: ${e.message}`); setName(aud.name); setEditing(false) }
    finally { setSaving(false) }
  }

  return (
    <>
      <tr>
        <td>
          {editing ? (
            <span className="row" style={{ gap: 6, alignItems: 'center' }}>
              <input value={name} onChange={(e) => setName(e.target.value)} autoFocus disabled={saving}
                onKeyDown={(e) => { if (e.key === 'Enter') saveName(); if (e.key === 'Escape') { setEditing(false); setName(aud.name) } }}
                style={{ width: 200, fontSize: 13 }} />
              <button className="ghost sm" onClick={saveName} disabled={saving}>{saving ? '…' : 'Save'}</button>
            </span>
          ) : (
            <>
              {aud.name}
              <button className="linklike" title="Rename this audience" style={{ marginLeft: 8, fontSize: 11.5 }}
                onClick={() => setEditing(true)}>rename</button>
            </>
          )}
          {aud.filename && <div className="mono muted" style={{ fontSize: 10.5 }}>{aud.filename}</div>}
        </td>
        <td className="muted" style={{ whiteSpace: 'nowrap' }}>{fmtWhen(aud.uploaded_at)}</td>
        <td className="muted" style={{ fontSize: 12 }}>{aud.by || '—'}</td>
        <td>{num(aud.contacts ?? aud.added)}</td>
        <td><StatusChips counts={aud.status_counts} /></td>
        <td style={{ whiteSpace: 'nowrap' }}>
          <button className="ghost sm" onClick={() => setOpen((v) => !v)}>{open ? 'Hide' : 'View'}</button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={6} style={{ padding: 0, background: 'var(--surface-hover)' }}>
            <AudienceContacts audienceId={aud.id} />
          </td>
        </tr>
      )}
    </>
  )
}

export default function CsvAudiencePanel() {
  const [audiences, setAudiences] = useState(null)
  const [file, setFile] = useState(null)
  const [name, setName] = useState('')
  const [nameTouched, setNameTouched] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  async function load() {
    try { setAudiences((await api.audiences()).audiences || []) }
    catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [])

  function pickFile(f) {
    setFile(f || null)
    setResult(null); setError(null)
    if (f && !nameTouched) setName(f.name.replace(/\.[^.]+$/, ''))
  }

  async function upload() {
    if (!file) return
    setUploading(true); setError(null); setResult(null)
    try {
      const csv = await file.text()
      const r = await api.uploadAudience(name.trim(), file.name, csv)
      if (!r.ok) throw new Error(r.error || 'upload failed')
      setResult(r)
      setFile(null); setName(''); setNameTouched(false)
      await load()
    } catch (e) { setError(e.message) }
    finally { setUploading(false) }
  }

  const added = result?.audience?.added ?? 0
  return (
    <div className="panel" style={{ marginBottom: 22 }}>
      <div className="section-h" style={{ marginTop: 0 }}>CSV Upload</div>
      <div className="muted" style={{ fontSize: 12.5, marginBottom: 10 }}>
        Upload a CSV of contacts (first/last name, job title, email, LinkedIn URL, country,
        company name/website/industry/size — header spellings are matched flexibly; email is
        required). The upload becomes a named audience, batched and ready to generate + enroll
        like any pulled list.
      </div>

      <ErrorBanner error={error} />

      <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <input type="file" accept=".csv,text/csv" style={{ fontSize: 12.5 }}
          onChange={(e) => pickFile(e.target.files && e.target.files[0])} disabled={uploading} />
        <input placeholder="Audience name" value={name} disabled={uploading}
          onChange={(e) => { setName(e.target.value); setNameTouched(true) }} style={{ width: 220 }} />
        <button onClick={upload} disabled={uploading || !file}>
          {uploading ? <Spinner label="Uploading + batching…" /> : 'Upload audience'}
        </button>
      </div>

      {result && result.ok && (
        <div className="banner info" style={{ marginTop: 12, marginBottom: 0 }}>
          <b>{result.audience.name}</b>: added <b>{num(added)}</b> contacts in{' '}
          <b>{num(result.audience.new_batches)}</b> new {result.audience.new_batches === 1 ? 'batch' : 'batches'}.
          <SkippedNote counts={result.audience.counts} />
          {added > 0 && <> Generate copy on the <b>Pipeline</b> tab.</>}
        </div>
      )}

      {audiences && audiences.length > 0 && (
        <div className="panel" style={{ padding: 0, marginTop: 14, overflow: 'auto' }}>
          <table className="dense">
            <thead><tr><th>Audience</th><th>Uploaded</th><th>By</th><th>Contacts</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {audiences.map((a) => (
                <AudienceRow key={a.id} aud={a}
                  onRenamed={(id, newName) => setAudiences((cur) => cur.map((x) => (x.id === id ? { ...x, name: newName } : x)))} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {audiences && audiences.length === 0 && (
        <div className="muted" style={{ marginTop: 10, fontSize: 12 }}>No CSV audiences uploaded yet.</div>
      )}
    </div>
  )
}
