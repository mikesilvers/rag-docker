import { useState, useEffect, useRef } from 'react'
import { api, CollectionInfo, IngestConfig } from '../api/client'
import StrategyExplainer from '../components/StrategyExplainer'
import { useRole } from '../context/AuthContext'

const STRATEGIES = ['fixed', 'overlap', 'language', 'context_aware', 'semantic']

export default function ChunkingPage() {
  const { role } = useRole()
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [collection, setCollection] = useState('')
  const [config, setConfig] = useState<IngestConfig | null>(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')

  const [pendingCollections, setPendingCollections] = useState<string[]>([])
  const saving = pendingCollections.includes(collection)
  const generation = useRef(0)
  const loadedGeneration = useRef(-1)
  const saveTicket = useRef(0)
  const selected = useRef('')
  const pendingSaves = useRef(new Set<string>())

  function selectCollection(name: string) {
    if (name === selected.current) return
    selected.current = name
    generation.current++
    setCollection(name)
    setConfig(null)
    setSaved(false)
    setError('')
  }

  useEffect(() => {
    let cancelled = false
    api.getCollections().then(r => {
      if (cancelled) return
      setCollections(r.collections)
      if (r.collections.length > 0) selectCollection(r.collections[0].name)
    }).catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)) })
    return () => { cancelled = true; generation.current++ }
  }, [])

  useEffect(() => {
    if (!collection || saving || loadedGeneration.current === generation.current) return
    const version = generation.current
    let cancelled = false
    api.getIngestConfig(collection).then(value => {
      if (!cancelled && version === generation.current) {
        if (value.collection !== collection) throw new Error('The returned configuration belongs to another collection.')
        loadedGeneration.current = version
        setConfig(value)
      }
    }).catch(e => {
      if (!cancelled && version === generation.current) setError(e instanceof Error ? e.message : String(e))
    })
    return () => { cancelled = true }
  }, [collection, saving])

  async function save() {
    if (!config || config.collection !== selected.current || pendingSaves.current.has(config.collection)) return
    const version = generation.current
    const ticket = ++saveTicket.current
    const target = config.collection
    pendingSaves.current.add(target)
    setPendingCollections([...pendingSaves.current])
    setSaved(false)
    setError('')
    try {
      const value = await api.saveIngestConfig({
        collection: config.collection,
        chunking_strategy: config.chunking_strategy,
        chunk_size: config.chunk_size,
        chunk_overlap: config.chunk_overlap,
        similarity_threshold: config.similarity_threshold,
        min_chunk_size: config.min_chunk_size,
      })
      if (version !== generation.current || ticket !== saveTicket.current) return
      if (value.collection !== selected.current) throw new Error('The returned configuration belongs to another collection.')
      setConfig(value)
      setSaved(true)
      setTimeout(() => {
        if (version === generation.current && ticket === saveTicket.current) setSaved(false)
      }, 3000)
    } catch (e: unknown) {
      if (version === generation.current && ticket === saveTicket.current) setError(e instanceof Error ? e.message : String(e))
    } finally {
      // A pending write belongs to its collection even after A → B → A.
      // Returning to that collection waits, then reloads the committed value.
      pendingSaves.current.delete(target)
      setPendingCollections([...pendingSaves.current])
    }
  }

  const showOverlap = config && config.chunking_strategy !== 'semantic' && config.chunking_strategy !== 'context_aware'
  const showSimilarity = config && config.chunking_strategy === 'semantic' && role === 'engineer'

  return (
    <div className="max-w-xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Chunking Configuration</h1>
      <div className="mb-4">
        <label className="block text-sm font-medium mb-1">Collection</label>
        <select value={collection} onChange={e => selectCollection(e.target.value)} className="border rounded px-3 py-2 text-sm w-full">
          {collections.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}
        </select>
      </div>
      {collection && !config && !error && <p className="text-sm text-gray-600">Loading saved settings…</p>}
      {error && <p className="text-red-600 text-sm mt-2">{error}</p>}
      {config && config.collection === collection && (
        <fieldset disabled={saving}>
          {config.is_default && <p className="text-xs text-amber-600 bg-amber-50 border border-amber-200 rounded px-3 py-2 mb-4">Using system defaults. Save to set a custom configuration for this collection.</p>}
          <div className="mb-4">
            <label className="block text-sm font-medium mb-1">Strategy</label>
            <select value={config.chunking_strategy} onChange={e => setConfig({ ...config, chunking_strategy: e.target.value })} className="border rounded px-3 py-2 text-sm w-full">
              {STRATEGIES.map(s => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}
            </select>
            <StrategyExplainer strategy={config.chunking_strategy} />
          </div>
          <div className="grid grid-cols-2 gap-4 mb-4">
            <div>
              <label className="block text-xs text-gray-600 mb-1">Chunk Size: {config.chunk_size}</label>
              <input type="range" min={50} max={6000} step={50} value={config.chunk_size} onChange={e => { const size = +e.target.value; setConfig({ ...config, chunk_size: size, chunk_overlap: Math.min(config.chunk_overlap, Math.max(0, size - 50)) }) }} className="w-full" />
            </div>
            {showOverlap && (
              <div>
                <label className="block text-xs text-gray-600 mb-1">Overlap: {config.chunk_overlap}</label>
                <input type="range" min={0} max={Math.max(0, Math.min(2000, config.chunk_size - 50))} step={50} value={config.chunk_overlap} onChange={e => setConfig({ ...config, chunk_overlap: +e.target.value })} className="w-full" />
              </div>
            )}
            <div>
              <label className="block text-xs text-gray-600 mb-1">Min Chunk Size: {config.min_chunk_size}</label>
              <input type="range" min={0} max={6000} step={10} value={config.min_chunk_size} onChange={e => setConfig({ ...config, min_chunk_size: +e.target.value })} className="w-full" />
            </div>
            {showSimilarity && (
              <div>
                <label className="block text-xs text-gray-600 mb-1">Similarity Threshold: {config.similarity_threshold ?? 0.85}</label>
                <input type="range" min={0} max={1} step={0.05} value={config.similarity_threshold ?? 0.85} onChange={e => setConfig({ ...config, similarity_threshold: +e.target.value })} className="w-full" />
              </div>
            )}
          </div>
          <button onClick={save} className="bg-blue-600 text-white px-5 py-2 rounded text-sm hover:bg-blue-700">{saving ? 'Saving…' : 'Save as Default'}</button>
          {saved && <span className="ml-3 text-green-600 text-sm">Saved!</span>}
        </fieldset>
      )}
    </div>
  )
}
