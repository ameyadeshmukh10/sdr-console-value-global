// Pages auto-refresh their data on open when it was never synced or was synced
// at least this long ago (Analytics stats + attribution, Replies inbox scan,
// Trends interested-reply analysis).
const AUTO_REFRESH_MS = 60 * 60 * 1000

// True when an ISO timestamp is missing, unparseable, or AUTO_REFRESH_MS old.
export const isStale = (iso) => {
  const t = iso ? new Date(iso).getTime() : NaN
  return Number.isNaN(t) || Date.now() - t >= AUTO_REFRESH_MS
}
