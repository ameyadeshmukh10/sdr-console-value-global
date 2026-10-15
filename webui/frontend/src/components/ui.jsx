// Small shared presentational helpers used across pages.

export function Badge({ kind, value }) {
  if (!value) return <span className="muted">—</span>
  return <span className={`badge ${kind}-${value}`}>{value}</span>
}

export function Spinner({ label }) {
  return (
    <span className="row" style={{ gap: 8 }}>
      <span className="spinner" />{label && <span className="muted">{label}</span>}
    </span>
  )
}

// `accent` adds a brand-gradient left rule; `tone` ("good" | "warn" | "bad")
// colors the value. Both are optional and default to the existing plain look.
export function Stat({ label, value, sub, accent = false, tone }) {
  return (
    <div className={'stat' + (accent ? ' accent' : '')} {...(tone ? { 'data-tone': tone } : {})}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub != null && <div className="sub">{sub}</div>}
    </div>
  )
}

// Import-time lead-quality flags (csv_audience.lead_flags). OFF_ICP_FLAGS
// mirrors the server's OFF_ICP_FLAGS: outside US/CA, <1,000 employees,
// healthcare — shown amber; the rest are informational (muted).
export const OFF_ICP_FLAGS = ['non_us_ca', 'country_inferred', 'below_icp_floor', 'healthcare']
export const FLAG_LABELS = {
  non_us_ca: 'outside US/CA',
  country_inferred: 'non-US/CA domain',
  below_icp_floor: '<1,000 employees',
  healthcare: 'healthcare',
  country_unknown: 'no country',
  no_linkedin: 'no LinkedIn',
}

export function flagLabel(flag) {
  const [key, extra] = flag.split(':')
  if (key === 'country_inferred' && extra) return `likely ${extra.toUpperCase()} (domain)`
  return FLAG_LABELS[key] || key.replace(/_/g, ' ')
}

export function FlagChips({ flags, country }) {
  if (!flags || !flags.length) return <span className="muted">—</span>
  return (
    <span className="row" style={{ gap: 4, flexWrap: 'wrap' }}>
      {flags.map((f) => {
        const off = OFF_ICP_FLAGS.includes(f.split(':')[0])
        const label = f === 'non_us_ca' && country ? `${country}` : flagLabel(f)
        return (
          <span key={f} className="badge" title={f === 'non_us_ca' ? 'outside US/Canada' : 'import-time quality flag'}
            style={{ fontSize: 10, ...(off ? { color: 'var(--amber)', borderColor: 'var(--amber)' } : { color: 'var(--muted)' }) }}>
            {label}
          </span>
        )
      })}
    </span>
  )
}

export function ErrorBanner({ error }) {
  if (!error) return null
  return <div className="banner error">{String(error)}</div>
}

// Channel icons shared by the Analytics section headers and the Orchestration
// diagram. Both render inside an <svg> (they are <g> groups, not elements).
export const LINKEDIN_BLUE = '#0a66c2'

export function EmailIcon({ x, y, color = 'var(--muted)' }) {
  return (
    <g transform={`translate(${x},${y})`}>
      <rect x="0" y="0" width="16" height="12" rx="2" fill="none" stroke={color} strokeWidth="1.4" />
      <path d="M1,1 L8,7 L15,1" fill="none" stroke={color} strokeWidth="1.4" />
    </g>
  )
}

export function LinkedInIcon({ x, y, size = 16 }) {
  return (
    <g transform={`translate(${x},${y})`}>
      <rect x="0" y="0" width={size} height={size} rx="3" fill={LINKEDIN_BLUE} />
      <text x={size / 2} y={size - 4} textAnchor="middle" fill="#fff" fontSize={size - 5} fontWeight="700"
        fontFamily="Georgia, serif">in</text>
    </g>
  )
}

export function pct(v) {
  return v == null ? '—' : `${v}%`
}

export function num(v) {
  return v == null ? '—' : v.toLocaleString()
}
