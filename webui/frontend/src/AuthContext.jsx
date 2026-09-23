import { createContext, useContext, useEffect, useState } from 'react'
import { api, getToken, setToken, setOnUnauthorized } from './api'

// Minimal auth state for the SDR Console login gate. The token lives in
// localStorage (owned by api.js); the email and role are kept alongside it —
// the role only decides whether the Admin nav item renders, never whether an
// action is allowed: the server re-checks it on every /api/admin/* call.
const EMAIL_KEY = 'sdr_auth_email'
const ROLE_KEY = 'sdr_auth_role'
const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  // Initialize synchronously from localStorage so an already-signed-in user
  // never sees a one-frame flash of the login screen on first paint.
  const [auth, setAuth] = useState(() => ({
    token: getToken(),
    email: localStorage.getItem(EMAIL_KEY) || '',
    role: localStorage.getItem(ROLE_KEY) || '',
  }))

  function logout() {
    setToken(null)
    localStorage.removeItem(EMAIL_KEY)
    localStorage.removeItem(ROLE_KEY)
    setAuth({ token: null, email: '', role: '' })
  }

  async function login(email, password) {
    const res = await api.login(email, password)
    setToken(res.token)
    localStorage.setItem(EMAIL_KEY, res.email)
    localStorage.setItem(ROLE_KEY, res.role || 'member')
    setAuth({ token: res.token, email: res.email, role: res.role || 'member' })
    return res
  }

  // When the server rejects our token mid-session, fall back to the login screen.
  useEffect(() => {
    setOnUnauthorized(logout)
    return () => setOnUnauthorized(null)
  }, [])

  // Re-read the role from the server on mount: a session that predates the role
  // (or whose role an admin has since changed) has a stale/absent cached value.
  useEffect(() => {
    if (!auth.token) return
    api.me().then((me) => {
      localStorage.setItem(ROLE_KEY, me.role || 'member')
      setAuth((a) => (a.role === me.role ? a : { ...a, role: me.role || 'member' }))
    }).catch(() => { /* offline or 401 — the 401 handler already signs us out */ })
  }, [auth.token])

  return (
    <AuthContext.Provider value={{ ...auth, isAdmin: auth.role === 'admin', login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
