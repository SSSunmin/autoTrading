import type { Health, LoopStatus, Market, Positions } from '../api/types'

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

// the rules' five, then the agent's selling sell review (포지션 only, server execution.AGENT_SELL) — counted apart
export const EXIT_REASONS = ['stop_loss', 'trailing', 'expired', 'exit_signal', 'force', 'agent_sell'] as const

export type Source = 'example' | 'measured' | 'proposed'
export const SOURCE_KO: Record<Source, string> = { example: '예시', measured: '실측', proposed: '제안' }

/** What the instrument cell shows: the name in bold over the ticker tag (mockup v0.6 .tk), the ticker as the market
 *  writes it (KRX 000660.KS, US AAPL). Without a name a US ticker stands alone; a KRX code says the name is missing. */
export function tickerCellParts(ticker: string, name: string | null | undefined, market: Market): { primary: string; tag: string | null } {
  if (name) return { primary: name, tag: ticker }
  return market === 'US' ? { primary: ticker, tag: null } : { primary: '회사명 미확인', tag: ticker }
}

/** The status bar's run chip: the server's next stage (scheduler plan), or the retry when a due stage failed. */
export function runChipText(loop: LoopStatus | undefined, kstTime: (iso: string) => string): { text: string; failed: boolean } {
  if (!loop) return { text: '다음 실행 확인 중', failed: false }
  if (loop.status === 'failed' && loop.retry_at_kst) return { text: `실행 실패 · 재시도 ${kstTime(loop.retry_at_kst)} KST`, failed: true }
  return { text: `다음 실행 ${loop.next.label_ko}`, failed: loop.status === 'failed' }
}

export interface BandLine { kind: 'loop' | 'refresh'; title: string; detail: string; since: string | null }

/** The failure band under the status bar (mockup v0.7.2 States ④): a scheduled stage whose latest record failed, a
 *  cache refresh that left symbols out. Gone once both are fine. */
export function failureBand(h: Pick<Health, 'loop' | 'refresh'> | undefined): BandLine[] {
  if (!h) return []
  const out: BandLine[] = h.loop.failed.map((r) => ({
    kind: 'loop' as const, title: `일일 루프 ${r.stage_ko} 실패 · ${r.trade_date}`,
    detail: r.error ?? r.reason ?? '오류 기록 없음', since: r.ended_at,
  }))
  const f = h.refresh
  if (f && f.failed_count > 0) {
    out.push({ kind: 'refresh', title: `캐시 갱신 실패 · ${f.failed_count} / ${f.symbols ?? '?'} 심볼`,
      detail: `${f.failed_symbols.join(' · ')}${f.failed_count > f.failed_symbols.length ? ' …' : ''}${f.failed_error ? ` — ${f.failed_error}` : ''}`,
      since: f.last_success_at })
  }
  return out
}

/** Sector gauge rows: one per classified sector against the cap; 미분류 has no sector and so no cap (shown apart). */
export function sectorGauges(p: Pick<Positions, 'sector_exposure' | 'limits'>): { capped: { label: string; value: number }[]; unclassified: number | null; empty: boolean } {
  const rows = p.sector_exposure ?? []
  return {
    capped: rows.filter((r) => r.classified).map((r) => ({ label: `섹터 · ${r.sector}`, value: r.pct })),
    unclassified: rows.find((r) => !r.classified)?.pct ?? null,
    empty: rows.length === 0,
  }
}
