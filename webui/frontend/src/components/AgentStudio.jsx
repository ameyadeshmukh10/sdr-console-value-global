import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Badge, Spinner, ErrorBanner } from './ui.jsx'
import { BRAND, PERSONA_COLORS } from '../theme.js'
import {
  PipelineSection, IcpFilterSection, GuardrailsSection, SignalsSection,
} from './OrchestrationSections.jsx'

// The Orchestration studio: one tabbed panel where a non-technical operator
// reads AND edits how the AI SDR thinks — custom ICP keywords, per-persona
// framing, the five ERP trigger plays, and the knowledge-base documents.
// Edits are stored on the data volume as an override layer: the committed
// defaults stay untouched, "Reset to default" restores them, and the copy
// linter is deliberately NOT editable, so no edit can bypass the guardrails.

export const STUDIO_TABS = [
  { id: 'overview', label: 'Pipeline overview' },
  { id: 'icp', label: 'ICP filter' },
  { id: 'personas', label: 'Persona agents' },
  { id: 'sequencing', label: 'Sequencing & plays' },
  { id: 'knowledge', label: 'Knowledge base' },
]

const SEGMENT_ORDER = ['ma_carveout', 'erp_migration', 'license_audit', 'ebs_oci', 'ebs_performance']
const PERSONA_ORDER = ['erp-owner', 'dba', 'data-governance', 'it-leadership']

function CustomizedBadge({ on }) {
  if (!on) return <span className="badge muted">default</span>
  return <span className="badge" style={{ color: BRAND.amber, borderColor: BRAND.amber }}>customized</span>
}

function EditStamp({ meta }) {
  if (!meta?.updated_at) return null
  return (
    <span className="muted" style={{ fontSize: 11.5 }}>
      edited {String(meta.updated_at).slice(0, 10)}{meta.updated_by ? ` by ${meta.updated_by}` : ''}
    </span>
  )
}

function FieldLabel({ children, hint }) {
  return (
    <div style={{ margin: '14px 0 5px' }}>
      <span style={{ fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.6px', color: 'var(--faint)' }}>{children}</span>
      {hint && <span className="muted" style={{ fontSize: 12, marginLeft: 8, textTransform: 'none', letterSpacing: 0 }}>{hint}</span>}
    </div>
  )
}

function EditorFooter({ dirty, overridden, busy, onSave, onReset, saved }) {
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

// ---- keyword chips (ICP tab) ------------------------------------------------
function ChipInput({ values, onChange, placeholder, color }) {
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
        <input value={draft} placeholder={placeholder} style={{ maxWidth: 320 }}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add() } }} />
        <button className="ghost sm" onClick={add} disabled={!draft.trim()}>Add</button>
      </div>
    </div>
  )
}

