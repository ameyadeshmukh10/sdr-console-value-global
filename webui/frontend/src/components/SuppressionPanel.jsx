import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'

// The client's do-not-contact account list. Hard matches are blocked at CSV
// ingest, the segment gate, and enrollment; soft (fuzzy) matches are flagged
// for human review. Loading the signed-off list BEFORE the first send is a
// hard program requirement.
export default function SuppressionPanel() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(false)
  const [csvText, setCsvText] = useState('')
  const [replace, setReplace] = useState(false)
  const [result, setResult] = useState(null)
  const fileRef = useRef(null)

  const load = useCallback(() => {
    api.suppression().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }, [])
  useEffect(() => { load() }, [load])

  async function upload() {
    if (!csvText.trim()) return
    setBusy(true); setError(null); setResult(null)
    try {
      const r = await api.suppressionUpload(csvText, replace)
      if (r.ok === false) setError(r.error || 'upload failed')
      else { setResult(r); setCsvText(''); setReplace(false) }
      load()
    } catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  async function remove(id) {
    setBusy(true); setError(null)
    try { await api.suppressionRemove(id); load() }
    catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  function pickFile(e) {
    const f = e.target.files?.[0]
    if (!f) return
    const reader = new FileReader()
    reader.onload = () => setCsvText(String(reader.result || ''))
    reader.readAsText(f)
  }

  const rules = data?.rules || []
  return (
    <div className="panel" style={{ marginBottom: 22 }}>
      <div className="row between">
        <span className="section-h" style={{ margin: 0 }}>
          Do-not-contact list
          <span className="badge danger" style={{ marginLeft: 10 }}>{num(rules.length)} accounts</span>
        </span>
        <button className="ghost sm" onClick={() => setOpen(!open)}>{open ? 'Hide' : 'Manage'}</button>
      </div>
      <p className="muted" style={{ fontSize: 13, marginBottom: open ? 10 : 0 }}>
        Client-owned relationships, live opportunities, and non-targets. Matching accounts are blocked
        at import, the segment gate, and enrollment.
        {data && data.gated_accounts > 0 && (
          <> Currently matching {num(data.hard_matches)} of {num(data.gated_accounts)} awaiting accounts
          {data.soft_matches > 0 ? ` (+${num(data.soft_matches)} near-miss, flagged for review)` : ''}
          {data.fusion_only > 0 ? ` · ${num(data.fusion_only)} Fusion-only` : ''}.</>
        )}
        {rules.length === 0 && <b> No list loaded yet — load it before the first send.</b>}
      </p>

      {open && (
        <>
          <ErrorBanner error={error} />
          {result && (
            <div className="banner info" style={{ marginBottom: 10 }}>
              Loaded {num(result.added)} new rule{result.added === 1 ? '' : 's'} — {num(result.active_rules)} active.
            </div>
          )}
          {rules.length > 0 && (
            <div className="panel" style={{ padding: 0, marginBottom: 12, maxHeight: 260, overflow: 'auto' }}>
              <table className="dense">
                <thead><tr><th>Account</th><th>Domain</th><th>Reason</th><th></th></tr></thead>
                <tbody>
                  {rules.map((r) => (
                    <tr key={r.id}>
                      <td><b>{r.name}</b></td>
                      <td className="mono muted">{r.domain || '—'}</td>
                      <td className="muted">{r.reason || '—'}</td>
                      <td>
                        <button className="ghost sm" disabled={busy} onClick={() => remove(r.id)}>Remove</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <textarea rows={4} value={csvText} onChange={(e) => setCsvText(e.target.value)}
            placeholder={'One account per line (optionally name,domain,reason):\nWaste Management\nAcme Corp,acme.com,existing client'}
            style={{ width: '100%', marginBottom: 8 }} />
          <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
            <button className="sm" disabled={busy || !csvText.trim()} onClick={upload}>
              {busy ? <Spinner /> : 'Load accounts'}
            </button>
            <button className="ghost sm" onClick={() => fileRef.current?.click()}>Choose CSV…</button>
            <input ref={fileRef} type="file" accept=".csv,.txt" hidden onChange={pickFile} />
            <label className="row muted" style={{ gap: 6, fontSize: 13 }}>
              <input type="checkbox" checked={replace} style={{ width: 'auto' }}
                onChange={(e) => setReplace(e.target.checked)} />
              Replace the existing list (full reload)
            </label>
          </div>
        </>
      )}
    </div>
  )
}
