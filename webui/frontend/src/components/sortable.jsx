import { useMemo, useState } from 'react'

// Sortable tables — shared header cell + client-side sort hook.
// sort = { key, dir: 'asc' | 'desc' } | null (null = the data's own order).

// Sortable header cell. The control is a native button (keyboard-operable);
// aria-sort stays on the th.
export function SortTh({ label, k, sort, onSort, width, style }) {
  const active = sort?.key === k
  return (
    <th style={{ width, whiteSpace: 'nowrap', ...style }}
      aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : undefined}>
      <button type="button" className="th-sort" onClick={() => onSort(k)}
        title={`Sort by ${label.toLowerCase()}`}>
        {label}{active ? (sort.dir === 'asc' ? ' ▲' : ' ▼') : ''}
      </button>
    </th>
  )
}

// Click cycle: ascending → descending → back to the data's own order.
export function nextSort(s, k) {
  if (!s || s.key !== k) return { key: k, dir: 'asc' }
  return s.dir === 'asc' ? { key: k, dir: 'desc' } : null
}

// Empty values (null / undefined / '') always sink to the bottom regardless of
// direction; numbers compare numerically, everything else case-insensitively.
export function compareValues(av, bv, dir) {
  const ae = av == null || av === '', be = bv == null || bv === ''
  if (ae || be) return ae && be ? 0 : ae ? 1 : -1
  const sign = dir === 'desc' ? -1 : 1
  if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sign
  if (typeof av === 'boolean' && typeof bv === 'boolean') return (Number(av) - Number(bv)) * sign
  return String(av).localeCompare(String(bv), undefined, { sensitivity: 'base', numeric: true }) * sign
}

// Client-side sort for a table's rows. `accessors` maps a column key to a
// function returning the value to sort by (default: row[key]) — use it when the
// cell shows a derived value. Returns { rows, sort, onSort } for SortTh.
export function useSort(rows, accessors = {}) {
  const [sort, setSort] = useState(null)
  const sorted = useMemo(() => {
    const list = rows || []
    if (!sort) return list
    const get = accessors[sort.key] || ((r) => r?.[sort.key])
    return [...list].sort((a, b) => compareValues(get(a), get(b), sort.dir))
  }, [rows, sort])  // eslint-disable-line react-hooks/exhaustive-deps
  return { rows: sorted, sort, onSort: (k) => setSort((s) => nextSort(s, k)) }
}
