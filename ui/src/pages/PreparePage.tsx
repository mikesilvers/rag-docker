import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  api, CollectionInfo, JobStatus, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB, PreparedDocument, PreparedList,
  PrepareJob, preparedOutputUrl, preparedRawUrl,
} from '../api/client'
import { useRole } from '../context/AuthContext'
import StrategyExplainer from '../components/StrategyExplainer'
import ProgressPanel from '../components/ProgressPanel'
import JobList, { newestToOpen, useJobList } from '../components/JobList'

// Document preparation (#638). Raw documents are uploaded to the preparation area
// on the instance, prepared by a background job (the code does the corpus rules'
// work, the local LLM only removes headers, footers and boilerplate and writes a
// title and summary for PDF, DOCX, TXT and MD; CSV and JSON parts are labelled
// from their data, #648), reviewed here, and imported into a collection through the
// normal ingest path. They stay on the instance until deleted.

const TERMINAL = ['completed', 'failed', 'partial', 'cancelled', 'interrupted']
const prepareRunning = (j: { status: string }) => !TERMINAL.includes(j.status)
const loadPrepareJobs = () => api.listPrepareJobs().then(r => r.jobs)
const STRATEGIES = ['fixed', 'overlap', 'language', 'context_aware', 'semantic']
const ACCEPT = '.pdf,.docx,.txt,.md,.csv,.json,.jsonl,.jsonl.gz,.zip'

function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function fallbackMark(d: PreparedDocument): string {
  if (d.code_only_by_type) return 'code-only (by type)'
  const count = d.parts.reduce((n, p) => n + p.fallbacks.length, 0)
  return d.fallback ? `fell back (${count})` : ''
}

