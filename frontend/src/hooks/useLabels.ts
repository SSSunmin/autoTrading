import { api } from '../api/client'
import type { Labels } from '../api/types'
import { useApi } from './useApi'

let cached: Promise<Labels> | null = null

/** /api/labels once per page load (labels, market currency / index / session). A failed load is retried next time. */
export function useLabels() {
  return useApi(() => (cached ??= api.labels().catch((e: unknown) => { cached = null; throw e })), [])
}
