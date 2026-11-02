import { useCallback, useEffect, useRef, useState } from 'react'
import { JobOwner } from '../api/client'

// The running and recent jobs a page lists (#617). The list is read from the
// API whenever the page opens, so a job started earlier, in another tab or
// before a reload is found again. Nothing is kept in browser storage (#25).

// While the last answer held a running job, the list is read again this often.
const RELOAD_MS = 10000

export interface JobRow {
  key: string
  kind: string
  target: string
  status: string
  progress: string
  createdAt: string | null
  owner: JobOwner | null
  open: boolean
  onOpen: () => void
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

// Reads a list once on mount, again on reload(), and every RELOAD_MS while it
// holds a running entry; nothing after the page is left. `loaded` turns true
// with the first answer, successful or not.
export function useJobList<T>(load: () => Promise<T[]>, isRunning: (entry: T) => boolean) {
  const [entries, setEntries] = useState<T[]>([])
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)
  const alive = useRef(true)
  const timer = useRef<number | null>(null)
  const loadRef = useRef(load)
  const runningRef = useRef(isRunning)
  loadRef.current = load
  runningRef.current = isRunning

  const reload = useCallback(() => {
    if (timer.current) { window.clearTimeout(timer.current); timer.current = null }
    loadRef.current().then(list => {
      if (!alive.current) return
      setEntries(list)
      setError('')
      setLoaded(true)
      if (list.some(e => runningRef.current(e))) timer.current = window.setTimeout(reload, RELOAD_MS)
    }, (e: unknown) => {
      if (!alive.current) return
      setError(`Could not load the running and recent jobs: ${message(e)}`)
      setLoaded(true)
    })
  }, [])

  useEffect(() => {
    alive.current = true
    reload()
    return () => {
      alive.current = false
      if (timer.current) window.clearTimeout(timer.current)
    }
  }, [reload])

  return { entries, error, loaded, reload }
}

// The entry a page opens by itself: the newest running one, else the newest.
// `entries` are newest first, as the API orders them.
export function newestToOpen<T>(entries: T[], isRunning: (entry: T) => boolean): T | null {
  return entries.find(isRunning) ?? entries[0] ?? null
}

export function startedLabel(createdAt: string | null): string {
  if (!createdAt) return '-'
  const when = new Date(createdAt)
  return Number.isNaN(when.getTime()) ? '-' : when.toLocaleString()
}

export function ownerLabel(owner: JobOwner | null): string {
  if (!owner) return 'owner unknown'
  return owner.kind === 'service' ? `${owner.name} (API key)` : owner.name
}

export default function JobList({ rows, error, loaded, showOwner }: {
  rows: JobRow[]; error: string; loaded: boolean; showOwner: boolean
}) {
  return (
    <section aria-label="Running and recent jobs" className="bg-white border rounded p-4 mb-6">
      <h2 className="font-semibold mb-2">Running and recent jobs</h2>
      {error && <p role="alert" className="text-red-600 text-sm mb-2">{error}</p>}
      {loaded && !error && rows.length === 0 && (
        <p className="text-sm text-gray-500">No running or recent jobs.</p>
      )}
      {rows.length > 0 && (
        <ul className="space-y-1">
          {rows.map(row => (
            <li key={row.key}>
              <button type="button" onClick={row.onOpen} aria-current={row.open ? 'true' : undefined}
                className={`job-row w-full text-left text-sm border rounded px-3 py-2 flex flex-wrap gap-x-3 ${row.open ? 'border-blue-500 bg-blue-50' : 'hover:bg-gray-50'}`}>
                <span className="font-medium">{row.kind}</span>
                <span className="flex-1 break-all">{row.target}</span>
                <span className={row.status === 'completed' ? 'text-green-600' : row.status === 'failed' ? 'text-red-600' : 'text-blue-600'}>{row.status}</span>
                <span className="text-gray-600">{row.progress}</span>
                <span className="text-gray-500 text-xs self-center">Started {startedLabel(row.createdAt)}</span>
                {showOwner && <span className="job-owner text-gray-500 text-xs self-center">{ownerLabel(row.owner)}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
