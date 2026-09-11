import { useEffect, useState } from 'react'
import { Spinner, ErrorBanner, Stat, num } from './ui.jsx'
import { NODE_BY_ID, NodeTile } from './StudioCanvas.jsx'
import {
  PERSONA_ORDER, SEGMENT_ORDER, customized,
  IcpRulesEditor, TitleTester, PersonaEditor, PersonaLockedDetails, PlayEditor, DocEditor,
} from './StudioEditors.jsx'
import {
  PipelineSection, IcpFilterSection, GuardrailsSection, SignalsSection,
} from './OrchestrationSections.jsx'

// The node inspector: a right slide-over pane with a horizontal tab bar as its
// primary nav. Every canvas node opens here; editable nodes host the studio
// editors, read-only nodes their live config.

const SEG_LABEL = {
  ma_carveout: 'M&A carve-out', erp_migration: 'ERP migration', license_audit: 'License audit',
  ebs_oci: 'EBS on OCI', ebs_performance: 'EBS performance',
}
const KNOWLEDGE_DESC = {
  'offer.md': 'The offer, the 10/20/70 story, the proof with attribution rules, objections, and the ban list.',
  'cta-offers.md': 'The offer ladder: the POV read, the free assessment, the 20-minute call, and the anti-patterns.',
  'icp-email.md': 'The email recipe: subject rule, the 4-touch structure, formatting, and the hard guardrails.',
}

// Tab lists per node. `row` keys from the canvas map to an initial tab id.
function tabsFor(nodeId) {
  if (nodeId === 'start') return [{ id: 'how', label: 'How it runs' }]
  if (nodeId === 'icp') {
    return [{ id: 'rules', label: 'Custom rules' }, { id: 'test', label: 'Try a title' },
            { id: 'builtin', label: 'Built-in rules' }]
  }
  if (nodeId === 'router') return [{ id: 'routing', label: 'Routing' }]
  if (nodeId.startsWith('agent-')) {
    return [{ id: 'framing', label: 'Framing' }, { id: 'locked', label: 'Fixed details' }]
  }
  if (nodeId === 'lint') return [{ id: 'guardrails', label: 'Guardrails' }]
  if (nodeId === 'signals') return [{ id: 'overview', label: 'Signal intelligence' }]
  if (nodeId === 'plays') return SEGMENT_ORDER.map((s) => ({ id: s, label: SEG_LABEL[s] }))
  if (nodeId === 'knowledge') {
    return ['offer.md', 'cta-offers.md', 'icp-email.md'].map((f) => ({ id: f, label: f }))
  }
  if (nodeId === 'email' || nodeId === 'linkedin') return [{ id: 'channel', label: 'Channel' }]
  if (nodeId === 'suppress') return [{ id: 'status', label: 'Status & controls' }]
  return [{ id: 'about', label: 'About' }]
}

// Which tab a canvas row click lands on.
export function tabForRow(nodeId, rowKey) {
  if (!rowKey) return tabsFor(nodeId)[0]?.id
  if (nodeId === 'plays' || nodeId === 'knowledge') return rowKey
  if (nodeId === 'icp') return 'rules'
  return tabsFor(nodeId)[0]?.id
}

