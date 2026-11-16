import type { Rating, ThesisCheck, TickerDetail } from '../api/types'

/** The six tabs of 화면 4, in the mockup's order. */
export const TICKER_TABS = [
  { id: 'decisions', label: '결정 이력' }, { id: 'screening', label: '선별 이력' }, { id: 'outcomes', label: '결과·교훈' },
  { id: 'snapshot', label: '검증 스냅샷' }, { id: 'thesis', label: '논지 점검' }, { id: 'holding', label: '보유' },
] as const

/** A verdict as a rating chip, the KRX colours (red up, blue down): 유지 neutral, 축소 · 청산 the sell side. */
export function verdictBadge(c: Pick<ThesisCheck, 'verdict' | 'verdict_ko'>): { rating: Rating | 'pending'; label: string } {
  const rating = ({ keep: 'Hold', trim: 'Underweight', exit: 'Sell', memo: 'pending' } as const)[c.verdict]
  return { rating, label: c.verdict_ko }
}

/** The latest sell review, which the tab's summary line reads; null when there is none (memos only, or nothing). */
export function latestReview(checks: ThesisCheck[]): ThesisCheck | null {
  return [...checks].reverse().find((c) => c.kind === 'review') ?? null
}

/** The summary under the verdict: "2개 조건 중 1개 충족". */
export function metLine(c: Pick<ThesisCheck, 'met' | 'total'>): string {
  return c.met == null ? `깨짐 조건 ${c.total}개` : `${c.total}개 조건 중 ${c.met}개 충족`
}

/** The earnings badge of the header: a date (or range) with its source and fetch day, or the server's note alone. */
export function earningsLine(d: Pick<TickerDetail, 'earnings' | 'earnings_note_ko'>): { date: string | null; sub: string } {
  const e = d.earnings
  if (!e) return { date: null, sub: d.earnings_note_ko }
  const date = e.date ? (e.date_to ? `${e.date} ~ ${e.date_to}` : e.date) : null
  return { date, sub: `${e.source ?? '출처 미상'} · ${e.fetched ?? '?'} 조회 · ${d.earnings_note_ko}` }
}
