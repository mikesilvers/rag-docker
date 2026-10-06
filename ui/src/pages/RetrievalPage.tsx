import { useEffect, useRef, useState } from 'react'
import { api, CollectionInfo } from '../api/client'
import { useRole } from '../context/RoleContext'
import { useQueryConfig, QueryConfig } from '../context/QueryConfigContext'

const MODES = [
  { id: 'hnsw', label: 'Vector — existing index', description: 'Finds similar chunks using the physical index already configured for this collection. Choosing this method does not switch between HNSW and Flat.' },
  { id: 'hybrid', label: 'Hybrid', description: 'Combines keyword search with meaning-based search. Best when your questions include specific terms, names, or codes. Adjust the slider to balance between the two modes.' },
  { id: 'semantic', label: 'Semantic', description: 'Pure meaning-based search. Best for conceptual questions where the exact words are less important than the idea.' },
]

export default function RetrievalPage() {
  const { role } = useRole()
  const { collection, setCollection, config, isDefault, loading, error, saveConfig } = useQueryConfig()
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [mode, setMode] = useState(config.retrieval_mode === 'flat' ? 'hnsw' : config.retrieval_mode)
  const [topK, setTopK] = useState(config.top_k)
  const [alpha, setAlpha] = useState(config.alpha)
  const [indexError, setIndexError] = useState('')
  const [indexLoading, setIndexLoading] = useState(false)
  const [indexRead, setIndexRead] = useState(false)
  const [applied, setApplied] = useState(false)
  const [saveError, setSaveError] = useState('')
  const indexRequest = useRef(0)
  const saveRequest = useRef(0)

  useEffect(() => {
    const ticket = ++indexRequest.current
    api.getCollections().then(r => {
      if (ticket !== indexRequest.current) return
      setIndexError('')
      setCollections(r.collections)
      setIndexRead(true)
      if (!collection && r.collections.length > 0) setCollection(r.collections[0].name)
    }).catch(() => { if (ticket === indexRequest.current) { setIndexError('Could not read the current physical index.'); setIndexRead(true) } })
    return () => { indexRequest.current++ }
    // Runs once; picking a default collection must not fight the user's choice.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Saved settings arrive asynchronously and change whenever another
  // collection is picked, so the form mirrors the context rather than owning
  // the values. `applied` is deliberately not reset here: a save replaces
  // `config`, which would otherwise clear the confirmation immediately.
  useEffect(() => {
    setMode(config.retrieval_mode === 'flat' ? 'hnsw' : config.retrieval_mode)
    setTopK(config.top_k)
    setAlpha(config.alpha)
  }, [config])

  useEffect(() => {
    saveRequest.current++
    setApplied(false)
    setSaveError('')
    return () => { saveRequest.current++ }
  }, [collection])

  async function apply() {
    const ticket = ++saveRequest.current
    setApplied(false)
    const next: QueryConfig = {
      retrieval_mode: mode,
      top_k: topK,
      alpha,
      ef: null, // Legacy overrides were saved but never applied to query execution.
      // The role toggle drives the answer style live; persisting it here is
      // what makes the stored config usable by an exported retrieval script.
      response_format: role === 'end_user' ? 'end_user' : 'engineer',
    }
    setSaveError('')
    try {
      const saved = await saveConfig(next)
      if (!saved || ticket !== saveRequest.current) return
      setApplied(true)
      setTimeout(() => { if (ticket === saveRequest.current) setApplied(false) }, 3000)
    } catch (e: unknown) {
      if (ticket !== saveRequest.current) return
      setSaveError(e instanceof Error ? e.message : String(e))
    }
  }

  const physicalIndex = collections.find(c => c.name === collection)

  async function refreshIndex() {
    const ticket = ++indexRequest.current
    setIndexLoading(true); setIndexError('')
    try {
      const result = await api.getCollections()
      if (ticket !== indexRequest.current) return
      setCollections(result.collections)
      if (!collection && result.collections.length > 0) setCollection(result.collections[0].name)
    }
    catch { if (ticket === indexRequest.current) setIndexError('Could not refresh the physical index; displayed details are from the prior read.') }
    finally { if (ticket === indexRequest.current) { setIndexLoading(false); setIndexRead(true) } }
  }

  return (
    <div className="max-w-xl mx-auto">
      <h1 className="text-2xl font-bold mb-2">Retrieval Configuration</h1>
      <p className="text-sm text-gray-500 mb-6">
        Settings are saved per collection on the server, so they survive a restart and travel with an export.
      </p>

      <div className="mb-4">
        <label className="block text-sm font-medium mb-2">Collection</label>
        <select
          value={collection}
          onChange={e => setCollection(e.target.value)}
          className="w-full border rounded px-3 py-2 text-sm"
        >
          {collections.length === 0 && <option value="">No collections yet</option>}
          {collections.map(c => (
            <option key={c.name} value={c.name}>{c.name} ({c.object_count} chunks)</option>
          ))}
        </select>
        <p className="text-xs text-gray-500 mt-1">
          {loading
            ? 'Loading saved settings…'
            : collection
              ? isDefault
                ? 'No settings saved for this collection yet — showing defaults.'
                : 'Showing the settings saved for this collection.'
              : 'Create a collection to configure retrieval.'}
        </p>
        {error && <p className="text-xs text-amber-600 mt-1">Could not load saved settings ({error}). Showing defaults.</p>}
      </div>

      <div className="mb-4">
        <label className="block text-sm font-medium mb-2">Retrieval Mode</label>
        <div className="space-y-2">
          {MODES.map(m => (
            <label key={m.id} className={`flex gap-3 p-3 border rounded cursor-pointer ${mode === m.id ? 'border-blue-500 bg-blue-50' : 'hover:bg-gray-50'}`}>
              <input type="radio" name="mode" value={m.id} checked={mode === m.id} onChange={() => setMode(m.id)} className="mt-1" />
              <div>
                <div className="text-sm font-medium">{m.label}</div>
                <div className="text-xs text-gray-500">{m.description}</div>
              </div>
            </label>
          ))}
        </div>
      </div>

      <div className="mb-4">
        <label className="block text-xs text-gray-600 mb-1">Top-K Results: {topK}</label>
        <input type="range" min={1} max={50} value={topK} onChange={e => setTopK(+e.target.value)} className="w-full" />
      </div>

      {mode === 'hybrid' && (
        <div className="mb-4">
          <label className="block text-xs text-gray-600 mb-1">
            Keyword ← Balance → Meaning: {alpha}
          </label>
          <input type="range" min={0} max={1} step={0.05} value={alpha} onChange={e => setAlpha(+e.target.value)} className="w-full" />
        </div>
      )}

      <div className="mb-4 border rounded p-3 text-sm">
        <h2 className="font-semibold mb-2">Physical index (read only)</h2>
        {indexError && <p className="text-amber-700">{indexError}</p>}
        {physicalIndex ? <>
          <p>Type: {physicalIndex.index_type} · Distance: {physicalIndex.distance_metric}</p>
          {physicalIndex.hnsw_config && <p className="mt-1">ef: {physicalIndex.hnsw_config.ef} · efConstruction: {physicalIndex.hnsw_config.efConstruction} · maxConnections: {physicalIndex.hnsw_config.maxConnections}</p>}
          <p className="text-xs text-gray-500 mt-1">Observed when index details were last refreshed. Saved query methods do not rebuild the index.</p>
        </> : <p>{indexRead ? 'Index details are unavailable for this collection.' : 'Reading index details…'}</p>}
        {config.ef !== null && <p className="text-xs text-amber-700 mt-2">The legacy saved ef override ({config.ef}) is inactive. Queries use the physical index settings; saving here clears that override.</p>}
        <button onClick={refreshIndex} disabled={indexLoading} className="mt-2 border rounded px-2 py-1 text-xs disabled:opacity-50">Refresh index details</button>
      </div>

      <button
        onClick={apply}
        disabled={!collection || loading}
        className="bg-blue-600 text-white px-5 py-2 rounded text-sm hover:bg-blue-700 disabled:opacity-50"
      >
        Save for this collection
      </button>
      {applied && <span className="ml-3 text-green-600 text-sm">Saved!</span>}
      {saveError && <p className="text-red-600 text-sm mt-3">{saveError}</p>}
    </div>
  )
}
