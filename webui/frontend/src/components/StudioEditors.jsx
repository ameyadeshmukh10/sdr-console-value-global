import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Badge, Spinner } from './ui.jsx'
import { BRAND, PERSONA_COLORS } from '../theme.js'

// Editing primitives for the Orchestration studio's node inspector pane.
// Edits are stored on the data volume as an override layer: the committed
// defaults stay untouched, "Reset to default" restores them, and the copy
// linter is deliberately NOT editable, so no edit can bypass the guardrails.

export const PERSONA_ORDER = ['erp-owner', 'dba', 'data-governance', 'it-leadership']
export const SEGMENT_ORDER = ['ma_carveout', 'erp_migration', 'license_audit', 'ebs_oci', 'ebs_performance']

// One shared instructions store per page: load once, refresh on save/reset.
export function useInstructions() {
  const [ins, setIns] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(null)     // 'save' | 'reset' while a call runs
  const [note, setNote] = useState(null)     // {tone, text}
  const [version, setVersion] = useState(0)  // bump to remount editors on fresh data

  useEffect(() => {
    api.instructions().then(setIns).catch((e) => setError(e.message))
  }, [])

  async function save(body) {
    setBusy('save'); setError(null); setNote(null)
    try {
      const r = await api.saveInstructions(body)
      setIns(r); setVersion((v) => v + 1)
      setNote(r.warnings?.length
        ? { tone: 'warn', text: `Saved, with notes: ${r.warnings.join(' · ')}` }
        : { tone: 'info', text: 'Saved. Changes apply to the next generation run — already-generated copy is unchanged until regenerated.' })
    } catch (e) { setError(e.message) }
    finally { setBusy(null) }
  }

  async function reset(body, what) {
    if (!window.confirm(`Reset — ${what}? Your edit is discarded.`)) return
    setBusy('reset'); setError(null); setNote(null)
    try {
      const r = await api.resetInstructions(body)
      setIns(r); setVersion((v) => v + 1)
      setNote({ tone: 'info', text: 'Restored the committed default.' })
    } catch (e) { setError(e.message) }
    finally { setBusy(null) }
  }

  const clearNote = () => setNote(null)
  return { ins, error, busy, note, version, save, reset, clearNote }
}

// True when this item carries an operator override (drives the ● dots).
export function customized(ins, kind, key) {
  if (!ins) return false
  if (kind === 'persona') return !!ins.personas?.overrides?.[key]
  if (kind === 'play') return !!ins.plays?.overrides?.[key]
  if (kind === 'knowledge') return ins.knowledge?.[key]?.override != null
  if (kind === 'icp') {
    return Object.keys(ins.icp?.include || {}).length > 0 || (ins.icp?.exclude || []).length > 0
  }
  return false
}

export function CustomizedBadge({ on }) {
  if (!on) return <span className="badge muted">default</span>
  return <span className="badge" style={{ color: BRAND.amber, borderColor: BRAND.amber }}>customized</span>
}

export function EditStamp({ meta }) {
  if (!meta?.updated_at) return null
  return (
    <span className="muted" style={{ fontSize: 11.5 }}>
      edited {String(meta.updated_at).slice(0, 10)}{meta.updated_by ? ` by ${meta.updated_by}` : ''}
    </span>
  )
}