// ---- ICP tab ----------------------------------------------------------------
function IcpTab({ config, ins, busy, onSave, onReset }) {
  const saved = ins.icp || { include: {}, exclude: [] }
  const [include, setInclude] = useState(() => ({ ...saved.include }))
  const [exclude, setExclude] = useState(() => [...(saved.exclude || [])])
  const [showBuiltIn, setShowBuiltIn] = useState(false)
  const [testTitle, setTestTitle] = useState('')
  const [testResult, setTestResult] = useState(null)

  const overridden = Object.keys(saved.include || {}).length > 0 || (saved.exclude || []).length > 0
  const dirty = JSON.stringify({ include, exclude }) !== JSON.stringify({ include: saved.include || {}, exclude: saved.exclude || [] })

  async function runTest() {
    if (!testTitle.trim()) return
    setTestResult({ loading: true })
    try { setTestResult(await api.icpTest(testTitle.trim())) }
    catch (e) { setTestResult({ ok: false, error: e.message }) }
  }

  return (
    <>
      <p className="muted" style={{ fontSize: 13, maxWidth: 720, marginTop: 0 }}>
        Every imported contact's job title is classified into the buying group (or dropped) by the
        built-in rules below. Your custom keywords extend them without changing their precedence:
        an <b>exclude</b> keyword drops a title outright, an <b>include</b> keyword admits a title
        the built-in rules missed and assigns it to the persona you choose. Plain-word matching,
        no code needed — saved rules apply to the very next import.
      </p>

      <div className="touch">
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
      </div>

      <div className="touch">
        <b>Try a title</b>
        <p className="muted" style={{ fontSize: 12.5, margin: '4px 0 10px' }}>
          Paste any job title to see exactly how the live rules (built-in + your custom keywords) classify it.
        </p>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <input value={testTitle} placeholder="e.g. Director of Enterprise Applications" style={{ maxWidth: 380 }}
            onChange={(e) => setTestTitle(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') runTest() }} />
          <button className="ghost sm" onClick={runTest} disabled={!testTitle.trim()}>Classify</button>
        </div>
        {testResult && (
          <div style={{ marginTop: 10, fontSize: 13 }}>
            {testResult.loading ? <Spinner /> : testResult.ok ? (
              <span className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                {testResult.icp
                  ? <span className="badge" style={{ color: BRAND.jade, borderColor: BRAND.jade }}>✓ ICP</span>
                  : <span className="badge" style={{ color: BRAND.red, borderColor: BRAND.red }}>✗ not ICP</span>}
                <span>{testResult.role}</span>
                {testResult.persona && <Badge kind="persona" value={testResult.persona} />}
                {!testResult.icp && <span className="muted">this title would be skipped</span>}
              </span>
            ) : <span style={{ color: BRAND.red }}>{testResult.error}</span>}
          </div>
        )}
      </div>

      <button className="linklike" onClick={() => setShowBuiltIn((v) => !v)}>
        {showBuiltIn ? '▾ Hide the built-in rules' : '▸ Show the built-in rules (read-only)'}
      </button>
      {showBuiltIn && <div style={{ marginTop: 10 }}><IcpFilterSection data={config?.icp_filter} /></div>}
    </>
  )
}

// ---- persona tab ------------------------------------------------------------
function PersonaEditor({ pid, def, ovr, meta, busy, onSave, onReset }) {
  const [pain, setPain] = useState(ovr?.pain ?? def?.pain ?? '')
  const [outcome, setOutcome] = useState(ovr?.outcome ?? def?.outcome ?? '')
  const overridden = !!ovr
  const dirty = pain !== (ovr?.pain ?? def?.pain ?? '') || outcome !== (ovr?.outcome ?? def?.outcome ?? '')
  const ta = { width: '100%', resize: 'vertical', fontFamily: 'inherit', fontSize: 13.5, lineHeight: 1.55 }

  return (
    <div className="touch">
      <div className="row between" style={{ alignItems: 'center' }}>
        <b>
          <span aria-hidden style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 999, background: PERSONA_COLORS[pid] || 'var(--border)', marginRight: 8, verticalAlign: 'baseline' }} />
          {def?.name || pid}
        </b>
        <span className="row" style={{ gap: 10, alignItems: 'center' }}>
          <EditStamp meta={meta} />
          <CustomizedBadge on={overridden} />
        </span>
      </div>
      <p className="muted" style={{ fontSize: 12.5, margin: '6px 0 0' }}>
        These two fields steer how the writer frames every email to this persona. Plain language
        works best: describe the pain in their words, and the outcome we sell. The give ladder,
        tone, and every guardrail stay fixed.
      </p>

      <FieldLabel hint="what hurts for this person">Pain</FieldLabel>
      <textarea rows={4} style={ta} value={pain} onChange={(e) => setPain(e.target.value)} />
      <FieldLabel hint="what we sell them">Outcome to sell</FieldLabel>
      <textarea rows={3} style={ta} value={outcome} onChange={(e) => setOutcome(e.target.value)} />

      {def?.ctas && (
        <div className="kv" style={{ marginTop: 12 }}>
          <div className="muted">Gives / CTAs <span title="Fixed by the offer ladder — edit cta-offers.md in Sequencing & plays">🔒</span></div>
          <div style={{ fontSize: 12.5 }}>{def.ctas}</div>
        </div>
      )}
      {def?.tone && (
        <div className="kv">
          <div className="muted">Tone 🔒</div>
          <div style={{ fontSize: 12.5 }}>{def.tone}</div>
        </div>
      )}

      <EditorFooter dirty={dirty} overridden={overridden} busy={busy}
        saved="Saved — the next generation run writes with this framing."
        onSave={() => onSave({ kind: 'persona', key: pid, content: { pain, outcome } })}
        onReset={() => onReset({ kind: 'persona', key: pid }, `restore ${def?.name || pid} to the committed default`)} />
    </div>
  )
}

