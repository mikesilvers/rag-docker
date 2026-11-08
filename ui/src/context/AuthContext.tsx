import { createContext, useCallback, useContext, useEffect, useState, ReactNode } from 'react'
import { api, ApiError, AuthUser, Role, onAuthProblem } from '../api/client'

// Who is logged in, as the API says (#25). The role comes only from
// GET /api/auth/me; the browser stores nothing about it.
type AuthState =
  | { phase: 'loading' }
  | { phase: 'error'; message: string }
  | { phase: 'setup_required' }
  | { phase: 'logged_out' }
  | { phase: 'logged_in'; user: AuthUser }

interface AuthValue {
  state: AuthState
  user: AuthUser | null
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  changePassword: (current: string, next: string) => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ phase: 'loading' })

  const refresh = useCallback(async () => {
    try {
      const status = await api.authStatus()
      if (status.setup_required) { setState({ phase: 'setup_required' }); return }
    } catch (e) {
      setState({ phase: 'error', message: e instanceof Error ? e.message : String(e) })
      return
    }
    try {
      setState({ phase: 'logged_in', user: await api.me() })
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setState({ phase: 'logged_out' })
      else if (e instanceof ApiError && e.code === 'SETUP_REQUIRED') setState({ phase: 'setup_required' })
      else setState({ phase: 'error', message: e instanceof Error ? e.message : String(e) })
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  // Any call that finds the session gone returns to the login page; one that
  // finds a password change due returns to the password page.
  useEffect(() => {
    onAuthProblem(code => {
      if (code === 'AUTH_REQUIRED') setState({ phase: 'logged_out' })
      else setState(s => s.phase === 'logged_in' ? { phase: 'logged_in', user: { ...s.user, must_change_password: true } } : s)
    })
    return () => onAuthProblem(null)
  }, [])

  const login = useCallback(async (username: string, password: string) => {
    const user = await api.login(username, password)
    setState({ phase: 'logged_in', user })
  }, [])

  const logout = useCallback(async () => {
    try { await api.logout() } finally { setState({ phase: 'logged_out' }) }
  }, [])

  const changePassword = useCallback(async (current: string, next: string) => {
    const result = await api.changePassword(current, next)
    setState({ phase: 'logged_in', user: { username: result.username, role: result.role, must_change_password: false } })
  }, [])

  const user = state.phase === 'logged_in' ? state.user : null
  return (
    <AuthContext.Provider value={{ state, user, login, logout, changePassword, refresh }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}

// The logged-in user's role, for pages that adapt to it. Pages render only
// after login, so a page never sees null in practice.
export function useRole(): { role: Role | null } {
  return { role: useAuth().user?.role ?? null }
}