export function FieldLabel({ children, hint }) {
  return (
    <div style={{ margin: '14px 0 5px' }}>
      <span style={{ fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.6px', color: 'var(--faint)' }}>{children}</span>
      {hint && <span className="muted" style={{ fontSize: 12, marginLeft: 8, textTransform: 'none', letterSpacing: 0 }}>{hint}</span>}
    </div>
  )
}

export function EditorFooter({ dirty, overridden, busy, onSave, onReset, saved }) {
  return (
    <div className="row" style={{ gap: 10, marginTop: 14, alignItems: 'center', flexWrap: 'wrap' }}>
      <button className="sm" disabled={!dirty || busy} onClick={onSave}>
        {busy === 'save' ? <Spinner /> : 'Save'}
      </button>
      {overridden && (
        <button className="ghost sm" disabled={!!busy} onClick={onReset}>
          {busy === 'reset' ? <Spinner /> : 'Reset to default'}
        </button>
      )}
      {!dirty && saved && <span className="muted" style={{ fontSize: 12 }}>{saved}</span>}
    </div>
  )
}

const TA = { width: '100%', resize: 'vertical', fontFamily: 'inherit', fontSize: 13.5, lineHeight: 1.55 }

// ---- keyword chips (ICP editing) --------------------------------------------
export function ChipInput({ values, onChange, placeholder, color }) {
  const [draft, setDraft] = useState('')
  const add = () => {
    const v = draft.trim()
    if (!v) return
    if (!values.some((x) => x.toLowerCase() === v.toLowerCase())) onChange([...values, v])
    setDraft('')
  }
  return (
    <div>
      <div style={{ lineHeight: 2.1, minHeight: 26 }}>
        {values.map((v) => (
          <span key={v} className="badge" style={{ color: color || 'var(--text)', borderColor: color || 'var(--border-strong)', marginRight: 6 }}>
            {v}
            <button className="linklike" title={`Remove "${v}"`} style={{ marginLeft: 6, color: 'inherit', fontWeight: 700 }}
              onClick={() => onChange(values.filter((x) => x !== v))}>×</button>
          </span>
        ))}
        {values.length === 0 && <span className="muted" style={{ fontSize: 12.5 }}>none yet</span>}
      </div>
      <div className="row" style={{ gap: 8, marginTop: 6 }}>
        <input value={draft} placeholder={placeholder} style={{ maxWidth: 300 }}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add() } }} />
        <button className="ghost sm" onClick={add} disabled={!draft.trim()}>Add</button>
      </div>
    </div>
  )
}

// ---- ICP custom rules --------------------------------------------------------
export function IcpRulesEditor({ ins, busy, onSave, onReset }) {
  const saved = ins.icp || { include: {}, exclude: [] }
  const [include, setInclude] = useState(() => ({ ...saved.include }))
  const [exclude, setExclude] = useState(() => [...(saved.exclude || [])])
  const overridden = customized(ins, 'icp')
  const dirty = JSON.stringify({ include, exclude }) !== JSON.stringify({ include: saved.include || {}, exclude: saved.exclude || [] })

  return (
    <>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        Custom keywords extend the built-in rules without changing their precedence: an{' '}
        <b>exclude</b> keyword drops a title outright, an <b>include</b> keyword admits a title
        the built-in rules missed. Plain-word matching, no code — saved rules apply to the very
        next import.
      </p>
      <div className="row between" style={{ alignItems: 'center' }}>
        <b>Custom rules</b>
        <span className="row" style={{ gap: 10, alignItems: 'center' }}>
          <EditStamp meta={ins.meta?.icp} />
          <CustomizedBadge on={overridden} />
        </span>
      </div>

      <FieldLabel hint="titles containing any of these are never contacted">Always exclude</FieldLabel>
      <ChipInput values={exclude} onChange={setExclude} placeholder="e.g. field marketing" color={BRAND.red} />

      {PERSONA_ORDER.map((pid) => (
        <div key={pid}>
          <FieldLabel hint="titles containing any of these are admitted as this persona">
            Also include as <Badge kind="persona" value={pid} />
          </FieldLabel>
          <ChipInput values={include[pid] || []} color={PERSONA_COLORS[pid]}
            onChange={(v) => setInclude((m) => { const next = { ...m }; if (v.length) next[pid] = v; else delete next[pid]; return next })}
            placeholder="e.g. netsuite administrator" />
        </div>
      ))}

      <EditorFooter dirty={dirty} overridden={overridden} busy={busy}
        onSave={() => onSave({ kind: 'icp', content: { include, exclude } })}
        onReset={() => onReset({ kind: 'icp' }, 'remove every custom ICP keyword')} />
    </>
  )
}

