import { BRAND } from '../theme.js'

// The Orchestration studio canvas: the live pipeline drawn as a node graph —
// soft white cards with pastel icon tiles, branch rows inside nodes, and thin
// curved connectors on a dotted grid. Clicking a node (or a row inside one)
// opens the inspector pane on the right, where the node is read and edited.
// Layout is static: this graph IS the architecture, it does not rearrange.

const ICO = {
  width: 15, height: 15, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round',
}
const ICONS = {
  start: <svg {...ICO}><polygon points="6 4 20 12 6 20 6 4" /></svg>,
  end: <svg {...ICO}><rect x="6" y="6" width="12" height="12" rx="2" /></svg>,
  guard: <svg {...ICO}><path d="M12 3 5 6v5c0 4.5 3 8 7 10 4-2 7-5.5 7-10V6z" /><path d="m9.2 12 2 2 3.6-4" /></svg>,
  router: <svg {...ICO}><path d="M4 12h6" /><path d="M10 12c4 0 4-6 8-6M10 12c4 0 4 6 8 6" /><path d="m15 3 3 3-3 3M15 15l3 3-3 3" /></svg>,
  agent: <svg {...ICO}><path d="M17 3.7a2.4 2.4 0 0 1 3.4 3.4L8 19.5 3.6 20.4l.9-4.4z" /></svg>,
  mail: <svg {...ICO}><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></svg>,
  signal: <svg {...ICO}><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" /></svg>,
  play: <svg {...ICO}><path d="m12 3 9 5-9 5-9-5z" /><path d="m3 13 9 5 9-5" /></svg>,
  book: <svg {...ICO}><path d="M4 5a2 2 0 0 1 2-2h14v18H6a2 2 0 0 0-2 2z" /><path d="M20 17H6a2 2 0 0 0-2 2" /></svg>,
  stop: <svg {...ICO}><circle cx="12" cy="12" r="9" /><path d="m5.6 5.6 12.8 12.8" /></svg>,
}

// Pastel icon tiles per node family (the reference's color language).
const TILES = {
  start: { bg: 'rgba(82, 254, 191, 0.30)', fg: BRAND.jadeDeep },
  end: { bg: 'rgba(15, 28, 24, 0.07)', fg: 'rgba(15, 28, 24, 0.55)' },
  guard: { bg: 'rgba(226, 178, 74, 0.28)', fg: '#8a6116' },
  router: { bg: 'rgba(226, 178, 74, 0.28)', fg: '#8a6116' },
  agent: { bg: 'rgba(109, 93, 211, 0.14)', fg: BRAND.violet },
  email: { bg: 'rgba(34, 130, 111, 0.14)', fg: BRAND.jade },
  linkedin: { bg: 'rgba(10, 102, 194, 0.13)', fg: '#0a66c2' },
  signal: { bg: 'rgba(51, 182, 144, 0.18)', fg: BRAND.jadeDeep },
  play: { bg: 'rgba(226, 178, 74, 0.28)', fg: '#8a6116' },
  book: { bg: 'rgba(47, 109, 181, 0.13)', fg: '#2f6db5' },
  stop: { bg: 'rgba(220, 38, 38, 0.10)', fg: BRAND.red },
}

// ---- geometry ---------------------------------------------------------------
const HEAD = 52     // node header height
const ROW = 27      // branch row height (incl. its 4px gap)
const PAD = 8       // rows bottom padding
const nodeH = (n) => (n.rows ? HEAD + n.rows.length * ROW + PAD : (n.small ? 52 : 58))
const headCY = (n) => n.y + (n.rows || !n.small ? 26 : 26)
const rowCY = (n, i) => n.y + HEAD + i * ROW + ROW / 2 - 2

