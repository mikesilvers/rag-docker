import { FormEvent, useEffect, useState } from 'react'
import { api, AuditFilters, AuditPage as AuditPageData, AuditRecord, auditCsvUrl } from '../api/client'

// The names the API records (api/services/audit.py ALL_ACTIONS), for the filter.
const ACTIONS = [
  'admin.key.issue', 'admin.key.revoke', 'admin.principal.create', 'admin.principal.disable',
  'admin.principal.enable', 'admin.principal.set-role', 'admin.user.create', 'admin.user.disable',
  'admin.user.enable', 'admin.user.reset-password', 'admin.user.set-role', 'audit.download', 'audit.view',
  'auth.login', 'auth.logout', 'auth.password', 'collection.create', 'collection.delete', 'export',
  'goldstandard.download', 'goldstandard.save', 'import', 'ingest', 'ingest.config', 'operator.bootstrap',
  'operator.create-user', 'operator.disable', 'operator.enable', 'operator.reset-password', 'operator.set-role',
  'query', 'retrieval.config', 'system.start', 'tune.rechunk', 'tune.reembed', 'tune.reindex',
]
const RESULTS = ['success', 'refused', 'error', 'accepted', 'succeeded', 'failed', 'interrupted']
const EMPTY: AuditFilters = { from: '', to: '', actor: '', action: '', collection: '', result: '' }

// A datetime-local value (no zone) read as UTC, as the labels say.
const utc = (local: string) => (local ? `${local}Z` : '')

function actorText(r: AuditRecord): string {
  if (r.actor === null) return r.actor_kind
  return r.key_id ? `${r.actor} (key ${r.key_id})` : r.actor_kind === 'user' ? r.actor : `${r.actor} (${r.actor_kind})`
}