function PersonasTab({ ins, busy, onSave, onReset, version }) {
  const [pid, setPid] = useState(PERSONA_ORDER[0])
  const defs = ins.personas?.defaults || {}
  const ovrs = ins.personas?.overrides || {}
  return (
    <>
      <p className="muted" style={{ fontSize: 13, maxWidth: 720, marginTop: 0 }}>
        Four persona agents write the outreach; messaging is uniform across the buying group by
        client decision, so an edit here adjusts the <i>framing</i> the writer uses for that
        persona, not the sequence structure.
      </p>
      <div className="tabs" style={{ marginBottom: 14 }}>
        {PERSONA_ORDER.map((p) => (
          <button key={p} className={'tab' + (pid === p ? ' active' : '')} onClick={() => setPid(p)}
            style={pid === p ? { color: PERSONA_COLORS[p], borderColor: PERSONA_COLORS[p], background: 'transparent' } : undefined}>
            {defs[p]?.name || p}
            {ovrs[p] && <span className="n" title="customized">●</span>}
          </button>
        ))}
      </div>
      <PersonaEditor key={pid + version} pid={pid} def={defs[pid]} ovr={ovrs[pid]}
        meta={ins.meta?.[`persona:${pid}`]} busy={busy} onSave={onSave} onReset={onReset} />
    </>
  )
}

// ---- sequencing & plays tab -------------------------------------------------
const PLAY_FIELD_META = [
  ['problem', 'The problem this trigger creates', 'what the event breaks or threatens for them — the writer builds email 1 and 2 from this'],
  ['solution', 'What ERP Data Retirement does about it', 'the answer, in plain words — grounds the offer in emails 1-2'],
  ['opener', 'Email 1 opens on', 'what the first sentence anchors to'],
]

function PlayEditor({ seg, def, ovr, meta, busy, onSave, onReset }) {
  const eff = (k) => ovr?.[k] ?? def?.[k] ?? ''
  const [fields, setFields] = useState(() => ({ problem: eff('problem'), solution: eff('solution'), opener: eff('opener') }))
  const overridden = !!ovr
  const dirty = PLAY_FIELD_META.some(([k]) => fields[k] !== (ovr?.[k] ?? def?.[k] ?? ''))
  const ta = { width: '100%', resize: 'vertical', fontFamily: 'inherit', fontSize: 13.5, lineHeight: 1.55 }

  return (
    <div className="touch">
      <div className="row between" style={{ alignItems: 'center' }}>
        <b>{def?.label || seg}</b>
        <span className="row" style={{ gap: 10, alignItems: 'center' }}>
          <EditStamp meta={meta} />
          <CustomizedBadge on={overridden} />
        </span>
      </div>
      {PLAY_FIELD_META.map(([k, label, hint]) => (
        <div key={k}>
          <FieldLabel hint={hint}>{label}</FieldLabel>
          <textarea rows={k === 'opener' ? 3 : 5} style={ta} value={fields[k]}
            onChange={(e) => setFields((f) => ({ ...f, [k]: e.target.value }))} />
        </div>
      ))}
      <EditorFooter dirty={dirty} overridden={overridden} busy={busy}
        saved="Saved — the next generation run for this trigger uses this play."
        onSave={() => onSave({ kind: 'play', key: seg, content: fields })}
        onReset={() => onReset({ kind: 'play', key: seg }, `restore the ${def?.label || seg} play to the committed default`)} />
    </div>
  )
}

