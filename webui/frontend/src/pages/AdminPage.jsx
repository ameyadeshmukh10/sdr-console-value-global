import { Fragment, useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../AuthContext.jsx'
import { Spinner, ErrorBanner } from '../components/ui.jsx'

// Admin — console logins. Create an email + password sign-in for a teammate,
// reset a password, change a role, or remove a login.
//
// Two kinds of account show up here:
//   * BUILT-IN — hard-coded in webui/server/app.py, always admins, read-only
//     (a code change is the only way to alter them; that's what keeps a bad
//     edit here from locking everyone out of this view).
//   * Managed — created on this page; they live in data/outreach/users.json on
//     the volume as salted PBKDF2 digests, never as plaintext.
//
// This page is only reachable by an admin, but that is convenience, not
// security: the server re-checks the caller's role on every /api/admin/* call.

const fmtWhen = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—')

const ROLE_HELP = {
  admin: 'Full console access, and can manage logins on this page.',
  member: 'Full console access, but cannot manage logins.',
}

function CreateUserForm({ minPassword, onCreated }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [role, setRole] = useState('member')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const mismatch = confirm.length > 0 && password !== confirm
  const tooShort = password.length > 0 && password.length < minPassword
  const ready = email.trim() && password.length >= minPassword && password === confirm

  async function submit(e) {
    e.preventDefault()
    if (busy || !ready) return
    setError(null)
    setBusy(true)
    try {
      const res = await api.adminCreateUser(email.trim(), password, role)
      setEmail(''); setPassword(''); setConfirm(''); setRole('member')
      onCreated(`Created ${res.user.email} as ${res.user.role}. They can sign in now.`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="panel">
      <div className="section-h" style={{ marginTop: 0 }}>Create a login</div>
      <form onSubmit={submit} autoComplete="off">
        <ErrorBanner error={error} />
        <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', alignItems: 'start' }}>
          <label className="field">
            Email
            <input
              type="email" placeholder="teammate@everworker.ai" required autoComplete="off"
              value={email} onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label className="field">
            Password
            <input
              type="password" placeholder={`at least ${minPassword} characters`} required
              autoComplete="new-password"
              value={password} onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <label className="field">
            Confirm password
            <input
              type="password" placeholder="repeat it" required autoComplete="new-password"
              value={confirm} onChange={(e) => setConfirm(e.target.value)}
            />
          </label>
          <label className="field">
            Role
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="member">Member</option>
              <option value="admin">Admin</option>
            </select>
          </label>
        </div>
        <div className="row between" style={{ marginTop: 16, alignItems: 'center', gap: 14 }}>
          <span className="muted" style={{ fontSize: 12.5 }}>
            {tooShort ? `Passwords must be at least ${minPassword} characters.`
              : mismatch ? 'The two passwords don’t match.'
                : ROLE_HELP[role]}
          </span>
          <button type="submit" disabled={busy || !ready}>
            {busy ? 'Creating…' : 'Create login'}
          </button>
        </div>
      </form>
      <div className="muted" style={{ fontSize: 12, marginTop: 14, lineHeight: 1.6 }}>
        Give the person their password over a channel you trust — the console never shows it
        again (only a salted hash is stored) and there is no self-serve reset. If they lose
        it, set a new one from the table below.
      </div>
    </div>
  )
}

function PasswordPrompt({ email, minPassword, onDone, onCancel }) {
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const ready = password.length >= minPassword && password === confirm

  async function submit(e) {
    e.preventDefault()
    if (busy || !ready) return
    setError(null)
    setBusy(true)
    try {
      await api.adminSetPassword(email, password)
      onDone(`New password set for ${email}.`)
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }} autoComplete="off">
      <input
        type="password" autoFocus placeholder={`new password (${minPassword}+)`}
        autoComplete="new-password" style={{ width: 190 }}
        value={password} onChange={(e) => setPassword(e.target.value)}
      />
      <input
        type="password" placeholder="confirm" autoComplete="new-password" style={{ width: 150 }}
        value={confirm} onChange={(e) => setConfirm(e.target.value)}
      />
      <button className="sm" type="submit" disabled={busy || !ready}>{busy ? 'Saving…' : 'Save'}</button>
      <button className="sm ghost" type="button" onClick={onCancel} disabled={busy}>Cancel</button>
      {error && <span style={{ color: 'var(--red)', fontSize: 12 }}>{error}</span>}
    </form>
  )
}

export default function AdminPage() {
  const { email: me } = useAuth()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [resetting, setResetting] = useState(null)   // email whose password row is open
  const [busyRow, setBusyRow] = useState(null)

  function load() {
    api.adminUsers()
      .then((d) => { setData(d); setError(null) })
      .catch((e) => setError(e.message))
  }
  useEffect(load, [])

  function after(msg) {
    setNotice(msg)
    setResetting(null)
    setBusyRow(null)
    load()
  }

  async function act(email, fn, msg) {
    setBusyRow(email)
    setError(null)
    setNotice(null)
    try {
      await fn()
      after(msg)
    } catch (e) {
      setError(e.message)
      setBusyRow(null)
    }
  }

  function changeRole(user, role) {
    act(user.email, () => api.adminSetRole(user.email, role), `${user.email} is now ${role}.`)
  }

  function remove(user) {
    if (!window.confirm(`Delete the login ${user.email}? They lose access immediately.`)) return
    act(user.email, () => api.adminDeleteUser(user.email), `Deleted ${user.email}.`)
  }

  const users = data?.users || []
  const minPassword = data?.min_password || 10

  return (
    <>
      <div className="page-title">Admin</div>
      <div className="page-sub">
        Who can sign in to this console. Create an email + password login for a teammate, reset
        a password, or remove access.
      </div>

      <ErrorBanner error={error} />
      {notice && <div className="banner info">{notice}</div>}

      <div className="grid" style={{ gap: 18 }}>
        <CreateUserForm minPassword={minPassword} onCreated={after} />

        <div className="panel" style={{ padding: 0, overflow: 'hidden' }}>
          <div className="section-h" style={{ margin: 0, padding: '18px 24px 12px' }}>
            Logins {data && <span className="muted">· {users.length}</span>}
          </div>
          {!data && !error && <div style={{ padding: '0 24px 22px' }}><Spinner label="Loading logins…" /></div>}
          {data && (
            <table>
              <thead>
                <tr>
                  <th>Email</th>
                  <th>Role</th>
                  <th>Created</th>
                  <th>Password set</th>
                  <th style={{ textAlign: 'right' }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => {
                  const isMe = u.email === me
                  const locked = u.builtin
                  const busy = busyRow === u.email
                  return (
                    <Fragment key={u.email}>
                    <tr>
                      <td>
                        {u.email}
                        {isMe && <span className="muted" style={{ fontSize: 11.5 }}> · you</span>}
                        {locked && <span className="badge" style={{ marginLeft: 8 }}>built-in</span>}
                      </td>
                      <td>
                        {locked || isMe ? (
                          <span className={u.role === 'admin' ? 'badge cta' : 'badge'}>{u.role}</span>
                        ) : (
                          <select value={u.role} disabled={busy}
                            onChange={(e) => changeRole(u, e.target.value)}
                            style={{ padding: '4px 8px', fontSize: 13 }}>
                            <option value="member">member</option>
                            <option value="admin">admin</option>
                          </select>
                        )}
                      </td>
                      <td className="muted">{locked ? 'in code' : fmtWhen(u.created_at)}</td>
                      <td className="muted">{locked ? '—' : fmtWhen(u.password_changed_at)}</td>
                      <td style={{ textAlign: 'right' }}>
                        {locked ? (
                          <span className="muted" style={{ fontSize: 12 }}>managed in code</span>
                        ) : (
                          <div className="row" style={{ gap: 8, justifyContent: 'flex-end' }}>
                            <button className="sm ghost" disabled={busy || resetting === u.email}
                              onClick={() => { setNotice(null); setResetting(u.email) }}>
                              Set password
                            </button>
                            <button className="sm ghost" disabled={busy || isMe}
                              title={isMe ? 'You cannot delete your own login' : ''}
                              onClick={() => remove(u)}>
                              Delete
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                    {/* Own row, so opening it never reflows the table's columns. */}
                    {resetting === u.email && (
                      <tr>
                        <td colSpan={5} style={{ background: 'var(--panel-2)' }}>
                          <div className="row" style={{ gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
                            <span className="muted" style={{ fontSize: 12.5 }}>
                              New password for <b>{u.email}</b>
                            </span>
                            <PasswordPrompt
                              email={u.email} minPassword={minPassword}
                              onDone={after} onCancel={() => setResetting(null)}
                            />
                          </div>
                        </td>
                      </tr>
                    )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          )}
          <div className="muted" style={{ fontSize: 12, padding: '14px 24px 18px', lineHeight: 1.6 }}>
            Built-in logins are defined in the server code and can only be changed there — that is
            what stops an edit here from locking everyone out. You cannot change your own role or
            delete your own login; ask another admin.
          </div>
        </div>
      </div>
    </>
  )
}
