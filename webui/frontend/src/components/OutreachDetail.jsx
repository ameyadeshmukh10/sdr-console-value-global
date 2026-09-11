import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Badge, Spinner, ErrorBanner } from './ui.jsx'

// Slide-over drawer showing one lead's full 4-touch email sequence + LinkedIn
// copy. Gated-flow additions: ✎ Edit lets the user rewrite any subject/body/
// LinkedIn touch (the edit is saved verbatim — lint only surfaces soft
// warnings), and Approve stamps the copy for enrollment. onChanged (optional)
// tells the caller to refresh its list after a save/approve.
const EMAIL_KEYS = [1, 2, 3, 4]
const LI_FIELDS = [
  ['li_connect', 'Connection request'],
  ['li_msg1', 'Message 1'],
  ['li_msg2', 'Message 2'],
]

export default function OutreachDetail({ id, onClose, onChanged }) {
  const [d, setD] = useState(null)
  const [error, setError] = useState(null)
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState(null)
  const [busy, setBusy] = useState(null) // 'save' | 'approve'
  const [warnings, setWarnings] = useState([])

  function load() {
    api.outreachDetail(id).then(setD).catch((e) => setError(e.message))
  }
  useEffect(() => {
    setD(null); setError(null); setEditing(false); setForm(null); setWarnings([])
    load()
  }, [id]) // eslint-disable-line react-hooks/exhaustive-deps

  function startEdit() {
    setForm({
      email: Object.fromEntries(EMAIL_KEYS.flatMap((i) => [
        [`subject${i}`, d.email[`subject${i}`] || ''],
        [`body${i}`, d.email[`body${i}`] || ''],
      ])),
      linkedin: Object.fromEntries(LI_FIELDS.map(([k]) => [k, d.linkedin[k] || ''])),
    })
    setWarnings([])
    setEditing(true)
  }

  async function save() {
    setBusy('save'); setError(null)
    try {
      const r = await api.updateOutreach(id, form)
      if (r.ok === false) setError(r.error || 'save failed')
      else {
        setWarnings(r.warnings || [])
        setEditing(false)
        if (r.detail) setD(r.detail); else load()
        onChanged?.()
      }
    } catch (e) { setError(e.message) }
    finally { setBusy(null) }
  }

  async function approve() {
    setBusy('approve'); setError(null)
    try {
      const r = await api.approveOutreach({ contact_ids: [id] })
      if (r.ok === false) setError(r.error || 'approve failed')
      else { load(); onChanged?.() }
    } catch (e) { setError(e.message) }
    finally { setBusy(null) }
  }

  const emailSteps = d ? EMAIL_KEYS.map((i) => ({
    n: i, subject: d.email[`subject${i}`], body: d.email[`body${i}`],
  })) : []
  const canApprove = d?.contact?.gated && d.contact.status === 'generated' && !d.contact.approved
  const setEmail = (k, v) => setForm((f) => ({ ...f, email: { ...f.email, [k]: v } }))
  const setLi = (k, v) => setForm((f) => ({ ...f, linkedin: { ...f.linkedin, [k]: v } }))

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <div className="drawer">
        <button className="close-x" onClick={onClose}>Close ✕</button>
        <ErrorBanner error={error} />
        {!d && !error && <Spinner label="Loading copy…" />}
        {d && (
          <>
            <h2 className="page-title" style={{ marginBottom: 2 }}>
              {d.contact.first_name} {d.contact.last_name}
            </h2>
            <p className="page-sub" style={{ marginBottom: 14 }}>
              {d.contact.title}{d.contact.company ? ` · ${d.contact.company}` : ''}
            </p>
            <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
              <Badge kind="persona" value={d.contact.persona} />
              <span className="badge cta">{d.cta_type}</span>
              <Badge kind="status" value={d.contact.status} />
              {d.contact.segment && <span className="badge" style={{ color: 'var(--jade)', borderColor: 'var(--jade)' }}>{d.contact.segment}</span>}
              {d.contact.gated && (d.contact.approved
                ? <span className="badge" style={{ color: 'var(--green)', borderColor: 'var(--green)' }}>✓ approved</span>
                : d.contact.status === 'generated'
                  ? <span className="badge" style={{ color: 'var(--amber)', borderColor: 'var(--amber)' }}>awaiting approval</span>
                  : null)}
              {d.edited_at && <span className="badge muted" title={d.edited_at}>edited</span>}
              {d.contact.batch_id != null && <span className="badge">batch #{d.contact.batch_id}</span>}
            </div>

            <div className="kv">
              <span className="k">Email</span><span className="mono">{d.contact.email}</span>
              {d.contact.linkedin_url && (<>
                <span className="k">LinkedIn</span>
                <a href={d.contact.linkedin_url} target="_blank" rel="noreferrer">{d.contact.linkedin_url}</a>
              </>)}
              {d.contact.error && (<><span className="k">Error</span><span style={{ color: 'var(--amber)' }}>{d.contact.error}</span></>)}
            </div>

            <div className="row" style={{ gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
              {!editing && (
                <button className="ghost sm" onClick={startEdit}>✎ Edit copy</button>
              )}
              {editing && (<>
                <button className="sm" disabled={busy === 'save'} onClick={save}>
                  {busy === 'save' ? <Spinner /> : 'Save edits'}
                </button>
                <button className="ghost sm" disabled={busy === 'save'}
                  onClick={() => { setEditing(false); setWarnings([]) }}>Cancel</button>
              </>)}
              {canApprove && !editing && (
                <button className="sm" disabled={busy === 'approve'} onClick={approve}>
                  {busy === 'approve' ? <Spinner /> : '✓ Approve for enrollment'}
                </button>
              )}
            </div>

            {warnings.length > 0 && (
              <div className="banner" style={{ marginTop: 10, borderColor: 'var(--amber)', color: 'var(--amber)' }}>
                Saved. Style notes (your edit stands either way): {warnings.join(' · ')}
              </div>
            )}

            <div className="section-h">Signal</div>
            <div className="touch"><div className="body">{d.signal}</div></div>
            {d.account_signal?.signal && d.account_signal.signal !== d.signal && (
              <div className="touch">
                <div className="step">Account research{d.account_signal.researched_at ? ` · ${String(d.account_signal.researched_at).slice(0, 10)}` : ''}</div>
                <div className="body">{d.account_signal.signal}</div>
              </div>
            )}
            {d.account_signal?.trigger && (
              <div className="touch">
                <div className="step">
                  {d.account_signal.trigger.label} evidence
                  {d.account_signal.trigger.date ? ` · ${d.account_signal.trigger.date}` : ''}
                  {d.account_signal.trigger.score != null ? ` · score ${d.account_signal.trigger.score}` : ''}
                </div>
                {d.account_signal.trigger.headline && <div className="subj">{d.account_signal.trigger.headline}</div>}
                {d.account_signal.trigger.summary && <div className="body">{d.account_signal.trigger.summary}</div>}
                {d.account_signal.trigger.source_url && (
                  <div className="body" style={{ marginTop: 6 }}>
                    <a href={d.account_signal.trigger.source_url} target="_blank" rel="noreferrer" className="mono">
                      {d.account_signal.trigger.source_url}
                    </a>
                  </div>
                )}
              </div>
            )}

            <div className="section-h">Email sequence</div>
            {!editing && emailSteps.map((s) => (
              <div className="touch" key={s.n}>
                <div className="step">Touch {s.n}</div>
                <div className="subj">{s.subject}</div>
                <div className="body">{s.body}</div>
              </div>
            ))}
            {editing && EMAIL_KEYS.map((i) => (
              <div className="touch" key={i}>
                <div className="step">Touch {i}</div>
                <input value={form.email[`subject${i}`]} placeholder="Subject"
                  onChange={(e) => setEmail(`subject${i}`, e.target.value)}
                  style={{ width: '100%', marginBottom: 6 }} />
                <textarea value={form.email[`body${i}`]} rows={6}
                  onChange={(e) => setEmail(`body${i}`, e.target.value)}
                  style={{ width: '100%', resize: 'vertical', fontFamily: 'inherit', fontSize: 'inherit' }} />
              </div>
            ))}

            <div className="section-h">LinkedIn</div>
            {!editing && LI_FIELDS.map(([k, label]) => (
              <div className="touch" key={k}>
                <div className="step">{label}</div>
                <div className="body">{d.linkedin[k] || <span className="muted">—</span>}</div>
              </div>
            ))}
            {editing && LI_FIELDS.map(([k, label]) => (
              <div className="touch" key={k}>
                <div className="step">{label}</div>
                <textarea value={form.linkedin[k]} rows={3}
                  onChange={(e) => setLi(k, e.target.value)}
                  style={{ width: '100%', resize: 'vertical', fontFamily: 'inherit', fontSize: 'inherit' }} />
              </div>
            ))}
          </>
        )}
      </div>
    </>
  )
}