function SequencingTab({ ins, busy, onSave, onReset, version }) {
  const defs = ins.plays?.defaults || {}
  const ovrs = ins.plays?.overrides || {}
  const [seg, setSeg] = useState(SEGMENT_ORDER[0])
  return (
    <>
      <p className="muted" style={{ fontSize: 13, maxWidth: 720, marginTop: 0 }}>
        When an account is approved through a trigger segment, the writer anchors the whole
        sequence on that trigger's <b>play</b>. Edit the play in plain language; the 4-touch
        structure, subjects, word bands and the ban list are enforced by the linter and cannot
        be broken from here.
      </p>
      <div className="tabs" style={{ marginBottom: 14 }}>
        {SEGMENT_ORDER.map((s) => (
          <button key={s} className={'tab' + (seg === s ? ' active' : '')} onClick={() => setSeg(s)}>
            {defs[s]?.label || s}
            {ovrs[s] && <span className="n" title="customized">●</span>}
          </button>
        ))}
      </div>
      <PlayEditor key={seg + version} seg={seg} def={defs[seg]} ovr={ovrs[seg]}
        meta={ins.meta?.[`play:${seg}`]} busy={busy} onSave={onSave} onReset={onReset} />

      <div className="section-h">The offer ladder & CTAs</div>
      <p className="muted" style={{ fontSize: 12.5, marginTop: 0 }}>
        The give ladder (POV read → free assessment → 20-minute call), the approved ask wording,
        and the anti-patterns live in one document every writer reads. Edit it below.
      </p>
      <DocEditor fname="cta-offers.md" doc={ins.knowledge?.['cta-offers.md']}
        meta={ins.meta?.['knowledge:cta-offers.md']} busy={busy} onSave={onSave} onReset={onReset}
        version={version} />
    </>
  )
}

// ---- knowledge tab ----------------------------------------------------------
function DocEditor({ fname, doc, meta, busy, onSave, onReset, version, description }) {
  const effective = doc?.override ?? doc?.default ?? ''
  const [text, setText] = useState(effective)
  useEffect(() => { setText(doc?.override ?? doc?.default ?? '') }, [version, fname]) // eslint-disable-line react-hooks/exhaustive-deps
  const overridden = doc?.override != null
  const dirty = text !== effective
  if (!doc) return null
  return (
    <div className="touch">
      <div className="row between" style={{ alignItems: 'center' }}>
        <b className="mono" style={{ fontSize: 13 }}>{fname}</b>
        <span className="row" style={{ gap: 10, alignItems: 'center' }}>
          <EditStamp meta={meta} />
          <CustomizedBadge on={overridden} />
        </span>
      </div>
      {description && <p className="muted" style={{ fontSize: 12.5, margin: '6px 0 0' }}>{description}</p>}
      <textarea rows={16} spellCheck={false} value={text} onChange={(e) => setText(e.target.value)}
        style={{ width: '100%', resize: 'vertical', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 12.5, lineHeight: 1.6, marginTop: 10 }} />
      <div className="row between" style={{ alignItems: 'center' }}>
        <EditorFooter dirty={dirty} overridden={overridden} busy={busy}
          saved="Saved — every writer reads this on the next generation run."
          onSave={() => onSave({ kind: 'knowledge', key: fname, content: text })}
          onReset={() => onReset({ kind: 'knowledge', key: fname }, `restore ${fname} to the committed default`)} />
        <span className="muted" style={{ fontSize: 11.5 }}>{text.length.toLocaleString()} characters</span>
      </div>
    </div>
  )
}

