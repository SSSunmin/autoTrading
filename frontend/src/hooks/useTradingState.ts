import { useEffect, useState } from 'react'

import { api, STATE_CHANGED } from '../api/client'
import { useApi } from './useApi'

/** The operating mode / halt state; re-fetched whenever a write (halt, resume, mode change) announces itself. */
export function useTradingState() {
  const [tick, setTick] = useState(0)
  useEffect(() => {
    const bump = () => setTick((t) => t + 1)
    window.addEventListener(STATE_CHANGED, bump)
    return () => window.removeEventListener(STATE_CHANGED, bump)
  }, [])
  return useApi(() => api.mode(), [tick])
}
