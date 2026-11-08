import { FormEvent, useEffect, useState } from 'react'
import { api, AdminUser, Role } from '../api/client'
import { useAuth } from '../context/AuthContext'

const ROLES: Role[] = ['end_user', 'developer', 'engineer']
const ROLE_LABELS: Record<Role, string> = { end_user: 'End User', developer: 'Developer', engineer: 'AI Engineer' }

// Account administration for Engineers (#25). The API decides what is allowed:
// the last enabled Engineer can't be demoted or disabled, and an Engineer's own
// password is changed on the Change password page, not reset here. Accounts are
// disabled, never deleted. A generated password is shown once, from the answer
// that carried it, and kept only in this page's memory until "Done".
export default function UsersPage() {
  const { user: me, refresh } = useAuth()
  const [users, setUsers] = useState<AdminUser[]>([])
  const [listError, setListError] = useState('')
  const [actionError, setActionError] = useState('')
  const [username, setUsername] = useState('')
  const [role, setRole] = useState<Role>('end_user')
  const [busy, setBusy] = useState(false)
  const [shown, setShown] = useState<{ username: string; password: string } | null>(null)

  async function load() {
    try {
      setUsers((await api.listUsers()).users)
      setListError('')
    } catch (e: unknown) {
      setListError(`Could not load the users: ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  useEffect(() => { load() }, [])

  async function act(target: string, run: () => Promise<{ user: AdminUser; password?: string }>) {
    setActionError('')
    setBusy(true)
    try {
      const result = await run()
      if (result.password) setShown({ username: result.user.username, password: result.password })
      await load()
      // A demoted Engineer leaves this page, a disabled one is logged out.
      if (target === me?.username) await refresh()
      return true
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : String(e))
      await load()
      return false
    } finally {
      setBusy(false)
    }
  }

  async function create(e: FormEvent) {
    e.preventDefault()
    if (await act('', () => api.createUser(username, role))) setUsername('')
  }

  return (
    <div className="max-w-5xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Users</h1>
      {listError && <p role="alert" className="text-red-600 text-sm mb-4">{listError}</p>}

      {shown && (
        <div data-testid="one-time-password" className="mb-6 border border-amber-300 bg-amber-50 rounded p-4 text-sm">
          <p className="mb-2">
            Password for <strong>{shown.username}</strong>: <code className="bg-white border border-gray-200 rounded px-2 py-1 select-all">{shown.password}</code>
          </p>
          <p className="text-gray-700 mb-3">
            It is shown only once. Give it to the user; they must change it when they first log in.
          </p>
          <button onClick={() => setShown(null)} className="bg-blue-600 text-white px-3 py-1 rounded text-sm hover:bg-blue-700">Done</button>
        </div>
      )}

      <form onSubmit={create} className="flex flex-wrap items-end gap-3 mb-6">
        <label className="text-sm text-gray-700">
          Username
          <input id="new-username" value={username} onChange={e => setUsername(e.target.value)} autoComplete="off"
            className="mt-1 block border border-gray-300 rounded px-3 py-2 text-sm" required />
        </label>
        <label className="text-sm text-gray-700">
          Role
          <select id="new-role" value={role} onChange={e => setRole(e.target.value as Role)}
            className="mt-1 block border border-gray-300 rounded px-3 py-2 text-sm">
            {ROLES.map(r => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
          </select>
        </label>
        <button type="submit" disabled={busy || !username}
          className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700 disabled:opacity-50">Create user</button>
        <p className="text-xs text-gray-500 basis-full">
          3-32 characters: lowercase letters, digits, '.', '_' or '-'. The account gets a generated password.
        </p>
      </form>

      {actionError && <p role="alert" className="text-red-600 text-sm mb-4">{actionError}</p>}

      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="border-b border-gray-200 text-left text-gray-600">
            <th className="py-2 pr-4">Username</th>
            <th className="py-2 pr-4">Role</th>
            <th className="py-2 pr-4">Status</th>
            <th className="py-2 pr-4">Password</th>
            <th className="py-2 pr-4">Created</th>
            <th className="py-2"></th>
          </tr>
        </thead>
        <tbody>
          {users.map(u => {
            const own = u.username === me?.username
            return (
              <tr key={u.username} className="border-b border-gray-100" data-username={u.username}>
                <td className="py-2 pr-4 font-medium">{u.username}{own && <span className="text-gray-500 font-normal"> (you)</span>}</td>
                <td className="py-2 pr-4">
                  <select aria-label={`Role of ${u.username}`} value={u.role} disabled={busy}
                    onChange={e => { const next = e.target.value as Role; act(u.username, () => api.setUserRole(u.username, next)) }}
                    className="border border-gray-300 rounded px-2 py-1 text-sm">
                    {ROLES.map(r => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
                  </select>
                </td>
                <td className="py-2 pr-4">{u.enabled ? 'Enabled' : 'Disabled'}</td>
                <td className="py-2 pr-4">{u.must_change_password ? 'Change due' : 'Set'}</td>
                <td className="py-2 pr-4">{new Date(u.created_at).toLocaleDateString()}</td>
                <td className="py-2 space-x-3 whitespace-nowrap">
                  {own
                    ? <span className="text-xs text-gray-500">Use Change password for your own</span>
                    : <button disabled={busy} onClick={() => act(u.username, () => api.resetUserPassword(u.username))}
                        className="text-blue-600 hover:underline disabled:opacity-50">Reset password</button>}
                  <button disabled={busy}
                    onClick={() => act(u.username, () => u.enabled ? api.disableUser(u.username) : api.enableUser(u.username))}
                    className="text-blue-600 hover:underline disabled:opacity-50">{u.enabled ? 'Disable' : 'Enable'}</button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