export const NODES = [
  { id: 'start', kind: 'start', x: 30, y: 298, w: 150, small: true,
    title: 'Start', sub: 'contact imported' },
  { id: 'icp', kind: 'guard', x: 240, y: 260, w: 230,
    title: 'ICP filter', sub: 'Guardrail · buyer group',
    rows: [{ k: 'pass', label: 'Pass' }, { k: 'fail', label: 'Fail' }] },
  { id: 'endnot', kind: 'end', x: 544, y: 425, w: 186, small: true,
    title: 'End', sub: 'not contacted' },
  { id: 'router', kind: 'router', x: 544, y: 196, w: 250,
    title: 'Persona router', sub: 'routes by job title',
    rows: [
      { k: 'erp-owner', label: 'ERP & application owner' },
      { k: 'dba', label: 'Database owner' },
      { k: 'data-governance', label: 'Data governance' },
      { k: 'it-leadership', label: 'IT leadership' },
    ] },
  { id: 'agent-erp-owner', kind: 'agent', x: 876, y: 78, w: 250, title: 'ERP owner agent', sub: 'Agent' },
  { id: 'agent-dba', kind: 'agent', x: 876, y: 174, w: 250, title: 'DBA agent', sub: 'Agent' },
  { id: 'agent-data-governance', kind: 'agent', x: 876, y: 270, w: 250, title: 'Data governance agent', sub: 'Agent' },
  { id: 'agent-it-leadership', kind: 'agent', x: 876, y: 366, w: 250, title: 'IT leadership agent', sub: 'Agent' },
  { id: 'lint', kind: 'guard', x: 1208, y: 232, w: 230,
    title: 'Copy linter', sub: 'Guardrail · every email',
    rows: [{ k: 'pass', label: 'Pass' }, { k: 'fail', label: 'Fail · retry or edit' }] },
  { id: 'email', kind: 'email', x: 1518, y: 170, w: 230, title: 'Email', sub: 'Email Bison campaigns' },
  { id: 'linkedin', kind: 'linkedin', x: 1518, y: 300, w: 230, title: 'LinkedIn', sub: 'HeyReach · deferred' },
  { id: 'suppress', kind: 'stop', x: 1518, y: 452, w: 230, dashed: true,
    title: 'Unenrollment checker', sub: 'safety gate · sweeps' },
  { id: 'signals', kind: 'signal', x: 544, y: 560, w: 250, dashed: true,
    title: 'Signal intelligence', sub: 'feeds the copy',
    rows: [{ k: 'tech', label: 'Technographics' }, { k: 'hiring', label: 'Hiring' }, { k: 'news', label: 'ERP news triggers' }] },
  { id: 'plays', kind: 'play', x: 876, y: 560, w: 250, dashed: true,
    title: 'Trigger plays', sub: 'anchor email 1',
    rows: [
      { k: 'ma_carveout', label: 'M&A carve-out' },
      { k: 'erp_migration', label: 'ERP migration' },
      { k: 'license_audit', label: 'License audit' },
      { k: 'ebs_oci', label: 'EBS on OCI' },
      { k: 'ebs_performance', label: 'EBS performance' },
    ] },
  { id: 'knowledge', kind: 'book', x: 1208, y: 560, w: 230, dashed: true,
    title: 'Knowledge base', sub: 'grounds every claim',
    rows: [{ k: 'offer.md', label: 'offer.md' }, { k: 'cta-offers.md', label: 'cta-offers.md' }, { k: 'icp-email.md', label: 'icp-email.md' }] },
]
const N = Object.fromEntries(NODES.map((n) => [n.id, n]))
export const NODE_BY_ID = N

const W = 1780
const H = 790

