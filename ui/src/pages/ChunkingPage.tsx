import { useState, useEffect } from 'react'
import { api, CollectionInfo, IngestConfig } from '../api/client'
import StrategyExplainer from '../components/StrategyExplainer'
import { useRole } from '../context/RoleContext'

const STRATEGIES = ['fixed', 'overlap', 'language', 'context_aware', 'semantic']

export default function ChunkingPage() {
  const { role } = useRole()
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [collection, setCollection] = useState('')
  const [config, setConfig] = useState<IngestConfig | null>(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    api.getCollections().then(r => {
      setCollections(r.collections)
      if (r.collections.length > 0) setCollection(r.collections[0].name)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!collection) return
    api.getIngestConfig(collection).then(setConfig).catch(() => {})
  }, [collection])

  async function save() {
    if (!config) return
    setError('')
    try {
      await api.saveIngestConfig({
        collection,
        chunking_strategy: config.chunking_strategy,
        chunk_size: config.chunk_size,
        chunk_overlap: config.chunk_overlap,
        similarity_threshold: config.similarity_threshold,
        min_chunk_size: config.min_chunk_size,
      })
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
      api.getIngestConfig(collection).then(setConfig)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const showOverlap = config && config.chunking_strategy !== 'semantic' && config.chunking_strategy !== 'context_aware'
  const showSimilarity = config && config.chunking_strategy === 'semantic' && role === 'engineer'

  return (
    <div className="max-w-xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Chunking Configuration</h1>
      <div className="mb-4">
        <label className="block text-sm font-medium mb-1">Collection</label>
        <select value={collection} onChange={e => setCollection(e.target.value)} className="border rounded px-3 py-2 text-sm w-full">
          {collections.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}
        </select>
      </div>
      {config && (
        <>
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
              <input type="range" min={50} max={6000} step={50} value={config.chunk_size} onChange={e => setConfig({ ...config, chunk_size: +e.target.value })} className="w-full" />
            </div>
            {showOverlap && (
              <div>
                <label className="block text-xs text-gray-600 mb-1">Overlap: {config.chunk_overlap}</label>
                <input type="range" min={0} max={2000} step={50} value={config.chunk_overlap} onChange={e => setConfig({ ...config, chunk_overlap: +e.target.value })} className="w-full" />
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
          <button onClick={save} className="bg-blue-600 text-white px-5 py-2 rounded text-sm hover:bg-blue-700">Save as Default</button>
          {saved && <span className="ml-3 text-green-600 text-sm">Saved!</span>}
          {error && <p className="text-red-600 text-sm mt-2">{error}</p>}
        </>
      )}
    </div>
  )
}
