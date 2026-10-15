import { useEffect, useRef, useState } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend,
} from 'recharts'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num, pct, EmailIcon, LinkedInIcon } from '../components/ui.jsx'
import { SortTh, useSort } from '../components/sortable.jsx'
import { BRAND, TOOLTIP_STYLE } from '../theme.js'
import { isStale } from '../autoRefresh.js'

// Pillar 3 — Analytics: campaign performance from cached stats, refreshable live.

// Deal amounts are portal-currency; the console formats them as USD.
const usd = (v) => (v == null ? '—' : Number(v).toLocaleString('en-US', {
  style: 'currency', currency: 'USD', maximumFractionDigits: 0,
}))

export default function AnalyticsPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [li, setLi] = useState(null)   // LinkedIn analytics
  const [aisdr, setAisdr] = useState(null)  // AI SDR deal attribution (MongoDB)
  const [syncMsg, setSyncMsg] = useState(null)
  const [syncBusy, setSyncBusy] = useState(false)
  // Auto-refresh fires at most once per page open, per data source.
  const autoChecked = useRef({ stats: false, aisdr: false })

  function load() {
    return api.analytics().then((d) => { setData(d); setError(null); return d }).catch((e) => { setError(e.message); return null })
  }
  function loadAisdr() {
    return api.aisdrAnalytics().then((d) => { setAisdr(d); return d })
      .catch(() => { setAisdr({ configured: true, error: 'unreachable' }); return null })
  }
  useEffect(() => {
    load().then((d) => {
      if (autoChecked.current.stats) return
      autoChecked.current.stats = true
      if (d && isStale(d.fetched_at)) refresh()
    })
  }, [])
  useEffect(() => { api.linkedinAnalytics().then(setLi).catch(() => setLi({ error: 'unreachable' })) }, [])
  useEffect(() => {
    loadAisdr().then((d) => {
      if (autoChecked.current.aisdr) return
      autoChecked.current.aisdr = true
      // last_sync_at is stamped by every run, failed ones included — so a broken
      // config retries at most hourly instead of on every page open.
      if (d && d.configured !== false && !d.error && isStale(d.last_sync_at)) syncAisdr()
    })
  }, [])

  // Kick the HubSpot -> MongoDB attribution sync, then poll until it finishes
  // (the seed run takes a couple of minutes) and refresh the tiles.
  async function syncAisdr() {
    setSyncBusy(true)
    setSyncMsg('Starting attribution sync…')
    try {
      await api.aisdrSync()
    } catch (e) {
      // 409 = a sync is already running (e.g. the nightly job) — keep polling it.
      if (e.message !== '409') {
        setSyncMsg(`Attribution sync failed to start: ${e.message}`)
        setSyncBusy(false)
        return
      }
    }
    setSyncMsg('Attribution sync running — pulling emails, deals and contacts from HubSpot…')
    for (let i = 0; i < 90; i++) {           // up to ~7.5 min
      await new Promise((r) => setTimeout(r, 5000))
      try {
        const s = await api.aisdrSyncStatus()
        if (!s.running) {
          setSyncMsg(s.last_run_ok === false ? `Attribution sync finished with an error: ${s.last_error || 'unknown'}` : 'Attribution sync complete.')
          setSyncBusy(false)
          loadAisdr()
          return
        }
      } catch { /* transient — keep polling */ }
    }
    setSyncMsg('Attribution sync is still running — refresh the page later.')
    setSyncBusy(false)
  }

  async function refresh() {
    setRefreshing(true); setError(null)
    try {
      const d = await api.refreshAnalytics()
      setData(d)
      if (!d.ok) setError('Refresh script returned errors — showing latest cached data.')
    } catch (e) { setError(e.message) }
    finally { setRefreshing(false) }
  }

  // One button, both jobs: the email-stats snapshot refresh and the deal
  // attribution sync run concurrently.
  async function refreshAll() {
    const jobs = [refresh()]
    if (aisdr?.configured !== false && !syncBusy) jobs.push(syncAisdr())
    await Promise.allSettled(jobs)
  }

  const t = data?.totals
  // Only campaigns that actually sent are worth charting.
  const active = (data?.campaigns || []).filter((c) => (c.total_leads_contacted || 0) > 0)
  const chartData = active.map((c) => ({
    name: c.campaign_name?.slice(0, 18) || `#${c.campaign_id}`,
    'Reply %': c.reply_rate_pct || 0,
    'Interested %': c.interested_rate_pct || 0,
  }))

  const fetchedWhen = data?.fetched_at ? new Date(data.fetched_at).toLocaleString() : '—'

  return (
    <div>
      <div className="row between">
        <h1 className="page-title">Analytics</h1>
        <button onClick={refreshAll} disabled={refreshing || syncBusy}>
          {refreshing || syncBusy ? <Spinner label="Refreshing…" /> : '↻ Refresh'}
        </button>
      </div>

      <div className="banner info">Last Synced: <b>{fetchedWhen}</b></div>
      <ErrorBanner error={error} />

      {/* AI SDR deal attribution — nightly HubSpot -> MongoDB sync. Rendered above
          the email block so it works regardless of email-stats state. */}
      <div className="section-h" style={{ marginBottom: 8 }}>AI SDR pipeline</div>
      {syncMsg && <div className="banner info">{syncMsg}</div>}
      {/* Unconfigured = no HubSpot or no MongoDB: nothing to attribute, so no tiles. */}
      {aisdr?.configured === false ? (
        <div className="banner info" style={{ marginBottom: 24 }}>Deal attribution not configured.</div>
      ) : (
      <div className="grid stat-grid" style={{ marginBottom: 24 }}>
        <Stat
          accent
          label="Deals created by AI SDR"
          value={aisdr?.error ? '—' : num(aisdr?.deals_created)}
          sub={aisdr?.error ? `Attribution store unreachable: ${aisdr.error}` : null}
        />
        <Stat
          accent
          label="Total pipeline"
          value={aisdr?.error ? '—' : usd(aisdr?.total_pipeline)}
          sub={aisdr?.last_error
            ? `Last sync error: ${aisdr.last_error}`
            : aisdr?.last_sync_at
              ? `Synced ${new Date(aisdr.last_sync_at).toLocaleString()}`
              : 'No sync has run yet — click Refresh'}
        />
      </div>
      )}

      {/* Email channel */}
      <div className="row" style={{ gap: 8, alignItems: 'center', marginBottom: 10 }}>
        <svg width="18" height="14"><EmailIcon x={1} y={1} color={BRAND.jade} /></svg>
        <h2 className="section-h" style={{ margin: 0 }}>Email</h2>
      </div>
      {!data ? <Spinner label="Loading…" /> : (
        <div className="grid stat-grid" style={{ marginBottom: 24 }}>
          <Stat label="Total leads" value={num(t.total_leads)} />
          <Stat label="Contacted" value={num(t.total_contacted)} />
          <Stat label="Replies" value={num(t.total_replies)} sub={`${pct(t.overall_reply_rate_pct)} reply rate`} />
          <Stat label="Bounces" value={num(t.total_bounced)} sub={`${pct(t.overall_bounce_rate_pct)} bounce rate`} />
          <Stat label="Interested" value={num(t.total_interested)} sub={`${pct(t.overall_interested_rate_pct)} interested rate`} accent />
        </div>
      )}

      {/* LinkedIn channel */}
      <div className="row" style={{ gap: 8, alignItems: 'center' }}>
        <svg width="18" height="18"><LinkedInIcon x={1} y={1} /></svg>
        <h2 className="section-h" style={{ margin: 0 }}>LinkedIn</h2>
      </div>
      {!li ? <Spinner label="Loading LinkedIn…" />
        : li.configured === false ? (
          <div className="banner info" style={{ marginTop: 10 }}>LinkedIn analytics not configured.</div>
        ) : li.error ? (
          <div className="banner warn" style={{ marginTop: 10 }}>Couldn't load LinkedIn stats: {li.error}</div>
        ) : (() => {
          const s = li.stats || {}, f = li.funnel || {}
          const rate = (n, d) => (d ? (100 * n) / d : 0)
          const liActive = (f.totalUsersInProgress || 0) + (f.totalUsersPending || 0)
          return (
            <>
              <div className="banner info" style={{ marginTop: 10 }}>
                Campaign <b>{li.campaign_name || `#${li.campaign_id}`}</b> · <span className="badge">{li.status}</span> · <span className="mono">#{li.campaign_id}</span>
              </div>
              <div className="grid stat-grid" style={{ marginTop: 12 }}>
                <Stat label="Leads in campaign" value={num(f.totalUsers)} sub={`${num(f.totalUsersFinished)} finished · ${num(liActive)} active`} />
                <Stat label="Connections sent" value={num(s.connectionsSent)} sub={`${num(s.connectionsAccepted)} accepted · ${pct(rate(s.connectionsAccepted, s.connectionsSent))}`} />
                <Stat label="Messages sent" value={num(s.messagesSent)} sub={`${num(s.totalMessageReplies)} replies · ${pct(rate(s.totalMessageReplies, s.messagesSent))}`} />
                <Stat label="Interested (auto-tagged)" value={num(s.autoTaggedInterested)} sub={`${pct(rate(s.autoTaggedInterested, s.uniqueLeadsContacted))} of contacted`} accent />
              </div>
              {(s.connectionsSent || 0) === 0 && (
                <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
                  No LinkedIn activity yet — metrics populate once the campaign starts sending. (LinkedIn
                  has no email-style "reply rate" feed; these are native connection/message stats.)
                </p>
              )}
            </>
          )
        })()}

      {data && (
        <>
          {chartData.length > 0 && (
            <div className="panel" style={{ marginTop: 30, marginBottom: 24, height: 320 }}>
              <div className="section-h" style={{ marginTop: 0 }}>Reply vs interested rate by email campaign</div>
              <ResponsiveContainer width="100%" height="86%">
                <BarChart data={chartData} margin={{ top: 8, right: 16, bottom: 8, left: -8 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={BRAND.grid} />
                  <XAxis dataKey="name" tick={{ fill: BRAND.muted, fontSize: 11 }} interval={0} angle={-18} textAnchor="end" height={60} />
                  <YAxis tick={{ fill: BRAND.muted, fontSize: 11 }} unit="%" />
                  <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: 'rgba(15,28,24,0.04)' }} />
                  {/* Recharts 3 sorts legend items alphabetically; keep the bar order. */}
                  <Legend wrapperStyle={{ fontSize: 12 }} itemSorter={(it) => (it.dataKey === 'Reply %' ? 0 : 1)} />
                  <Bar dataKey="Reply %" fill={BRAND.jade} radius={[3, 3, 0, 0]} />
                  <Bar dataKey="Interested %" fill={BRAND.mint} radius={[3, 3, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          <h2 className="section-h" style={chartData.length > 0 ? undefined : { marginTop: 30 }}>Email campaigns</h2>
          <div className="panel" style={{ padding: 0 }}>
            <CampaignsTable campaigns={data.campaigns} />
          </div>

          {data.errors?.length > 0 && (
            <div className="banner warn" style={{ marginTop: 16 }}>
              {data.errors.length} campaign(s) returned errors during the last fetch (e.g. drafts without a sequence).
            </div>
          )}
        </>
      )}
    </div>
  )
}

function CampaignsTable({ campaigns }) {
  const { rows, sort, onSort } = useSort(campaigns, {
    campaign_name: (c) => c.campaign_name || `#${c.campaign_id}`,
  })
  return (
    <table>
      <thead>
        <tr>
          <SortTh label="Campaign" k="campaign_name" sort={sort} onSort={onSort} />
          <SortTh label="Status" k="status" sort={sort} onSort={onSort} />
          <SortTh label="Leads" k="total_leads" sort={sort} onSort={onSort} />
          <SortTh label="Contacted" k="total_leads_contacted" sort={sort} onSort={onSort} />
          <SortTh label="Replies" k="unique_replies" sort={sort} onSort={onSort} />
          <SortTh label="Reply %" k="reply_rate_pct" sort={sort} onSort={onSort} />
          <SortTh label="Bounces" k="bounced" sort={sort} onSort={onSort} />
          <SortTh label="Bounce %" k="bounce_rate_pct" sort={sort} onSort={onSort} />
          <SortTh label="Interested" k="interested" sort={sort} onSort={onSort} />
          <SortTh label="Interested %" k="interested_rate_pct" sort={sort} onSort={onSort} />
        </tr>
      </thead>
      <tbody>
        {rows.map((c) => (
          <tr key={c.campaign_id}>
            <td>{c.campaign_name || `#${c.campaign_id}`}</td>
            <td><span className="badge">{c.status}</span></td>
            <td>{num(c.total_leads)}</td>
            <td>{num(c.total_leads_contacted)}</td>
            <td>{num(c.unique_replies)}</td>
            <td>{pct(c.reply_rate_pct)}</td>
            <td>{num(c.bounced)}</td>
            <td>{pct(c.bounce_rate_pct)}</td>
            <td>{num(c.interested)}</td>
            <td>{pct(c.interested_rate_pct)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
