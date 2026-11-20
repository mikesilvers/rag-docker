import { FormEvent, useEffect, useRef, useState } from 'react'
import { api, AdminUser, Group, ServicePrincipal } from '../api/client'

// Groups for collection access, for Engineers (#784). A group holds people and
// service principals; a restricted collection granted to a group (Access on the
// Collections page) is usable by its members while both hold. The API decides
// everything: names follow the username rule, a rename keeps members and
// grants, and deleting a group removes its grants without opening anything.
export default function GroupsPage() {
  const [groups, setGroups] = useState<Group[]>([])
  const [listError, setListError] = useState('')
  const [actionError, setActionError] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  // The Members dialog: the group, its draft, and the lists to tick. Each
  // opening is numbered, so an earlier opening's late lists never reach it.
  const [membersOf, setMembersOf] = useState('')
  const [draft, setDraft] = useState<{ users: string[]; service_principals: string[] }>({ users: [], service_principals: [] })
  const [people, setPeople] = useState<AdminUser[]>([])
  const [principals, setPrincipals] = useState<ServicePrincipal[]>([])
  const [dialogError, setDialogError] = useState('')
  const opening = useRef(0)
  const [renaming, setRenaming] = useState('')
  const [newName, setNewName] = useState('')
  const [deleting, setDeleting] = useState('')

  async function load() {
    try {
      setGroups((await api.listGroups()).groups)
      setListError('')
    } catch (e: unknown) {
      setListError(`Could not load the groups: ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  useEffect(() => { load() }, [])

  const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

  async function create(e: FormEvent) {
    e.preventDefault()
    setActionError('')
    setBusy(true)
    try {
      await api.createGroup(name)
      setName('')
    } catch (err: unknown) {
      setActionError(message(err))
    } finally {
      setBusy(false)
      await load()
    }
  }

  function openMembers(g: Group) {
    const ticket = ++opening.current
    setDialogError('')
    setMembersOf(g.name)
    setDraft({ users: [...g.users], service_principals: [...g.service_principals] })
    api.listUsers().then(r => { if (ticket === opening.current) setPeople(r.users) })
      .catch((e: unknown) => { if (ticket === opening.current) setDialogError(`Could not load the people: ${message(e)}`) })
    api.listPrincipals().then(r => { if (ticket === opening.current) setPrincipals(r.principals) })
      .catch((e: unknown) => { if (ticket === opening.current) setDialogError(`Could not load the service principals: ${message(e)}`) })
  }

  function closeDialogs() {
    opening.current++
    setMembersOf('')
    setRenaming('')
    setDeleting('')
    setDialogError('')
  }

  function toggle(list: 'users' | 'service_principals', member: string) {
    setDraft(d => ({ ...d, [list]: d[list].includes(member) ? d[list].filter(n => n !== member) : [...d[list], member] }))
  }

  async function run(action: () => Promise<unknown>) {
    setDialogError('')
    setBusy(true)
    try {
      await action()
      closeDialogs()
    } catch (e: unknown) {
      setDialogError(message(e))
    } finally {
      setBusy(false)
      await load()
    }
  }

  const memberCount = (g: Group) => g.users.length + g.service_principals.length

  return (
    <div className="max-w-5xl mx-auto">
      <h1 className="text-2xl font-bold mb-2">Groups</h1>
      <p className="text-sm text-gray-600 mb-6">
        A group holds people and service principals. Grant a restricted collection to a group with <strong>Access</strong> on
        the Collections page, and its members may use it while they are in the group. Changes apply to everyone's next
        request.
      </p>
      {listError && <p role="alert" className="text-red-600 text-sm mb-4">{listError}</p>}

      <form onSubmit={create} className="flex flex-wrap items-end gap-3 mb-6">
        <label className="text-sm text-gray-700">
          Name
          <input id="new-group-name" value={name} onChange={e => setName(e.target.value)} autoComplete="off"
            className="mt-1 block border border-gray-300 rounded px-3 py-2 text-sm" required />
        </label>
        <button type="submit" disabled={busy || !name}
          className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700 disabled:opacity-50">Create group</button>
        <p className="text-xs text-gray-500 basis-full">
          3-32 characters: lowercase letters, digits, '.', '_' or '-'; a name no other group has.
        </p>
      </form>

      {actionError && <p role="alert" className="text-red-600 text-sm mb-4">{actionError}</p>}

      {groups.length === 0 && !listError && <p className="text-sm text-gray-500">No groups yet.</p>}

      {groups.length > 0 && (
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="border-b border-gray-200 text-left text-gray-600">
              <th className="py-2 pr-4">Name</th>
              <th className="py-2 pr-4">People</th>
              <th className="py-2 pr-4">Service principals</th>
              <th className="py-2"></th>
            </tr>
          </thead>
          <tbody>
            {groups.map(g => (
              <tr key={g.name} data-group={g.name} className="border-b border-gray-100">
                <td className="py-2 pr-4 font-medium">{g.name}</td>
                <td className="py-2 pr-4">{g.users.join(', ') || '—'}</td>
                <td className="py-2 pr-4">{g.service_principals.join(', ') || '—'}</td>
                <td className="py-2 space-x-3 whitespace-nowrap">
                  <button disabled={busy} onClick={() => openMembers(g)} className="text-blue-600 hover:underline disabled:opacity-50">Members</button>
                  <button disabled={busy} onClick={() => { setDialogError(''); setNewName(g.name); setRenaming(g.name) }} className="text-blue-600 hover:underline disabled:opacity-50">Rename</button>
                  <button disabled={busy} onClick={() => { setDialogError(''); setDeleting(g.name) }} className="text-red-500 hover:underline disabled:opacity-50">Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {membersOf && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div role="dialog" aria-label={`Members of ${membersOf}`} className="bg-white rounded-xl p-6 w-96 shadow-xl max-h-[80vh] overflow-y-auto">
            <h2 className="font-semibold mb-3">Members of {membersOf}</h2>
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
            {dialogError && <p role="alert" className="text-red-600 text-sm mb-4">{dialogError}</p>}
            <div className="flex justify-end gap-2">
              <button onClick={closeDialogs} className="text-sm text-gray-500">Cancel</button>
              <button disabled={busy} onClick={() => run(() => api.setGroupMembers(membersOf, draft.users, draft.service_principals))}
                className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700 disabled:opacity-40">Save</button>
            </div>
          </div>
        </div>
      )}

      {renaming && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div role="dialog" aria-label={`Rename ${renaming}`} className="bg-white rounded-xl p-6 w-96 shadow-xl">
            <h2 className="font-semibold mb-3">Rename {renaming}</h2>
            <p className="text-sm text-gray-600 mb-3">Its members and the collections granted to it stay.</p>
            <input aria-label="New name" value={newName} onChange={e => setNewName(e.target.value)} autoComplete="off"
              className="border rounded px-3 py-2 text-sm w-full mb-4" />
            {dialogError && <p role="alert" className="text-red-600 text-sm mb-4">{dialogError}</p>}
            <div className="flex justify-end gap-2">
              <button onClick={closeDialogs} className="text-sm text-gray-500">Cancel</button>
              <button disabled={busy || !newName} onClick={() => run(() => api.renameGroup(renaming, newName))}
                className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700 disabled:opacity-40">Rename</button>
            </div>
          </div>
        </div>
      )}

      {deleting && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div role="dialog" aria-label={`Delete ${deleting}`} className="bg-white rounded-xl p-6 w-96 shadow-xl">
            <h2 className="font-semibold text-red-600 mb-3">Delete group {deleting}?</h2>
            <p className="text-sm text-gray-600 mb-4">
              Its members lose what the group gave them: every collection grant to {deleting} is removed. A collection
              granted only to this group stays restricted (AI Engineers and any other grants).
              {(() => { const g = groups.find(x => x.name === deleting); return g ? ` It has ${memberCount(g)} ${memberCount(g) === 1 ? 'member' : 'members'}.` : '' })()}
            </p>
            {dialogError && <p role="alert" className="text-red-600 text-sm mb-4">{dialogError}</p>}
            <div className="flex justify-end gap-2">
              <button onClick={closeDialogs} className="text-sm text-gray-500">Cancel</button>
              <button disabled={busy} onClick={() => run(() => api.deleteGroup(deleting))}
                className="bg-red-600 text-white px-4 py-2 rounded text-sm hover:bg-red-700 disabled:opacity-40">Delete</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