export default function StudioPane({ nodeId, initialTab, onClose, onSelect, config, store, gate }) {
  const node = NODE_BY_ID[nodeId]
  const tabs = tabsFor(nodeId)
  const [tab, setTab] = useState(initialTab || tabs[0]?.id)
  useEffect(() => { setTab(initialTab || tabsFor(nodeId)[0]?.id) }, [nodeId, initialTab])
  const { ins, error, busy, note, version, save, reset } = store
  if (!node) return null

  const pid = nodeId.startsWith('agent-') ? nodeId.slice('agent-'.length) : null

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <div className="drawer studio-pane">
        <button className="close-x" onClick={onClose}>Close ✕</button>
        <div className="row" style={{ gap: 12, alignItems: 'center', marginBottom: 4 }}>
          <NodeTile kind={node.kind} size={34} />
          <div>
            <h2 className="page-title" style={{ fontSize: 21, margin: 0 }}>{node.title}</h2>
            <span className="muted" style={{ fontSize: 12.5 }}>{node.sub}</span>
          </div>
        </div>

        <div className="tabs" style={{ margin: '14px 0 16px' }}>
          {tabs.map((t) => (
            <button key={t.id} className={'tab' + (tab === t.id ? ' active' : '')} onClick={() => setTab(t.id)}>
              {t.label}
              {nodeId === 'plays' && customized(ins, 'play', t.id) && <span className="n">●</span>}
              {nodeId === 'knowledge' && customized(ins, 'knowledge', t.id) && <span className="n">●</span>}
            </button>
          ))}
        </div>

        <ErrorBanner error={error} />
        {note && <div className={`banner ${note.tone}`} style={{ marginBottom: 14 }}>{note.text}</div>}
        {!ins && !error && <Spinner label="Loading instructions…" />}

        {nodeId === 'start' && <PipelineSection data={config?.pipeline} />}

        {ins && nodeId === 'icp' && tab === 'rules' && (
          <IcpRulesEditor key={version} ins={ins} busy={busy} onSave={save} onReset={reset} />
        )}
        {nodeId === 'icp' && tab === 'test' && <TitleTester />}
        {nodeId === 'icp' && tab === 'builtin' && <IcpFilterSection data={config?.icp_filter} />}

        {nodeId === 'router' && (
          <>
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              Every ICP contact routes to one persona agent by job title — first match wins, in
              the order below. Custom keywords are managed on the ICP filter node.
            </p>
            <div className="grid" style={{ gap: 10 }}>
              {PERSONA_ORDER.map((p) => (
                <button key={p} className="ghost" style={{ textAlign: 'left', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}
                  onClick={() => onSelect(`agent-${p}`)}>
                  <span>{ins?.personas?.defaults?.[p]?.name || p}</span>
                  <span className="muted" style={{ fontSize: 12 }}>open agent →</span>
                </button>
              ))}
            </div>
            <div style={{ marginTop: 16 }}><IcpFilterSection data={config?.icp_filter} /></div>
          </>
        )}

        {ins && pid && tab === 'framing' && (
          <PersonaEditor key={pid + version} pid={pid}
            def={ins.personas?.defaults?.[pid]} ovr={ins.personas?.overrides?.[pid]}
            meta={ins.meta?.[`persona:${pid}`]} busy={busy} onSave={save} onReset={reset} />
        )}
        {ins && pid && tab === 'locked' && <PersonaLockedDetails def={ins.personas?.defaults?.[pid]} />}

        {nodeId === 'lint' && (
          <>
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              Deliberately not editable: every generated email passes these checks before it can
              enroll, whatever is written anywhere else in the studio.
            </p>
            <GuardrailsSection data={config?.guardrails} />
          </>
        )}

        {nodeId === 'signals' && <SignalsSection data={config?.signals} />}

        {ins && nodeId === 'plays' && SEGMENT_ORDER.includes(tab) && (
          <PlayEditor key={tab + version} seg={tab}
            def={ins.plays?.defaults?.[tab]} ovr={ins.plays?.overrides?.[tab]}
            meta={ins.meta?.[`play:${tab}`]} busy={busy} onSave={save} onReset={reset} />
        )}

        {ins && nodeId === 'knowledge' && ins.knowledge?.[tab] && (
          <DocEditor fname={tab} doc={ins.knowledge[tab]} description={KNOWLEDGE_DESC[tab]}
            meta={ins.meta?.[`knowledge:${tab}`]} busy={busy} onSave={save} onReset={reset}
            version={version} />
        )}

        {nodeId === 'email' && (
          <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
            Approved copy enrolls into per-persona Email Bison campaigns (subjects and bodies ride
            as custom variables; the campaign appends the signature). Enrollment always runs
            behind the dry-run → confirm gate on the Pipeline view, and the unenrollment checker
            can stop in-flight sequences at any time.
          </p>
        )}
        {nodeId === 'linkedin' && (
          <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
            LinkedIn copy (connection note + two messages) is generated for every contact and
            enrolls into HeyReach — currently deferred until the client's sender profiles are
            activated. The unenrollment checker covers this channel too.
          </p>
        )}

        {nodeId === 'suppress' && <SuppressionTab gate={gate} />}
      </div>
    </>
  )
}