function KnowledgeTab({ config, ins, busy, onSave, onReset, version }) {
  return (
    <>
      <p className="muted" style={{ fontSize: 13, maxWidth: 720, marginTop: 0 }}>
        The knowledge base is the single source of truth every writer is grounded in: the offer,
        the proof it may cite, and the email recipe. Edits apply to the next generation run.
        The guardrail linter is not editable — copy that breaks the ban list or claim discipline
        still fails before enrollment, whatever is written here.
      </p>
      <DocEditor fname="offer.md" doc={ins.knowledge?.['offer.md']}
        description="The offer, the 10/20/70 story, the proof with attribution rules, objections, and the ban list."
        meta={ins.meta?.['knowledge:offer.md']} busy={busy} onSave={onSave} onReset={onReset} version={version} />
      <DocEditor fname="icp-email.md" doc={ins.knowledge?.['icp-email.md']}
        description="The email recipe: subject rule, the 4-touch structure, formatting, and the hard guardrails."
        meta={ins.meta?.['knowledge:icp-email.md']} busy={busy} onSave={onSave} onReset={onReset} version={version} />
      <div className="section-h">Guardrails (not editable)</div>
      <GuardrailsSection data={config?.guardrails} />
    </>
  )
}

// ---- the studio -------------------------------------------------------------
export default function AgentStudio({ config, activeTab, onTab, innerRef }) {
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

  const customizedCount = ins ? (
    Object.keys(ins.personas?.overrides || {}).length
    + Object.keys(ins.plays?.overrides || {}).length
    + Object.values(ins.knowledge || {}).filter((d) => d.override != null).length
    + ((Object.keys(ins.icp?.include || {}).length || (ins.icp?.exclude || []).length) ? 1 : 0)
  ) : 0

  return (
    <div ref={innerRef}>
      <div className="row between" style={{ marginTop: 26, marginBottom: 4, alignItems: 'baseline' }}>
        <div className="section-h" style={{ margin: 0 }}>Agent studio — how the AI SDR thinks</div>
        {customizedCount > 0 && (
          <span className="muted" style={{ fontSize: 12 }}>{customizedCount} customization{customizedCount === 1 ? '' : 's'} active</span>
        )}
      </div>
      <p className="muted" style={{ fontSize: 12.5, maxWidth: 760, margin: '0 0 14px' }}>
        Read and edit the agents' operating instructions in plain language. Edits are stored
        separately from the code, apply from the next generation run, and can always be reset to
        the committed default. The copy linter and suppression gates are never affected.
      </p>

      <div className="panel">
        <div className="tabs" style={{ marginBottom: 18 }}>
          {STUDIO_TABS.map((t) => (
            <button key={t.id} className={'tab' + (activeTab === t.id ? ' active' : '')} onClick={() => onTab(t.id)}>
              {t.label}
            </button>
          ))}
        </div>

        <ErrorBanner error={error} />
        {note && <div className={`banner ${note.tone}`} style={{ marginBottom: 14 }}>{note.text}</div>}
        {!ins && !error && <Spinner label="Loading agent instructions…" />}

        {ins && activeTab === 'overview' && (
          <>
            <PipelineSection data={config?.pipeline} />
            <div className="section-h">Signal intelligence</div>
            <SignalsSection data={config?.signals} />
            <div className="section-h">Guardrails</div>
            <GuardrailsSection data={config?.guardrails} />
          </>
        )}
        {ins && activeTab === 'icp' && (
          <IcpTab key={version} config={config} ins={ins} busy={busy} onSave={save} onReset={reset} />
        )}
        {ins && activeTab === 'personas' && (
          <PersonasTab ins={ins} busy={busy} onSave={save} onReset={reset} version={version} />
        )}
        {ins && activeTab === 'sequencing' && (
          <SequencingTab ins={ins} busy={busy} onSave={save} onReset={reset} version={version} />
        )}
        {ins && activeTab === 'knowledge' && (
          <KnowledgeTab config={config} ins={ins} busy={busy} onSave={save} onReset={reset} version={version} />
        )}
      </div>
    </div>
  )
}