export function TitleTester() {
  const [title, setTitle] = useState('')
  const [result, setResult] = useState(null)
  async function run() {
    if (!title.trim()) return
    setResult({ loading: true })
    try { setResult(await api.icpTest(title.trim())) }
    catch (e) { setResult({ ok: false, error: e.message }) }
  }
  return (
    <>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        Paste any job title to see exactly how the live rules (built-in + your custom keywords)
        classify it.
      </p>
      <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
        <input value={title} placeholder="e.g. Director of Enterprise Applications" style={{ maxWidth: 340 }}
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') run() }} />
        <button className="ghost sm" onClick={run} disabled={!title.trim()}>Classify</button>
      </div>
      {result && (
        <div style={{ marginTop: 12, fontSize: 13 }}>
          {result.loading ? <Spinner /> : result.ok ? (
            <span className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              {result.icp
                ? <span className="badge" style={{ color: BRAND.jade, borderColor: BRAND.jade }}>✓ ICP</span>
                : <span className="badge" style={{ color: BRAND.red, borderColor: BRAND.red }}>✗ not ICP</span>}
              <span>{result.role}</span>
              {result.persona && <Badge kind="persona" value={result.persona} />}
              {!result.icp && <span className="muted">this title would be skipped</span>}
            </span>
          ) : <span style={{ color: BRAND.red }}>{result.error}</span>}
        </div>
      )}
    </>
  )
}

// ---- persona framing ---------------------------------------------------------
export function PersonaEditor({ pid, def, ovr, meta, busy, onSave, onReset }) {
  const [pain, setPain] = useState(ovr?.pain ?? def?.pain ?? '')
  const [outcome, setOutcome] = useState(ovr?.outcome ?? def?.outcome ?? '')
  const overridden = !!ovr
  const dirty = pain !== (ovr?.pain ?? def?.pain ?? '') || outcome !== (ovr?.outcome ?? def?.outcome ?? '')

  return (
    <>
      <div className="row between" style={{ alignItems: 'center' }}>
        <span className="muted" style={{ fontSize: 12.5 }}>
          These two fields steer how the writer frames every email to this persona. The give
          ladder, tone, and every guardrail stay fixed.
        </span>
        <span className="row" style={{ gap: 10, alignItems: 'center', flexShrink: 0 }}>
          <EditStamp meta={meta} />
          <CustomizedBadge on={overridden} />
        </span>
      </div>

      <FieldLabel hint="what hurts for this person">Pain</FieldLabel>
      <textarea rows={5} style={TA} value={pain} onChange={(e) => setPain(e.target.value)} />
      <FieldLabel hint="what we sell them">Outcome to sell</FieldLabel>
      <textarea rows={4} style={TA} value={outcome} onChange={(e) => setOutcome(e.target.value)} />

      <EditorFooter dirty={dirty} overridden={overridden} busy={busy}
        saved="Saved — the next generation run writes with this framing."
        onSave={() => onSave({ kind: 'persona', key: pid, content: { pain, outcome } })}
        onReset={() => onReset({ kind: 'persona', key: pid }, `restore ${def?.name || pid} to the committed default`)} />
    </>
  )
}

export function PersonaLockedDetails({ def }) {
  if (!def) return null
  return (
    <>
      <p className="muted" style={{ fontSize: 12.5, marginTop: 0 }}>
        Fixed by the offer ladder and the client's voice rules — edit the documents in the
        Trigger plays and Knowledge base nodes to change them for every persona at once.
      </p>
      {def.ctas && (
        <div className="kv" style={{ marginTop: 12 }}>
          <div className="muted">Gives / CTAs 🔒</div>
          <div style={{ fontSize: 13 }}>{def.ctas}</div>
        </div>
      )}
      {def.tone && (
        <div className="kv">
          <div className="muted">Tone 🔒</div>
          <div style={{ fontSize: 13 }}>{def.tone}</div>
        </div>
      )}
      {def.description && (
        <div className="kv">
          <div className="muted">Agent brief</div>
          <div style={{ fontSize: 12.5 }} className="muted">{def.description}</div>
        </div>
      )}
    </>
  )
}

