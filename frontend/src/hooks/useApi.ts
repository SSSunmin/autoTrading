import { useEffect, useState, type DependencyList } from 'react'
import { ApiError } from '../api/client'

export interface ApiState<T> {
  data: T | null
  error: string | null
  status: number | null
  loading: boolean
}

/** Fetch once per dependency change. Errors keep the message the API sent (docs/04: Korean detail strings). */
export function useApi<T>(load: () => Promise<T>, deps: DependencyList): ApiState<T> {
  const [state, setState] = useState<ApiState<T>>({ data: null, error: null, status: null, loading: true })
  // The caller's deps are the dependency list; the lint rule cannot see through the indirection.
  // oxlint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    let alive = true
    setState((s) => ({ ...s, loading: true, error: null }))
    load().then(
      (data) => alive && setState({ data, error: null, status: 200, loading: false }),
      (err: unknown) => {
        if (!alive) return
        const status = err instanceof ApiError ? err.status : null
        const message = err instanceof Error ? err.message : String(err)
        setState({ data: null, error: message, status, loading: false })
      },
    )
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return state
}
