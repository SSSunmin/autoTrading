import { useEffect, useState, type DependencyList } from 'react'
import { ApiError } from '../api/client'

export interface ApiState<T> {
  data: T | null
  error: string | null
  status: number | null
  loading: boolean
}

const LOADING = { data: null, error: null, status: null, loading: true } as const

function sameDeps(a: DependencyList, b: DependencyList): boolean {
  return a.length === b.length && a.every((x, i) => Object.is(x, b[i]))
}

/** Fetch once per dependency change. Errors keep the message the API sent (docs/04: Korean detail strings).
 *  A result is kept with the deps it was loaded for and only returned while those are still the deps: on a change
 *  (a market switch) the very next render already shows loading, never the old market's numbers in the new
 *  market's currency, and a late response for the old deps is dropped (alive flag). */
export function useApi<T>(load: () => Promise<T>, deps: DependencyList): ApiState<T> {
  const [state, setState] = useState<ApiState<T> & { deps: DependencyList | null }>({ ...LOADING, deps: null })
  // The caller's deps are the dependency list; the lint rule cannot see through the indirection.
  // oxlint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    let alive = true
    load().then(
      (data) => alive && setState({ data, error: null, status: 200, loading: false, deps }),
      (err: unknown) => {
        if (!alive) return
        const status = err instanceof ApiError ? err.status : null
        const message = err instanceof Error ? err.message : String(err)
        setState({ data: null, error: message, status, loading: false, deps })
      },
    )
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  if (state.deps === null || !sameDeps(state.deps, deps)) return LOADING
  const { data, error, status, loading } = state
  return { data, error, status, loading }
}
