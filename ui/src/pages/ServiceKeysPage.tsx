import { FormEvent, useEffect, useState } from 'react'
import { api, Role, ServiceKey, ServicePrincipal } from '../api/client'

const ROLES: Role[] = ['end_user', 'developer', 'engineer']
const ROLE_LABELS: Record<Role, string> = { end_user: 'End User', developer: 'Developer', engineer: 'AI Engineer' }
const STATUS_LABELS: Record<ServiceKey['status'], string> = { active: 'Active', expired: 'Expired', revoked: 'Revoked' }

const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString() : 'never')

// Service principals and their API keys, for Engineers (#25, PR 3). A principal
// is an account that can't log in; services call the API with its keys, sent as
// `Authorization: Bearer <key>`, and a key can do what the principal's role
// allows. The API decides everything: keys expire (1-365 days, default 90),
// revoked and expired keys stay listed and never work again, and principals
// are disabled, never deleted. A new key is shown once, from the answer that
// carried it, and kept only in this page's memory until "Done".
export default function ServiceKeysPage() {
  const [principals, setPrincipals] = useState<ServicePrincipal[]>([])
  const [listError, setListError] = useState('')
  const [actionError, setActionError] = useState('')
  const [name, setName] = useState('')
  const [role, setRole] = useState<Role>('end_user')
  const [labels, setLabels] = useState<Record<string, string>>({})
  const [days, setDays] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [shown, setShown] = useState<{ name: string; secret: string } | null>(null)

  async function load() {
    try {
      setPrincipals((await api.listPrincipals()).principals)
      setListError('')
    } catch (e: unknown) {
      setListError(`Could not load the service principals: ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  useEffect(() => { load() }, [])

  async function act(run: () => Promise<{ principal: ServicePrincipal; secret?: string }>) {
    setActionError('')
    setBusy(true)
    try {
      const result = await run()
      if (result.secret) setShown({ name: result.principal.name, secret: result.secret })
      await load()
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
    if (await act(() => api.createPrincipal(name, role))) setName('')
  }

  async function issue(e: FormEvent, principal: string) {
    e.preventDefault()
    const lifetime = Number(days[principal] ?? '90')
    if (await act(() => api.issueKey(principal, labels[principal] ?? '', lifetime))) {
      setLabels(l => ({ ...l, [principal]: '' }))
    }
  }

  return (
    <div className="max-w-5xl mx-auto">
      <h1 className="text-2xl font-bold mb-2">Service keys</h1>
      <p className="text-sm text-gray-600 mb-6">
        A service principal is an account for a program, not a person: it can't log in, and calls the API with its
        keys, sent as <code>Authorization: Bearer &lt;key&gt;</code>. A key can do what the principal's role allows,
        except manage accounts or keys. To rotate a key, issue a new one, switch the service over to it, then revoke
        the old one; both work until then.
      </p>
      {listError && <p role="alert" className="text-red-600 text-sm mb-4">{listError}</p>}

      {shown && (
        <div data-testid="one-time-key" className="mb-6 border border-amber-300 bg-amber-50 rounded p-4 text-sm">
          <p className="mb-2">
            New key for <strong>{shown.name}</strong>: <code className="bg-white border border-gray-200 rounded px-2 py-1 select-all break-all">{shown.secret}</code>
          </p>
          <p className="text-gray-700 mb-3">
            It is shown only once. Give it to the service, which sends it as <code>Authorization: Bearer &lt;key&gt;</code>.
          </p>
          <button onClick={() => setShown(null)} className="bg-blue-600 text-white px-3 py-1 rounded text-sm hover:bg-blue-700">Done</button>
        </div>
      )}

      <form onSubmit={create} className="flex flex-wrap items-end gap-3 mb-6">
        <label className="text-sm text-gray-700">
          Name
          <input id="new-principal-name" value={name} onChange={e => setName(e.target.value)} autoComplete="off"
            className="mt-1 block border border-gray-300 rounded px-3 py-2 text-sm" required />
        </label>
        <label className="text-sm text-gray-700">
          Role
          <select id="new-principal-role" value={role} onChange={e => setRole(e.target.value as Role)}
            className="mt-1 block border border-gray-300 rounded px-3 py-2 text-sm">
            {ROLES.map(r => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
          </select>
        </label>
        <button type="submit" disabled={busy || !name}
          className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700 disabled:opacity-50">Create principal</button>
        <p className="text-xs text-gray-500 basis-full">
          3-32 characters: lowercase letters, digits, '.', '_' or '-'; a name no person or service already has.
        </p>
      </form>

      {actionError && <p role="alert" className="text-red-600 text-sm mb-4">{actionError}</p>}

      {principals.length === 0 && !listError && <p className="text-sm text-gray-500">No service principals yet.</p>}

      {principals.map(p => (
        <section key={p.name} data-principal={p.name} className="border border-gray-200 rounded p-4 mb-4">
          <div className="flex flex-wrap items-center gap-4 mb-3">
            <h2 className="font-semibold">{p.name}</h2>
            <select aria-label={`Role of ${p.name}`} value={p.role} disabled={busy}
              onChange={e => { const next = e.target.value as Role; act(() => api.setPrincipalRole(p.name, next)) }}
              className="border border-gray-300 rounded px-2 py-1 text-sm">
              {ROLES.map(r => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
            </select>
            <span data-testid="principal-status" className="text-sm">{p.enabled ? 'Enabled' : 'Disabled'}</span>
            <button disabled={busy}
              onClick={() => act(() => p.enabled ? api.disablePrincipal(p.name) : api.enablePrincipal(p.name))}
              className="text-sm text-blue-600 hover:underline disabled:opacity-50">{p.enabled ? 'Disable' : 'Enable'}</button>
          </div>

          <form onSubmit={e => issue(e, p.name)} className="flex flex-wrap items-end gap-3 mb-3">
            <label className="text-sm text-gray-700">
              Label (optional)
              <input aria-label={`Label for a new key of ${p.name}`} value={labels[p.name] ?? ''} maxLength={64}
                onChange={e => setLabels(l => ({ ...l, [p.name]: e.target.value }))} autoComplete="off"
                className="mt-1 block border border-gray-300 rounded px-3 py-2 text-sm" />
            </label>
            <label className="text-sm text-gray-700">
              Lifetime (days)
              <input aria-label={`Lifetime in days for a new key of ${p.name}`} type="number" min={1} max={365} step={1}
                value={days[p.name] ?? '90'} onChange={e => setDays(d => ({ ...d, [p.name]: e.target.value }))}
                className="mt-1 block w-24 border border-gray-300 rounded px-3 py-2 text-sm" required />
            </label>
            <button type="submit" disabled={busy}
              className="bg-blue-600 text-white px-3 py-2 rounded text-sm hover:bg-blue-700 disabled:opacity-50">Issue key</button>
          </form>

          {p.keys.length > 0 && (
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="border-b border-gray-200 text-left text-gray-600">
                  <th className="py-2 pr-4">Key</th>
                  <th className="py-2 pr-4">Label</th>
                  <th className="py-2 pr-4">Created</th>
                  <th className="py-2 pr-4">Expires</th>
                  <th className="py-2 pr-4">Last used</th>
                  <th className="py-2 pr-4">Status</th>
                  <th className="py-2"></th>
                </tr>
              </thead>
              <tbody>
                {p.keys.map(k => (
                  <tr key={k.id} data-key={k.id} className="border-b border-gray-100">
                    <td className="py-2 pr-4 font-mono">{k.id}</td>
                    <td className="py-2 pr-4">{k.label}</td>
                    <td className="py-2 pr-4">{day(k.created_at)}</td>
                    <td className="py-2 pr-4">{day(k.expires_at)}</td>
                    <td className="py-2 pr-4">{day(k.last_used_at)}</td>
                    <td className="py-2 pr-4">{STATUS_LABELS[k.status]}</td>
                    <td className="py-2">
                      {k.status === 'active' && (
                        <button disabled={busy} onClick={() => act(() => api.revokeKey(p.name, k.id))}
                          className="text-blue-600 hover:underline disabled:opacity-50">Revoke</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      ))}
    </div>
  )
}