export default function PreparePage() {
  const { role } = useRole()
  const [files, setFiles] = useState<File[]>([])
  const [list, setList] = useState<PreparedList | null>(null)
  const [listError, setListError] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [error, setError] = useState('')
  const [skipped, setSkipped] = useState<string[]>([])
  const [preview, setPreview] = useState<PreparedDocument | null>(null)
  // The preparation job shown, polled while it runs.
  const [prepareJob, setPrepareJob] = useState<PrepareJob | null>(null)
  const [prepareJobId, setPrepareJobId] = useState('')
  const touched = useRef(false)
  const jobs = useJobList<PrepareJob>(loadPrepareJobs, prepareRunning)
  // The import: collection, chunk settings, and the ingest job it started.
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [collection, setCollection] = useState('')
  const [collectionsError, setCollectionsError] = useState('')
  const [strategy, setStrategy] = useState('overlap')
  const [chunkSize, setChunkSize] = useState(1000)
  const [chunkOverlap, setChunkOverlap] = useState(200)
  const [minChunkSize, setMinChunkSize] = useState(100)
  const [similarityThreshold, setSimilarityThreshold] = useState(0.85)
  const [ingestJobId, setIngestJobId] = useState('')
  const [ingestJob, setIngestJob] = useState<JobStatus | null>(null)
  const [showNewColModal, setShowNewColModal] = useState(false)
  const [newColName, setNewColName] = useState('')
  const [newColIndexType, setNewColIndexType] = useState('hnsw')
  const [newColError, setNewColError] = useState('')

  const showOverlap = strategy !== 'semantic' && strategy !== 'context_aware'
  const showSimilarity = strategy === 'semantic' && role === 'engineer'

  async function loadList() {
    try {
      const l = await api.listPrepared()
      setList(l)
      setListError('')
      setSelected(s => s.filter(id => l.documents.some(d => d.id === id)))
    } catch (e: unknown) {
      setListError(`Could not load the prepared documents: ${message(e)}`)
    }
  }

  async function loadCollections(): Promise<CollectionInfo[] | null> {
    try {
      const r = await api.getCollections()
      setCollections(r.collections)
      setCollectionsError('')
      return r.collections
    } catch (e: unknown) {
      setCollectionsError(`Could not load the collections: ${message(e)}`)
      return null
    }
  }

  useEffect(() => {
    loadList()
    loadCollections().then(l => { if (l && l.length > 0) setCollection(l[0].name) })
  }, [])

  // The preparation job shown is polled every 3 seconds while it runs; the list
  // is read again when it ends.
  useEffect(() => {
    if (!prepareJobId) return
    const interval = setInterval(async () => {
      try {
        const j = await api.getPrepareJob(prepareJobId)
        setPrepareJob(j)
        if (!prepareRunning(j)) {
          clearInterval(interval)
          jobs.reload()
          loadList()
        }
      } catch (e: unknown) {
        setError(`Could not check the preparation's progress: ${message(e)}`)
      }
    }, 3000)
    return () => clearInterval(interval)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prepareJobId])

  useEffect(() => {
    if (!ingestJobId) return
    const interval = setInterval(async () => {
      try {
        const j = await api.getJobStatus(ingestJobId)
        setIngestJob(j)
        if (!prepareRunning(j)) clearInterval(interval)
      } catch (e: unknown) {
        setError(`Could not check the import's progress: ${message(e)}`)
      }
    }, 3000)
    return () => clearInterval(interval)
  }, [ingestJobId])

  function openJob(entry: PrepareJob) {
    touched.current = true
    setPrepareJob(entry)
    setPrepareJobId(prepareRunning(entry) ? entry.job_id : '')
  }

  useEffect(() => {
    if (!jobs.loaded || touched.current) return
    touched.current = true
    const entry = newestToOpen(jobs.entries, prepareRunning)
    if (entry) openJob(entry)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobs.loaded])

  async function upload() {
    if (!files.length) return
    setError('')
    const total = files.reduce((n, f) => n + f.size, 0)
    if (total > MAX_UPLOAD_BYTES) {
      setError(`These files total ${(total / 1024 / 1024).toFixed(0)} MB; one upload can be at most ${MAX_UPLOAD_MB} MB. Split them into smaller batches.`)
      return
    }
    const form = new FormData()
    files.forEach(f => form.append('files', f))
    try {
      const r = await api.uploadForPreparation(form)
      setSkipped(r.skipped)
      setFiles([])
      await loadList()
    } catch (e: unknown) {
      setError(message(e))
    }
  }

  async function prepare(ids: string[] | 'all') {
    setError('')
    try {
      const r = await api.startPreparation(ids)
      touched.current = true
      setPrepareJobId(r.job_id)
      setPrepareJob(null)
      jobs.reload()
      await loadList()
    } catch (e: unknown) {
      setError(message(e))
    }
  }

  async function cancel() {
    if (!prepareJob) return
    try {
      const r = await api.cancelPrepareJob(prepareJob.job_id)
      setPrepareJob({ ...prepareJob, status: r.status })
    } catch (e: unknown) {
      setError(message(e))
    }
  }

  async function remove(d: PreparedDocument, outputOnly: boolean) {
    const what = outputOnly ? `the prepared output of '${d.name}'` : `'${d.name}' (raw and prepared)`
    if (!window.confirm(`Delete ${what}? This can't be undone.`)) return
    setError('')
    try {
      if (outputOnly) await api.deletePreparedOutput(d.id)
      else await api.deletePrepared(d.id)
      if (preview?.id === d.id) setPreview(null)
      await loadList()
    } catch (e: unknown) {
      setError(message(e))
    }
  }

  async function showPreview(d: PreparedDocument) {
    setError('')
    try {
      setPreview(await api.getPrepared(d.id))
    } catch (e: unknown) {
      setError(message(e))
    }
  }

  async function importPrepared(all: boolean) {
    setError('')
    if (!collection) {
      setError('Choose a collection to import into first. Use + New to create one.')
      return
    }
    try {
      const r = await api.importPrepared({
        collection, ...(all ? { all: true } : { document_ids: selected }), strategy, chunk_size: chunkSize,
        chunk_overlap: chunkOverlap, similarity_threshold: similarityThreshold, min_chunk_size: minChunkSize,
      })
      setIngestJobId(r.job_id)
      setIngestJob(null)
      await loadList()
    } catch (e: unknown) {
      setError(message(e))
    }
  }

  async function createCollection() {
    if (!newColName.trim()) return
    let created: string
    try {
      created = (await api.createCollection({
        name: newColName, index_type: newColIndexType, distance_metric: 'cosine',
        hnsw_config: { efConstruction: 128, maxConnections: 64, ef: 64 },
      })).name
    } catch (e: unknown) {
      setNewColError(message(e))
      return
    }
    setShowNewColModal(false)
    setNewColName('')
    setNewColError('')
    await loadCollections()
    setCollections(prev => prev.some(c => c.name === created) ? prev : [...prev, {
      name: created, object_count: 0, index_type: newColIndexType, distance_metric: 'cosine', created_at: null,
    }])
    setCollection(created)
  }

  const docs = list?.documents ?? []
  const toggle = (id: string) => setSelected(s => s.includes(id) ? s.filter(x => x !== id) : [...s, id])
  const anyPrepared = docs.some(d => d.status === 'prepared')

  return (
    <div className="max-w-5xl mx-auto">
      <h1 className="text-2xl font-bold mb-2">Prepare Documents</h1>
      <p className="text-sm text-gray-600 mb-4">
        Raw documents are made to follow the corpus rules here: the code extracts the text, splits it to the limits,
        normalises it and converts JSON, and the local LLM only removes page headers, footers, page numbers and
        boilerplate and writes a title and summary for PDF, DOCX, TXT and MD. CSV and JSON parts are labelled from
        their data, with no LLM call. Where its cleaning would lose content, the part keeps the
        code-only text and the fallback is shown. Documents stay on this instance until you delete them.
        {' '}<Link to="/import" className="text-blue-600 hover:underline">Import page</Link>
      </p>
      <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2 mb-4">
        Preparation and ingest share the local LLM service, so either is slower while the other runs.
      </p>

      <div
        onDrop={e => { e.preventDefault(); setFiles(prev => [...prev, ...Array.from(e.dataTransfer.files)]) }}
        onDragOver={e => e.preventDefault()}
        className="border-2 border-dashed border-gray-300 rounded-lg p-6 text-center mb-3 cursor-pointer hover:border-blue-400"
        onClick={() => document.getElementById('prepare-file-input')?.click()}
      >
        <p className="text-gray-500">Drop raw files here or click to browse</p>
        <p className="text-xs text-gray-400 mt-1">PDF, DOCX, TXT, MD, CSV, JSON, JSONL, ZIP · up to {MAX_UPLOAD_MB} MB per upload</p>
        <input id="prepare-file-input" type="file" multiple className="hidden" accept={ACCEPT}
          onChange={e => setFiles(prev => [...prev, ...Array.from(e.target.files || [])])} />
      </div>
      {files.length > 0 && (
        <ul className="mb-3 space-y-1">
          {files.map((f, i) => (
            <li key={i} className="flex justify-between text-sm bg-gray-50 border rounded px-3 py-1">
              <span>{f.name}</span>
              <button onClick={() => setFiles(files.filter((_, j) => j !== i))} className="text-red-400 hover:text-red-600">✕</button>
            </li>
          ))}
        </ul>
      )}
      <button onClick={upload} disabled={!files.length}
        className="bg-blue-600 text-white px-4 py-2 rounded text-sm disabled:opacity-50 hover:bg-blue-700 mb-2">Upload</button>
      {skipped.length > 0 && (
        <div className="text-sm text-gray-600 mb-2">{skipped.length} files skipped:
          <ul className="list-disc ml-6">{skipped.map((s, i) => <li key={i}>{s}</li>)}</ul>
        </div>
      )}
      {error && <p role="alert" className="text-red-600 text-sm my-2">{error}</p>}

      <section aria-label="Prepared documents" className="bg-white border rounded p-4 my-4">
        <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
          <h2 className="font-semibold">Documents</h2>
          {list && <span data-testid="prepare-usage" className="text-sm text-gray-600">Space used: {size(list.used_bytes)} of {size(list.limit_bytes)}</span>}
        </div>
        {listError && <p role="alert" className="text-red-600 text-sm">{listError}</p>}
        {list && docs.length === 0 && <p className="text-sm text-gray-500">No documents yet.</p>}
        {docs.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="border-b text-left text-gray-600">
                  <th className="py-1 pr-2"></th><th className="py-1 pr-2">Name</th>
                  {role === 'engineer' && <th className="py-1 pr-2">Owner</th>}
                  <th className="py-1 pr-2">Status</th><th className="py-1 pr-2">Raw</th><th className="py-1 pr-2">Prepared</th>
                  <th className="py-1 pr-2">Parts</th><th className="py-1 pr-2">Fallback</th><th className="py-1 pr-2">Imports</th>
                  <th className="py-1">Actions</th>
                </tr>
              </thead>
              <tbody>
                {docs.map(d => (
                  <tr key={d.id} data-document={d.id} className="border-b align-top">
                    <td className="py-1 pr-2"><input type="checkbox" aria-label={`Select ${d.name}`}
                      checked={selected.includes(d.id)} onChange={() => toggle(d.id)} /></td>
                    <td className="py-1 pr-2 break-all">{d.name}
                      {d.last_error && <div className="text-xs text-red-600">Last error: {d.last_error}</div>}</td>
                    {role === 'engineer' && <td className="py-1 pr-2">{d.owner?.name ?? 'owner unknown'}</td>}
                    <td className="py-1 pr-2">{d.status}</td>
                    <td className="py-1 pr-2 whitespace-nowrap">{size(d.raw_bytes)}</td>
                    <td className="py-1 pr-2 whitespace-nowrap">{d.prepared_bytes ? size(d.prepared_bytes) : '-'}</td>
                    <td className="py-1 pr-2">{d.parts.length || '-'}</td>
                    <td className="py-1 pr-2">{fallbackMark(d)}</td>
                    <td className="py-1 pr-2 text-xs">{d.imports.map((i, n) =>
                      <div key={n}>{i.collection} · {new Date(i.at).toLocaleString()}</div>)}</td>
                    <td className="py-1 text-xs space-x-2 whitespace-nowrap">
                      {d.status === 'prepared' && <button onClick={() => showPreview(d)} className="text-blue-600 hover:underline">Preview</button>}
                      <a href={preparedRawUrl(d.id)} className="text-blue-600 hover:underline">Download raw</a>
                      {d.status === 'prepared' && <a href={preparedOutputUrl(d.id)} className="text-blue-600 hover:underline">Download prepared</a>}
                      {d.status === 'prepared' && <button onClick={() => remove(d, true)} className="text-red-600 hover:underline">Delete prepared</button>}
                      <button onClick={() => remove(d, false)} className="text-red-600 hover:underline">Delete</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="flex gap-2 mt-3">
          <button onClick={() => prepare(selected)} disabled={!selected.length}
            className="bg-blue-600 text-white px-4 py-2 rounded text-sm disabled:opacity-50 hover:bg-blue-700">Prepare selected</button>
          <button onClick={() => prepare('all')} disabled={!docs.length}
            className="border px-4 py-2 rounded text-sm disabled:opacity-50 hover:bg-gray-50">Prepare all</button>
        </div>
      </section>

      {prepareJob && (
        <section aria-label="Preparation progress" className="border rounded p-4 mb-4 bg-gray-50 text-sm">
          <div className="flex justify-between">
            <span>Preparation <span className="font-mono">{prepareJob.job_id}</span>: {prepareJob.documents_completed + prepareJob.documents_failed}/{prepareJob.documents_total} documents</span>
            <span className="text-blue-600">{prepareJob.status}</span>
          </div>
          {prepareJob.current && (
            <p className="mt-1">Now: {prepareJob.current.name}
              {prepareJob.current.windows_total > 0 && ` (${prepareJob.current.windows_done}/${prepareJob.current.windows_total} windows)`}</p>
          )}
          {prepareJob.errors.length > 0 && (
            <ul className="mt-2 text-red-600 list-disc ml-6">{prepareJob.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
          )}
          {prepareRunning(prepareJob) && (
            <button onClick={cancel} className="mt-2 border border-red-300 text-red-700 px-3 py-1 rounded hover:bg-red-50">Cancel</button>
          )}
        </section>
      )}

      {preview && (
        <section aria-label="Preview" className="border rounded p-4 mb-4">
          <div className="flex justify-between mb-2">
            <h2 className="font-semibold">Preview: {preview.name}</h2>
            <button onClick={() => setPreview(null)} className="text-sm text-gray-500 hover:text-gray-700">Close</button>
          </div>
          {preview.parts.map(p => (
            <div key={p.name} className="mb-4">
              <p className="font-medium break-all">{p.name} <span className="text-gray-500 font-normal">({size(p.bytes)})</span></p>
              {p.title ? <p className="text-sm"><span className="text-gray-500">Title:</span> {p.title}</p> : null}
              {p.summary ? <p className="text-sm"><span className="text-gray-500">Summary:</span> {p.summary}</p>
                : <p className="text-sm text-gray-500">No title or summary ({p.summary_error ?? 'none'}).</p>}
              {p.fallbacks.length > 0 && (
                <ul className="text-xs text-amber-700 list-disc ml-6">{p.fallbacks.map((f, i) =>
                  <li key={i}>{f.window === null ? 'whole part' : `window ${f.window}`}: {f.reason}</li>)}</ul>
              )}
              <pre className="mt-1 text-xs bg-gray-50 border rounded p-2 whitespace-pre-wrap max-h-64 overflow-auto">{p.preview}</pre>
            </div>
          ))}
        </section>
      )}

      <section aria-label="Import prepared documents" className="border rounded p-4 mb-4">
        <h2 className="font-semibold mb-2">Import into a collection</h2>
        <div className="mb-3">
          <label className="block text-sm font-medium mb-1">Collection</label>
          <div className="flex gap-2">
            <select value={collection} onChange={e => setCollection(e.target.value)} className="border rounded px-3 py-2 text-sm flex-1">
              {collections.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}
            </select>
            <button onClick={() => { setNewColError(''); setShowNewColModal(true) }} className="text-sm border rounded px-3 py-2 hover:bg-gray-50">+ New</button>
          </div>
          {collectionsError && <p role="alert" className="text-red-600 text-sm mt-2">{collectionsError}</p>}
        </div>
        <label className="block text-sm font-medium mb-1">Chunking Strategy</label>
        <select value={strategy} onChange={e => setStrategy(e.target.value)} className="border rounded px-3 py-2 text-sm w-full">
          {STRATEGIES.map(s => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}
        </select>
        <StrategyExplainer strategy={strategy} />
        <div className="grid grid-cols-2 gap-4 mt-3 mb-3">
          <div>
            <label className="block text-xs text-gray-600 mb-1">Chunk Size: {chunkSize}</label>
            <input type="range" min={50} max={6000} step={50} value={chunkSize} onChange={e => { const s = +e.target.value; setChunkSize(s); setChunkOverlap(o => Math.min(o, Math.max(0, s - 50))) }} className="w-full" />
          </div>
          {showOverlap && (
            <div>
              <label className="block text-xs text-gray-600 mb-1">Overlap: {chunkOverlap}</label>
              <input type="range" min={0} max={Math.max(0, Math.min(2000, chunkSize - 50))} step={50} value={chunkOverlap} onChange={e => setChunkOverlap(+e.target.value)} className="w-full" />
            </div>
          )}
          <div>
            <label className="block text-xs text-gray-600 mb-1">Min Chunk Size: {minChunkSize}</label>
            <input type="range" min={0} max={6000} step={10} value={minChunkSize} onChange={e => setMinChunkSize(+e.target.value)} className="w-full" />
          </div>
          {showSimilarity && (
            <div>
              <label className="block text-xs text-gray-600 mb-1">Similarity Threshold: {similarityThreshold}</label>
              <input type="range" min={0} max={1} step={0.05} value={similarityThreshold} onChange={e => setSimilarityThreshold(+e.target.value)} className="w-full" />
            </div>
          )}
        </div>
        <div className="flex gap-2">
          <button onClick={() => importPrepared(false)} disabled={!selected.length}
            className="bg-blue-600 text-white px-4 py-2 rounded text-sm disabled:opacity-50 hover:bg-blue-700">Import selected</button>
          <button onClick={() => importPrepared(true)} disabled={!anyPrepared}
            className="border px-4 py-2 rounded text-sm disabled:opacity-50 hover:bg-gray-50">Import all prepared</button>
        </div>
        {ingestJobId && <p className="text-sm text-gray-600 mt-2">Job ID: <span className="font-mono">{ingestJobId}</span></p>}
        {ingestJob && <ProgressPanel job={ingestJob} />}
      </section>

      <JobList error={jobs.error} loaded={jobs.loaded} showOwner={role === 'engineer'}
        rows={jobs.entries.map(entry => ({
          key: entry.job_id, kind: 'Preparation', target: entry.names.slice(0, 3).join(', ') + (entry.names.length > 3 ? '…' : ''),
          status: entry.status, progress: `${entry.documents_completed + entry.documents_failed}/${entry.documents_total} documents`,
          createdAt: entry.created_at, owner: entry.owner, open: entry.job_id === prepareJob?.job_id, onOpen: () => openJob(entry),
        }))} />

      {showNewColModal && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-80 shadow-xl">
            <h2 className="font-semibold mb-4">New Collection</h2>
            <input value={newColName} onChange={e => setNewColName(e.target.value)} placeholder="Collection name" className="border rounded px-3 py-2 text-sm w-full mb-3" />
            <select value={newColIndexType} onChange={e => setNewColIndexType(e.target.value)} className="border rounded px-3 py-2 text-sm w-full mb-4">
              <option value="hnsw">HNSW (Approximate)</option>
              <option value="flat">Flat (Exact KNN)</option>
            </select>
            {newColError && <p role="alert" className="text-red-600 text-sm mb-4">{newColError}</p>}
            <div className="flex justify-end gap-2">
              <button onClick={() => { setShowNewColModal(false); setNewColError('') }} className="text-sm text-gray-500 hover:text-gray-700">Cancel</button>
              <button onClick={createCollection} className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">Create</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
