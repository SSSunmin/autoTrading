import { useEffect, useState } from 'react'

import { api, STATE_CHANGED } from '../api/client'
import type { Market } from '../api/types'
import { useApi } from './useApi'

/** A counter bumped whenever a write (halt, resume, mode change) announces itself: list it in useApi deps to reload. */
export function useStateTick(): number {
  const [tick, setTick] = useState(0)
  useEffect(() => {
    const bump = () => setTick((t) => t + 1)
    window.addEventListener(STATE_CHANGED, bump)
    return () => window.removeEventListener(STATE_CHANGED, bump)
  }, [])
  return tick
}

/** The market's operating mode / halt state; re-fetched after a write and when the market changes. */
export function useTradingState(market: Market) {
  const tick = useStateTick()
  return useApi(() => api.mode(market), [tick, market])
}