// ---- unenrollment checker (status + manual sweep controls) -------------------
function lastRunLine(lastRun) {
  if (!lastRun || !lastRun.at) return 'No run yet — first sweep runs ~2 min after deploy.'
  const when = new Date(lastRun.at).toLocaleString()
  const s = lastRun.summary
  if (typeof s === 'string') return `Last run ${when} — ${lastRun.ok === false ? 'error' : 'ok'} · ${s}`
  if (lastRun.ok === false) {
    const why = s?.errors?.length ? s.errors.join('; ') : (s?.error || 'sweep failed')
    return `Last run ${when} — error · ${why}`
  }
  const stopped = (s?.bison?.stopped || 0) + (s?.heyreach?.stopped || 0)
  const detail = s ? ` · ${num(s.checked || 0)} checked, ${num(stopped)} stopped` : ''
  return `Last run ${when} — ok${detail}`
}

function SuppressionTab({ gate }) {
  const { unenroll, busy, msg, onRun } = gate
  if (!unenroll) return <Spinner label="Loading checker status…" />
  return (
    <>
      {(unenroll.rules || []).map((rule) => {
        const counts = rule.counts?.available ? rule.counts : null
        const byChan = counts?.by_channel_action || {}
        const lastSummary = (rule.last_run?.summary && typeof rule.last_run.summary === 'object')
          ? rule.last_run.summary : null
        const flagged = lastSummary?.flagged
        const topErr = counts?.top_errors?.[0]
        return (
          <div key={rule.id} style={{ marginBottom: 18 }}>
            <div className="row" style={{ gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
              <b style={{ fontSize: 14.5 }}>{rule.name}</b>
              <span className="badge" style={rule.enabled
                ? { color: 'var(--green)', borderColor: 'var(--green)' } : { color: 'var(--muted)' }}>
                {rule.enabled ? 'enabled' : 'disabled'}
              </span>
              {[['Email', rule.channels?.bison?.configured], ['LinkedIn', rule.channels?.heyreach?.configured]].map(([label, ok]) => (
                <span key={label} className="badge" style={ok ? undefined : { color: 'var(--muted)' }}>
                  {label} {ok ? '✓' : '— not configured'}
                </span>
              ))}
            </div>
            <p className="muted" style={{ fontSize: 12.5, margin: '8px 0 12px' }}>{rule.description}</p>
            {counts ? (
              <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 10, marginBottom: 12 }}>
                <Stat label="Flagged in HubSpot" value={flagged == null ? '—' : num(flagged)} />
                <Stat label="Contacts swept" value={num(counts.contacts)} />
                <Stat label="Stopped — email" value={num(byChan.bison?.stopped || 0)} />
                <Stat label="Stopped — LinkedIn" value={num(byChan.heyreach?.stopped || 0)} />
                <Stat label="Failed" value={num(counts.failed || 0)} tone={(counts.failed || 0) > 0 ? 'bad' : 'good'} />
              </div>
            ) : <p className="muted" style={{ fontSize: 12, margin: '0 0 12px' }}>No sweep results recorded yet.</p>}
            {topErr && (
              <p className="muted" style={{ fontSize: 12, margin: '0 0 12px' }}>
                Top failure ({num(topErr.n)}×): {String(topErr.error).slice(0, 200)}
              </p>
            )}
            <p className="muted" style={{ fontSize: 12, margin: '0 0 12px' }}>{lastRunLine(rule.last_run)}</p>
            <div className="row" style={{ gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
              <button className="sm" onClick={() => onRun(false)} disabled={busy}>
                {busy ? <Spinner label="Running…" /> : 'Run now'}
              </button>
              <button className="ghost sm" onClick={() => onRun(true)} disabled={busy}>Dry run</button>
            </div>
            {msg && <p className="muted" style={{ fontSize: 12, margin: '10px 0 0' }}>{msg}</p>}
            {!msg && unenroll.running && (
              <p className="muted" style={{ fontSize: 12, margin: '10px 0 0' }}>
                A sweep is running now{unenroll.progress ? ` — ${unenroll.progress}` : '…'}
              </p>
            )}
          </div>
        )
      })}
    </>
  )
}