function curve(x1, y1, x2, y2) {
  const mx = (x1 + x2) / 2
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`
}
function vcurve(x1, y1, x2, y2) {
  const my = (y1 + y2) / 2
  return `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`
}

const right = (n, cy) => [n.x + n.w, cy]
const left = (n, cy) => [n.x, cy]

function edges() {
  const e = []
  const solid = (p) => e.push({ d: p, dash: false })
  const dashed = (p) => e.push({ d: p, dash: true })

  solid(curve(...right(N.start, headCY(N.start)), ...left(N.icp, headCY(N.icp))))
  solid(curve(...right(N.icp, rowCY(N.icp, 0)), ...left(N.router, headCY(N.router))))
  solid(curve(...right(N.icp, rowCY(N.icp, 1)), ...left(N.endnot, headCY(N.endnot))))
  const agents = ['agent-erp-owner', 'agent-dba', 'agent-data-governance', 'agent-it-leadership']
  agents.forEach((aid, i) => {
    solid(curve(...right(N.router, rowCY(N.router, i)), ...left(N[aid], headCY(N[aid]))))
    solid(curve(...right(N[aid], headCY(N[aid])), ...left(N.lint, headCY(N.lint))))
  })
  solid(curve(...right(N.lint, rowCY(N.lint, 0)), ...left(N.email, headCY(N.email))))
  solid(curve(...right(N.lint, rowCY(N.lint, 0)), ...left(N.linkedin, headCY(N.linkedin))))
  // feeding layer + safety gate (dashed)
  dashed(vcurve(N.signals.x + N.signals.w / 2, N.signals.y, N.router.x + N.router.w / 2, N.router.y + nodeH(N.router)))
  dashed(vcurve(N.plays.x + N.plays.w / 2, N.plays.y, N['agent-it-leadership'].x + N['agent-it-leadership'].w / 2, N['agent-it-leadership'].y + nodeH(N['agent-it-leadership'])))
  dashed(vcurve(N.knowledge.x + N.knowledge.w / 2, N.knowledge.y, N.lint.x + N.lint.w / 2, N.lint.y + nodeH(N.lint)))
  dashed(vcurve(N.suppress.x + N.suppress.w / 2, N.suppress.y, N.linkedin.x + N.linkedin.w / 2, N.linkedin.y + nodeH(N.linkedin)))
  return e
}
const EDGES = edges()

export function NodeTile({ kind, size = 30 }) {
  const t = TILES[kind] || TILES.agent
  return (
    <span className="tile" style={{ width: size, height: size, background: t.bg, color: t.fg }}>
      {ICONS[kind === 'email' ? 'mail' : kind === 'linkedin' ? 'mail' : kind] || ICONS.agent}
    </span>
  )
}

export default function StudioCanvas({ selected, onSelect, customDots = {} }) {
  return (
    <div className="studio-scroll">
      <div className="studio-canvas" style={{ width: W, height: H }}>
        <svg width={W} height={H} style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
          {EDGES.map((e, i) => (
            <path key={i} d={e.d} fill="none" stroke="rgba(15,28,24,0.20)" strokeWidth="1.5"
              strokeDasharray={e.dash ? '5 4' : undefined} />
          ))}
        </svg>
        {NODES.map((n) => (
          <div key={n.id}
            className={'snode' + (selected === n.id ? ' sel' : '') + (n.dashed ? ' dashed' : '') + (n.kind === 'end' ? ' quiet' : '')}
            style={{ left: n.x, top: n.y, width: n.w }}
            onClick={() => n.kind !== 'end' && onSelect(n.id)}
            role={n.kind !== 'end' ? 'button' : undefined}
            tabIndex={n.kind !== 'end' ? 0 : undefined}
            onKeyDown={(e) => { if (e.key === 'Enter' && n.kind !== 'end') onSelect(n.id) }}>
            <div className="hd">
              <NodeTile kind={n.kind} />
              <div>
                <div className="t">{n.title}{customDots[n.id] && <span className="cdot" title="customized" />}</div>
                {n.sub && <div className="s">{n.sub}</div>}
              </div>
            </div>
            {n.rows && (
              <div className="rows">
                {n.rows.map((r) => (
                  <div key={r.k} className="srow"
                    onClick={(e) => { e.stopPropagation(); onSelect(n.id, r.k) }}>
                    {r.label}{customDots[`${n.id}:${r.k}`] && <span className="cdot" title="customized" />}
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
