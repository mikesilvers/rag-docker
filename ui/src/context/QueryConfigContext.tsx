import { createContext, useCallback, useContext, useEffect, useRef, useState, ReactNode } from 'react'
import { api, RetrievalConfig } from '../api/client'

export interface QueryConfig {
  retrieval_mode: string
  top_k: number
  alpha: number
  ef: number | null
  response_format: string
}

// Mirrors api/services/retrieval_config.py DEFAULTS. Used before a collection
// is chosen and as the fallback when the API cannot be reached.
export const DEFAULT_CONFIG: QueryConfig = {
  retrieval_mode: 'hnsw',
  top_k: 5,
  alpha: 0.75,
  ef: null,
  response_format: 'end_user',
}

interface QueryConfigValue {
  collection: string
  setCollection: (name: string) => void
  config: QueryConfig
  /** True while the collection has no saved settings and is using DEFAULT_CONFIG. */
  isDefault: boolean
  loading: boolean
  error: string
  saveConfig: (config: QueryConfig) => Promise<void>
}

const QueryConfigContext = createContext<QueryConfigValue | null>(null)

function fromResponse(r: RetrievalConfig): QueryConfig {
  return {
    retrieval_mode: r.retrieval_mode,
    top_k: r.top_k,
    alpha: r.alpha,
    ef: r.ef,
    response_format: r.response_format,
  }
}

export function QueryConfigProvider({ children }: { children: ReactNode }) {
  const [collection, setCollectionState] = useState('')
  const [config, setConfigState] = useState<QueryConfig>(DEFAULT_CONFIG)
  const [isDefault, setIsDefault] = useState(true)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  // Settings are fetched per collection, so a slow response for a collection
  // the user has already navigated away from must not overwrite the current
  // one. Every load carries a ticket; only the latest ticket may apply.
  const requestId = useRef(0)
  const selectedCollection = useRef('')
  const saveId = useRef(0)

  const setCollection = useCallback((name: string) => {
    if (name === selectedCollection.current) return
    selectedCollection.current = name
    // Invalidate immediately, before the next effect runs. A -> B -> A is
    // also a new generation even though the collection name matches again.
    requestId.current++
    setCollectionState(name)
  }, [])

  useEffect(() => {
    const ticket = ++requestId.current
    if (!collection) {
      setConfigState(DEFAULT_CONFIG)
      setIsDefault(true)
      setError('')
      setLoading(false)
      return
    }
    setLoading(true)
    setError('')
    api
      .getRetrievalConfig(collection)
      .then(r => {
        if (ticket !== requestId.current) return
        setConfigState(fromResponse(r))
        setIsDefault(r.is_default)
        setLoading(false)
      })
      .catch((e: unknown) => {
        if (ticket !== requestId.current) return
        // Falling back to defaults keeps Q&A answerable when the settings
        // endpoint is unavailable, rather than blocking the page.
        setConfigState(DEFAULT_CONFIG)
        setIsDefault(true)
        setError(e instanceof Error ? e.message : String(e))
        setLoading(false)
      })
    return () => { requestId.current++ }
  }, [collection])

  const saveConfig = useCallback(
    async (next: QueryConfig) => {
      if (!collection) throw new Error('Select a collection before saving retrieval settings.')
      const ticket = requestId.current
      const saveTicket = ++saveId.current
      const saved = await api.saveRetrievalConfig({ collection, ...next })
      // A save belongs to the selection generation that started it. An old
      // save must not publish into another collection or cancel its load.
      // Of concurrent saves, only the latest started may publish.
      if (
        collection !== selectedCollection.current ||
        ticket !== requestId.current ||
        saveTicket !== saveId.current
      ) return
      // A completed save supersedes any load still in flight for this
      // collection, which would otherwise land afterwards with stale values.
      requestId.current++
      setConfigState(fromResponse(saved))
      setIsDefault(saved.is_default)
      setError('')
      setLoading(false)
    },
    [collection],
  )

  return (
    <QueryConfigContext.Provider
      value={{ collection, setCollection, config, isDefault, loading, error, saveConfig }}
    >
      {children}
    </QueryConfigContext.Provider>
  )
}

export function useQueryConfig() {
  const ctx = useContext(QueryConfigContext)
  if (!ctx) throw new Error('useQueryConfig must be used within QueryConfigProvider')
  return ctx
}
