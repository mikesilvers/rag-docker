import { useState, useEffect } from 'react'
import { api, AdminUser, CollectionAccess, CollectionInfo, ServicePrincipal } from '../api/client'

// Who may use each collection (#606): "Open", "Restricted (n)" with n grants,
// or a dash when the state couldn't be read.
function accessLabel(a: CollectionAccess | null | undefined): string {
  if (!a) return '—'
  return a.restricted ? `Restricted (${a.users.length + a.service_principals.length})` : 'Open'
}

export default function CollectionsPage() {
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  // A refused create is shown inside the New Collection modal, and a failed
  // list on the page; both were hidden or swallowed before (#379).
  const [createError, setCreateError] = useState('')
  const [listError, setListError] = useState('')
  const [showModal, setShowModal] = useState(false)
  const [name, setName] = useState('')
  const [indexType, setIndexType] = useState('hnsw')
  const [distanceMetric, setDistanceMetric] = useState('cosine')
  const [efConstruction, setEfConstruction] = useState(128)
  const [maxConnections, setMaxConnections] = useState(64)
  const [ef, setEf] = useState(64)
  const [deleteTarget, setDeleteTarget] = useState('')
  const [deleteConfirm, setDeleteConfirm] = useState('')
  // Shown inside the delete dialog: the page-level error sits behind its
  // overlay, so a refused delete (409 while a job runs, #40) looked like nothing.
  const [deleteError, setDeleteError] = useState('')
  // Per-collection access (#606): each row's state, and the Access dialog.
  const [access, setAccess] = useState<Record<string, CollectionAccess | null>>({})
  const [accessTarget, setAccessTarget] = useState('')
  const [draft, setDraft] = useState<CollectionAccess>({ restricted: false, users: [], service_principals: [] })
  const [people, setPeople] = useState<AdminUser[]>([])
  const [principals, setPrincipals] = useState<ServicePrincipal[]>([])
  const [accessError, setAccessError] = useState('')

  async function load() {
    api.getCollections().then(r => {
      setCollections(r.collections)
      setListError('')
      for (const c of r.collections) {
        api.getCollectionAccess(c.name)
          .then(a => setAccess(prev => ({ ...prev, [c.name]: a })))
          .catch(() => setAccess(prev => ({ ...prev, [c.name]: null })))
      }
    }).catch((e: unknown) => setListError(`Could not load the collections: ${e instanceof Error ? e.message : String(e)}`))
  }

  function openAccess(name: string) {
    setAccessError('')
    setAccessTarget(name)
    const current = access[name]
    setDraft(current ? { ...current } : { restricted: false, users: [], service_principals: [] })
    api.getCollectionAccess(name).then(a => setDraft({ ...a }))
      .catch((e: unknown) => setAccessError(e instanceof Error ? e.message : String(e)))
    api.listUsers().then(r => setPeople(r.users))
      .catch((e: unknown) => setAccessError(`Could not load the people: ${e instanceof Error ? e.message : String(e)}`))
    api.listPrincipals().then(r => setPrincipals(r.principals))
      .catch((e: unknown) => setAccessError(`Could not load the service principals: ${e instanceof Error ? e.message : String(e)}`))
  }

  function closeAccess() {
    setAccessTarget('')
    setAccessError('')
  }

  function toggle(list: 'users' | 'service_principals', name: string) {
    setDraft(d => ({ ...d, [list]: d[list].includes(name) ? d[list].filter(n => n !== name) : [...d[list], name] }))
  }

  async function saveAccess() {
    const body = draft.restricted ? draft : { restricted: false, users: [], service_principals: [] }
    try {
      const saved = await api.setCollectionAccess(accessTarget, body)
      setAccess(prev => ({ ...prev, [accessTarget]: saved }))
      closeAccess()
    } catch (e: unknown) {
      setAccessError(e instanceof Error ? e.message : String(e))
    }
  }

  useEffect(() => { load() }, [])

  async function create() {
    try {
      await api.createCollection({ name, index_type: indexType, distance_metric: distanceMetric, hnsw_config: { efConstruction, maxConnections, ef } })
      setShowModal(false)
      setName('')
      setCreateError('')
      load()
    } catch (e: unknown) {
      setCreateError(e instanceof Error ? e.message : String(e))
    }
  }

  function openCreate() {
    setCreateError('')
    setShowModal(true)
  }

  function closeCreate() {
    setShowModal(false)
    setCreateError('')
  }

  async function deleteCollection() {
    if (deleteConfirm !== deleteTarget) return
    try {
      await api.deleteCollection(deleteTarget)
      setDeleteTarget('')
      setDeleteConfirm('')
      setDeleteError('')
      load()
    } catch (e: unknown) {
      setDeleteError(e instanceof Error ? e.message : String(e))
    }
  }

  function closeDelete() {
    setDeleteTarget('')
    setDeleteConfirm('')
    setDeleteError('')
  }

  return (
    <div className="max-w-4xl mx-auto">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold">Collections</h1>
        <button onClick={openCreate} className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">+ New Collection</button>
      </div>
      {listError && <p role="alert" className="text-red-600 text-sm mb-4">{listError}</p>}
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="border-b text-left text-gray-500">
            <th className="py-2">Name</th>
            <th className="py-2">Objects</th>
            <th className="py-2">Index</th>
            <th className="py-2">Distance</th>
            <th className="py-2">Created</th>
            <th className="py-2">Access</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {collections.map(c => (
            <tr key={c.name} className="border-b hover:bg-gray-50">
              <td className="py-2 font-medium">{c.name}</td>
              <td className="py-2">{c.object_count}</td>
              <td className="py-2">{c.index_type}</td>
              <td className="py-2">{c.distance_metric}</td>
              <td className="py-2 text-gray-500">{c.created_at ? new Date(c.created_at).toLocaleDateString() : '—'}</td>
              <td className="py-2">{accessLabel(access[c.name])}</td>
              <td className="py-2 space-x-3">
                <button onClick={() => openAccess(c.name)} className="text-blue-500 hover:text-blue-700 text-xs">Access</button>
                <button onClick={() => { setDeleteError(''); setDeleteTarget(c.name) }} className="text-red-400 hover:text-red-600 text-xs">Delete</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {showModal && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-96 shadow-xl">
            <h2 className="font-semibold mb-4">New Collection</h2>
            <input value={name} onChange={e => setName(e.target.value)} placeholder="Name" className="border rounded px-3 py-2 text-sm w-full mb-3" />
            <div className="grid grid-cols-2 gap-3 mb-3">
              <div>
                <label className="text-xs text-gray-500 block mb-1">Index Type</label>
                <select value={indexType} onChange={e => setIndexType(e.target.value)} className="border rounded px-2 py-1 text-sm w-full">
                  <option value="hnsw">HNSW</option>
                  <option value="flat">Flat (KNN)</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-gray-500 block mb-1">Distance</label>
                <select value={distanceMetric} onChange={e => setDistanceMetric(e.target.value)} className="border rounded px-2 py-1 text-sm w-full">
                  <option value="cosine">Cosine</option>
                  <option value="dot">Dot Product</option>
                  <option value="l2-squared">L2 Squared</option>
                </select>
              </div>
            </div>
            {indexType === 'hnsw' && (
              <div className="space-y-2 mb-4">
                <div>
                  <label className="text-xs text-gray-500">efConstruction: {efConstruction}</label>
                  <input type="range" min={64} max={512} step={8} value={efConstruction} onChange={e => setEfConstruction(+e.target.value)} className="w-full" />
                </div>
                <div>
                  <label className="text-xs text-gray-500">maxConnections: {maxConnections}</label>
                  <input type="range" min={16} max={128} step={4} value={maxConnections} onChange={e => setMaxConnections(+e.target.value)} className="w-full" />
                </div>
                <div>
                  <label className="text-xs text-gray-500">ef: {ef}</label>
                  <input type="range" min={16} max={512} step={8} value={ef} onChange={e => setEf(+e.target.value)} className="w-full" />
                </div>
              </div>
            )}
            {createError && <p role="alert" className="text-red-600 text-sm mb-4">{createError}</p>}
            <div className="flex justify-end gap-2">
              <button onClick={closeCreate} className="text-sm text-gray-500">Cancel</button>
              <button onClick={create} className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">Create</button>
            </div>
          </div>
        </div>
      )}

      {accessTarget && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div role="dialog" aria-label={`Access to ${accessTarget}`} className="bg-white rounded-xl p-6 w-96 shadow-xl max-h-[80vh] overflow-y-auto">
            <h2 className="font-semibold mb-3">Access to {accessTarget}</h2>
            <label className="flex items-center gap-2 text-sm mb-1">
              <input type="radio" name="access-mode" checked={!draft.restricted} onChange={() => setDraft(d => ({ ...d, restricted: false }))} />
              Open: everyone whose role allows it
            </label>
            <label className="flex items-center gap-2 text-sm mb-3">
              <input type="radio" name="access-mode" checked={draft.restricted} onChange={() => setDraft(d => ({ ...d, restricted: true }))} />
              Restricted: only the accounts ticked below (and AI Engineers)
            </label>
            {draft.restricted && (
              <div className="space-y-3 mb-4">
                <fieldset>
                  <legend className="text-xs text-gray-500 mb-1">People</legend>
                  {people.map(u => (
                    <label key={u.username} className="flex items-center gap-2 text-sm">
                      <input type="checkbox" checked={draft.users.includes(u.username)} onChange={() => toggle('users', u.username)} />
                      {u.username}{u.enabled ? '' : ' (disabled)'}
                    </label>
                  ))}
                </fieldset>
                <fieldset>
                  <legend className="text-xs text-gray-500 mb-1">Service principals</legend>
                  {principals.length === 0 && <p className="text-xs text-gray-400">None</p>}
                  {principals.map(p => (
                    <label key={p.name} className="flex items-center gap-2 text-sm">
                      <input type="checkbox" checked={draft.service_principals.includes(p.name)} onChange={() => toggle('service_principals', p.name)} />
                      {p.name}{p.enabled ? '' : ' (disabled)'}
                    </label>
                  ))}
                </fieldset>
              </div>
            )}
            {accessError && <p role="alert" className="text-red-600 text-sm mb-4">{accessError}</p>}
            <div className="flex justify-end gap-2">
              <button onClick={closeAccess} className="text-sm text-gray-500">Cancel</button>
              <button onClick={saveAccess} className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">Save</button>
            </div>
          </div>
        </div>
      )}

      {deleteTarget && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-96 shadow-xl">
            <h2 className="font-semibold text-red-600 mb-3">Delete Collection</h2>
            <p className="text-sm text-gray-600 mb-3">Type <strong>{deleteTarget}</strong> to confirm deletion of all objects.</p>
            <input value={deleteConfirm} onChange={e => setDeleteConfirm(e.target.value)} placeholder={deleteTarget} className="border rounded px-3 py-2 text-sm w-full mb-4" />
            {deleteError && <p role="alert" className="text-red-600 text-sm mb-4">{deleteError}</p>}
            <div className="flex justify-end gap-2">
              <button onClick={closeDelete} className="text-sm text-gray-500">Cancel</button>
              <button onClick={deleteCollection} disabled={deleteConfirm !== deleteTarget} className="bg-red-600 text-white px-4 py-2 rounded text-sm disabled:opacity-40 hover:bg-red-700">Delete</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
