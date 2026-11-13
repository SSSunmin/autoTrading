import type { Market } from '../api/types'

/** The branching behind the v0.6 common parts (parts.tsx, common.tsx), kept apart so the component files export
 *  components only (fast refresh) and the tests read the rules directly. */

export type GaugeState = 'ok' | 'near' | 'over'

/** Share of the limit at which a gauge turns amber before it is breached. */
export const GAUGE_NEAR = 0.8

/** How far ``value`` has gone toward ``limit``, on the limit's side: a drawdown of −3% toward −10% is 3% of the way,
 *  a +1% day toward a −2% loss limit is none. */
export function gaugeMagnitude(value: number, limit: number): number {
  return Math.max(0, Math.sign(limit) * value)
}

/** over at or past the limit (the mockup's rule), near from GAUGE_NEAR of it. */
export function gaugeState(value: number | null, limit: number): GaugeState {
  if (value == null) return 'ok'
  const m = gaugeMagnitude(value, limit), l = Math.abs(limit)
  return m >= l - 1e-12 ? 'over' : m >= GAUGE_NEAR * l - 1e-12 ? 'near' : 'ok'
}

export const EXIT_REASONS = ['stop_loss', 'trailing', 'expired', 'exit_signal', 'force'] as const

export type Source = 'example' | 'measured' | 'proposed'
export const SOURCE_KO: Record<Source, string> = { example: '예시', measured: '실측', proposed: '제안' }

/** What the instrument cell shows: the name in bold over the ticker tag (mockup v0.6 .tk), the ticker as the market
 *  writes it (KRX 000660.KS, US AAPL). Without a name a US ticker stands alone; a KRX code says the name is missing. */
export function tickerCellParts(ticker: string, name: string | null | undefined, market: Market): { primary: string; tag: string | null } {
  if (name) return { primary: name, tag: ticker }
  return market === 'US' ? { primary: ticker, tag: null } : { primary: '회사명 미확인', tag: ticker }
}