// ---- trigger plays -----------------------------------------------------------
const PLAY_FIELD_META = [
  ['problem', 'The problem this trigger creates', 'what the event breaks or threatens for them — the writer builds email 1 and 2 from this'],
  ['solution', 'What ERP Data Retirement does about it', 'the answer, in plain words — grounds the offer in emails 1-2'],
  ['opener', 'Email 1 opens on', 'what the first sentence anchors to'],
]

export function PlayEditor({ seg, def, ovr, meta, busy, onSave, onReset }) {
  const eff = (k) => ovr?.[k] ?? def?.[k] ?? ''
  const [fields, setFields] = useState(() => ({ problem: eff('problem'), solution: eff('solution'), opener: eff('opener') }))
  const overridden = !!ovr
  const dirty = PLAY_FIELD_META.some(([k]) => fields[k] !== (ovr?.[k] ?? def?.[k] ?? ''))

  return (
    <>
      <div className="row between" style={{ alignItems: 'center' }}>
        <span className="muted" style={{ fontSize: 12.5 }}>
          The whole sequence anchors on this play when an account is approved through this
          trigger. The 4-touch structure and the ban list stay enforced by the linter.
        </span>
        <span className="row" style={{ gap: 10, alignItems: 'center', flexShrink: 0 }}>
          <EditStamp meta={meta} />
          <CustomizedBadge on={overridden} />
        </span>
      </div>
      {PLAY_FIELD_META.map(([k, label, hint]) => (
        <div key={k}>
          <FieldLabel hint={hint}>{label}</FieldLabel>
          <textarea rows={k === 'opener' ? 3 : 5} style={TA} value={fields[k]}
            onChange={(e) => setFields((f) => ({ ...f, [k]: e.target.value }))} />
        </div>
      ))}
      <EditorFooter dirty={dirty} overridden={overridden} busy={busy}
        saved="Saved — the next generation run for this trigger uses this play."
        onSave={() => onSave({ kind: 'play', key: seg, content: fields })}
        onReset={() => onReset({ kind: 'play', key: seg }, `restore the ${def?.label || seg} play to the committed default`)} />
    </>
  )
}

// ---- knowledge documents -----------------------------------------------------
export function DocEditor({ fname, doc, meta, busy, onSave, onReset, version, description }) {
  const effective = doc?.override ?? doc?.default ?? ''
  const [text, setText] = useState(effective)
  useEffect(() => { setText(doc?.override ?? doc?.default ?? '') }, [version, fname]) // eslint-disable-line react-hooks/exhaustive-deps
  const overridden = doc?.override != null
  const dirty = text !== effective
  if (!doc) return null
  return (
    <>
      <div className="row between" style={{ alignItems: 'center' }}>
        {description
          ? <span className="muted" style={{ fontSize: 12.5 }}>{description}</span>
          : <b className="mono" style={{ fontSize: 13 }}>{fname}</b>}
        <span className="row" style={{ gap: 10, alignItems: 'center', flexShrink: 0 }}>
          <EditStamp meta={meta} />
          <CustomizedBadge on={overridden} />
        </span>
      </div>
      <textarea rows={20} spellCheck={false} value={text} onChange={(e) => setText(e.target.value)}
        style={{ width: '100%', resize: 'vertical', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 12.5, lineHeight: 1.6, marginTop: 10 }} />
      <div className="row between" style={{ alignItems: 'center' }}>
        <EditorFooter dirty={dirty} overridden={overridden} busy={busy}
          saved="Saved — every writer reads this on the next generation run."
          onSave={() => onSave({ kind: 'knowledge', key: fname, content: text })}
          onReset={() => onReset({ kind: 'knowledge', key: fname }, `restore ${fname} to the committed default`)} />
        <span className="muted" style={{ fontSize: 11.5 }}>{text.length.toLocaleString()} characters</span>
      </div>
    </>
  )
}
