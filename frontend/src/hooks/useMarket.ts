import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

import type { Market } from '../api/types'

export const MARKETS: Market[] = ['KRX', 'US']

/** The market the screens show, kept in the URL (`?market=US`; KRX when absent) so a reload or a shared link keeps it.
 *  Anything other than US reads as KRX here; the API itself refuses an unknown market with 400. */
export function useMarket(): [Market, (m: Market) => void] {
  const [params, setParams] = useSearchParams()
  const market: Market = params.get('market') === 'US' ? 'US' : 'KRX'
  const setMarket = useCallback((m: Market) => setParams((p) => {
    const q = new URLSearchParams(p)
    if (m === 'KRX') q.delete('market')
    else q.set('market', m)
    return q
  }), [setParams])
  return [market, setMarket]
}

/** A path with the current market kept: `to('/positions')` → '/positions?market=US' on the US screens. */
export function useMarketPath(): (path: string) => string {
  const [market] = useMarket()
  return useCallback((path: string) => {
    if (market === 'KRX') return path
    return path + (path.includes('?') ? '&' : '?') + 'market=' + market
  }, [market])
}