// The audit log, for Engineers (#29): who did what, to which data, with what
// result, newest first. The API decides what is recorded and who may read it;
// this page only filters and pages through it, and links the CSV of the same
// filters. Opening a page and downloading the CSV are recorded too.
export default function AuditPage() {
  const [form, setForm] = useState<AuditFilters>(EMPTY)
  const [filters, setFilters] = useState<AuditFilters>(EMPTY)
  // The cursors of the pages shown so far: [undefined] is the newest page.
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined])
  const [data, setData] = useState<AuditPageData | null>(null)
  const [error, setError] = useState('')
  // A page is on its way: shown, and Older is disabled, so a quick second
  // click can't add the same cursor twice.
  const [loading, setLoading] = useState(true)

  const before = cursors[cursors.length - 1]

  useEffect(() => {
    let current = true
    setLoading(true)
    api.listAudit(filters, before)
      .then(page => { if (current) { setData(page); setError('') } })
      .catch((e: unknown) => { if (current) setError(e instanceof Error ? e.message : String(e)) })
      .finally(() => { if (current) setLoading(false) })
    return () => { current = false }
  }, [filters, before])

  function apply(e: FormEvent) {
    e.preventDefault()
    setFilters({ ...form, from: utc(form.from), to: utc(form.to) })
    setCursors([undefined])
  }

  const set = (name: keyof AuditFilters) => (e: { target: { value: string } }) =>
    setForm(f => ({ ...f, [name]: e.target.value }))
  const field = 'mt-1 block border border-gray-300 rounded px-3 py-2 text-sm'

  return (
    <div className="max-w-6xl mx-auto">
      <h1 className="text-2xl font-bold mb-2">Audit log</h1>
      <p className="text-sm text-gray-600 mb-4">
        Logins, queries, imports, exports, deletions, tuning, account and key administration, and the operator
        command, newest first. Passwords, keys, passphrases and answers are never recorded.
        {data && (data.question_text_logged
          ? ' Question text is being recorded (AUDIT_LOG_QUESTIONS=true).'
          : ' Question text is not recorded (AUDIT_LOG_QUESTIONS=false).')}
      </p>
      {data && data.failed_writes > 0 && (
        <p role="alert" data-testid="audit-failed-writes" className="text-amber-700 bg-amber-50 border border-amber-300 rounded p-2 text-sm mb-4">
          {data.failed_writes} audit record(s) could not be written since the API started. The API log names them.
        </p>
      )}

      <form onSubmit={apply} className="flex flex-wrap items-end gap-3 mb-4">
        <label className="text-sm text-gray-700">From (UTC)
          <input id="audit-from" type="datetime-local" value={form.from} onChange={set('from')} className={field} />
        </label>
        <label className="text-sm text-gray-700">To (UTC)
          <input id="audit-to" type="datetime-local" value={form.to} onChange={set('to')} className={field} />
        </label>
        <label className="text-sm text-gray-700">Actor
          <input id="audit-actor" value={form.actor} onChange={set('actor')} autoComplete="off" className={field} />
        </label>
        <label className="text-sm text-gray-700">Action
          <select id="audit-action" value={form.action} onChange={set('action')} className={field}>
            <option value="">Any</option>
            {ACTIONS.map(a => <option key={a} value={a}>{a}</option>)}
          </select>
        </label>
        <label className="text-sm text-gray-700">Collection
          <input id="audit-collection" value={form.collection} onChange={set('collection')} autoComplete="off" className={field} />
        </label>
        <label className="text-sm text-gray-700">Result
          <select id="audit-result" value={form.result} onChange={set('result')} className={field}>
            <option value="">Any</option>
            {RESULTS.map(r => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <button type="submit" className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">Apply</button>
        <a data-testid="audit-csv" href={auditCsvUrl(filters)} className="text-sm text-blue-600 hover:underline py-2">Download CSV</a>
      </form>

      {error && <p role="alert" className="text-red-600 text-sm mb-4">{error}</p>}
      {loading && <p data-testid="audit-loading" className="text-sm text-gray-500 mb-4">Loading…</p>}
      {data && data.records.length === 0 && !error && <p className="text-sm text-gray-500">No records match.</p>}

      {data && data.records.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b border-gray-200 text-left text-gray-600">
                <th className="py-2 pr-3">Time (UTC)</th>
                <th className="py-2 pr-3">Actor</th>
                <th className="py-2 pr-3">Role</th>
                <th className="py-2 pr-3">Address</th>
                <th className="py-2 pr-3">Action</th>
                <th className="py-2 pr-3">Collection</th>
                <th className="py-2 pr-3">Target</th>
                <th className="py-2 pr-3">Result</th>
                <th className="py-2 pr-3">Job</th>
                <th className="py-2">Details</th>
              </tr>
            </thead>
            <tbody>
              {data.records.map(r => (
                <tr key={r.id} data-record={r.id} className="border-b border-gray-100 align-top">
                  <td className="py-2 pr-3 whitespace-nowrap">{r.time.replace('T', ' ').replace('Z', '')}</td>
                  <td className="py-2 pr-3">{actorText(r)}</td>
                  <td className="py-2 pr-3">{r.role ?? ''}</td>
                  <td className="py-2 pr-3 font-mono">{r.client_ip ?? ''}</td>
                  <td className="py-2 pr-3 font-mono">{r.action}</td>
                  <td className="py-2 pr-3">{r.collection ?? ''}</td>
                  <td className="py-2 pr-3">{r.target ?? ''}</td>
                  <td className="py-2 pr-3">{r.result}{r.status_code !== null ? ` (${r.status_code})` : ''}</td>
                  <td className="py-2 pr-3 font-mono">{r.job_id ?? ''}</td>
                  <td className="py-2 font-mono text-xs break-all">{Object.keys(r.details).length ? JSON.stringify(r.details) : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex gap-3 mt-4">
        <button disabled={cursors.length === 1} onClick={() => setCursors([undefined])}
          className="text-sm text-blue-600 hover:underline disabled:opacity-50 disabled:no-underline">Newest</button>
        <button disabled={loading || !data?.next_before} onClick={() => {
          const next = data?.next_before
          // Only a cursor not asked for yet: a second click before the page
          // renders sees the same next_before.
          if (next && !loading) setCursors(c => (c[c.length - 1] === next ? c : [...c, next]))
        }}
          className="text-sm text-blue-600 hover:underline disabled:opacity-50 disabled:no-underline">Older</button>
      </div>
    </div>
  )
}
