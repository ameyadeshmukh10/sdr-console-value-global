import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { ErrorBanner, num } from '../components/ui.jsx'
import StudioCanvas from '../components/StudioCanvas.jsx'
import StudioPane, { tabForRow } from '../components/StudioPane.jsx'
import { useInstructions, customized, PERSONA_ORDER, SEGMENT_ORDER } from '../components/StudioEditors.jsx'

// The Orchestration studio: the pipeline drawn as a node canvas. Click any
// node (or a branch row inside one) and its inspector slides in from the
// right — a tabbed pane where a non-technical operator reads and EDITS how
// the agents think: ICP keywords, persona framing, trigger plays, knowledge.
// Edits are a volume-backed override layer; the linter is never editable.

export default function DiagramPage() {
  const [config, setConfig] = useState(null)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)   // {id, tab}
  const [unenroll, setUnenroll] = useState(null)
  const [runBusy, setRunBusy] = useState(false)
  const [runMsg, setRunMsg] = useState(null)
  const store = useInstructions()

  useEffect(() => {
    api.orchestrationConfig().then(setConfig).catch((e) => setError(e.message))
    api.unenrollStatus().then(setUnenroll).catch(() => {})
  }, [])

  // Kick a sweep (or dry run), then poll until the run flag clears.
  async function runCheck(dryRun) {
    setRunBusy(true)
    setRunMsg(dryRun ? 'Starting dry run…' : 'Starting unenrollment check…')
    try {
      await api.unenrollRun({ dry_run: !!dryRun })
    } catch (e) {
      if (e.status !== 409) {   // 409 = already running; attach to it
        setRunMsg(`Unenrollment check failed to start: ${e.message}`)
        setRunBusy(false)
        return
      }
    }
    setRunMsg('Running — sweeping flagged contacts across both channels…')
    for (let i = 0; i < 120; i++) {          // up to ~10 min
      await new Promise((r) => setTimeout(r, 5000))
      try {
        const s = await api.unenrollStatus()
        if (s.running && s.progress) setRunMsg(`Running — ${s.progress}`)
        if (!s.running) {
          setUnenroll(s)
          if (dryRun) {
            const d = s.last_result
            setRunMsg(d && typeof d === 'object' && d.dry_run
              ? (d.ok === false
                  ? `Dry run failed: ${d.error || (d.errors || []).join('; ') || 'unknown'}`
                  : `Dry run complete — ${num(d.checked || 0)} checked, `
                    + `${num((d.bison?.stopped || 0) + (d.heyreach?.stopped || 0))} would be stopped. No changes made.`)
              : 'Dry run complete — no changes made.')
          } else {
            const lr = s.rules?.[0]?.last_run
            const sum = lr?.summary
            setRunMsg(lr?.ok === false
              ? `Finished with an error: ${typeof sum === 'string' ? sum : (sum?.errors?.length ? sum.errors.join('; ') : (sum?.error || 'unknown'))}`
              : 'Check complete.')
          }
          setRunBusy(false)
          return
        }
      } catch { /* transient — keep polling */ }
    }
    setRunMsg('Still running — reopen this pane later.')
    setRunBusy(false)
  }

  // Customization dots on the canvas (node- and row-level).
  const dots = {}
  const ins = store.ins
  if (ins) {
    if (customized(ins, 'icp')) dots.icp = true
    PERSONA_ORDER.forEach((p) => { if (customized(ins, 'persona', p)) dots[`agent-${p}`] = true })
    SEGMENT_ORDER.forEach((s) => { if (customized(ins, 'play', s)) { dots.plays = true; dots[`plays:${s}`] = true } })
    Object.keys(ins.knowledge || {}).forEach((f) => {
      if (customized(ins, 'knowledge', f)) { dots.knowledge = true; dots[`knowledge:${f}`] = true }
    })
  }
  const nCustom = Object.keys(dots).filter((k) => !k.includes(':')).length

  const select = (id, rowKey) => setSelected({ id, tab: tabForRow(id, rowKey) })

  return (
    <div>
      <div className="row between" style={{ alignItems: 'baseline', flexWrap: 'wrap' }}>
        <h1 className="page-title">Orchestration</h1>
        {nCustom > 0 && (
          <span className="muted" style={{ fontSize: 12.5 }}>
            ● {nCustom} node{nCustom === 1 ? '' : 's'} customized
          </span>
        )}
      </div>
      <p className="page-sub">
        The pipeline as it runs, node by node. Click any node — or a branch inside one — to open
        its inspector and edit how that piece thinks, in plain language. Edits apply from the
        next generation run and can always be reset; the copy linter is never editable.
      </p>
      <ErrorBanner error={error || store.error} />

      <StudioCanvas selected={selected?.id} onSelect={select} customDots={dots} />

      {selected && (
        <StudioPane nodeId={selected.id} initialTab={selected.tab}
          onClose={() => setSelected(null)} onSelect={select}
          config={config} store={store}
          gate={{ unenroll, busy: runBusy, msg: runMsg, onRun: runCheck }} />
      )}
    </div>
  )
}
