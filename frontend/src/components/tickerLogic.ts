import type { Order, Rating, SellReviewInfo, ThesisCheck, TickerDetail } from '../api/types'

/** The six tabs of 화면 4, in the mockup's order. */
export const TICKER_TABS = [
  { id: 'decisions', label: '결정 이력' }, { id: 'screening', label: '선별 이력' }, { id: 'outcomes', label: '결과·교훈' },
  { id: 'snapshot', label: '검증 스냅샷' }, { id: 'thesis', label: '논지 점검' }, { id: 'holding', label: '보유' },
] as const

/** The 등급 띠 of the 결정 이력 tab (mockup Ticker.dc.html): a tile per decision with the rating's short code, the
 *  MM/DD under it. */
const RATING_CODE: Record<string, string> = { Buy: 'B', Overweight: 'OW', Hold: 'H', Underweight: 'UW', Sell: 'S', REVIEW: 'R' }
export function bandTiles(timeline: TickerDetail['timeline']) {
  return timeline.map((t) => ({ ...t, code: RATING_CODE[t.rating] ?? t.rating, md: t.date.slice(5).replace('-', '/') }))
}

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

/** The earnings badge of the header: a date (or range) with its source and fetch day, or the server's note alone.
 *  ``failed``: the last refresh failed — shown as 조회 실패, never as "no date". */
export function earningsLine(d: Pick<TickerDetail, 'earnings' | 'earnings_note_ko'>): { date: string | null; sub: string; failed: boolean } {
  const e = d.earnings
  if (!e) return { date: null, sub: d.earnings_note_ko, failed: false }
  const date = e.date ? (e.date_to ? `${e.date} ~ ${e.date_to}` : e.date) : null
  return { date, sub: `${e.source ?? '출처 미상'} · ${e.fetched ? `${e.fetched} 조회` : '성공한 조회 없음'} · ${d.earnings_note_ko}`, failed: e.error != null }
}

/** The seven reasons a sell review gives (schemas.SellReason), in the schema's order. */
export const SELL_REASONS = ['target_reached', 'thesis_broken', 'better_alternative', 'overweight', 'catalyst_gone',
  'would_not_rebuy', 'hard_rule'] as const

/** What the review's order did, in one line: "Trim 10% → 54주 매도", "Exit → 120주 전량 매도", "보유 유지 · 주문 없음".
 *  The review's own order only — a rule exit (``-exit``) of the same day is the rule's. */
export function reviewOrderLine(r: Pick<SellReviewInfo, 'action' | 'trim_to_pct'>,
  orders: Pick<Order, 'order_id' | 'side' | 'quantity' | 'filled_quantity' | 'status_ko'>[]): string {
  if (r.action === 'Hold') return '보유 유지 · 주문 없음'
  const what = r.action === 'Exit' ? 'Exit' : r.trim_to_pct != null ? `Trim ${r.trim_to_pct}%` : 'Trim'
  const o = orders.find((x) => x.side === 'Sell' && !x.order_id.endsWith('-exit'))
  if (!o) return `${what} → 주문 없음`
  const qty = o.filled_quantity || o.quantity
  if (qty === 0) return `${what} → 매도 수량 없음 (${o.status_ko})`
  return `${what} → ${qty}주 ${r.action === 'Exit' ? '전량 ' : ''}매도 · ${o.status_ko}`
}
