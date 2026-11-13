import { money, pct } from './client'
import type { Market, Positions, TradingState } from './types'

/** What the admin resume will do, for the Settings panel (docs/04 resume, halt-semantics).
 *  The peak reset is offered only while a −20% stop stands in the resume's scope: this market (``state.dd_stop``) or,
 *  for the all-markets resume, ``others`` (names of the other markets with a stop). The preview reads the book the
 *  server resets from (/api/positions: equity at the last close). ``reset_peak`` is always sent, false when nothing
 *  is offered, so the server's default (true) never resets a peak the admin did not see. */
export interface ResumePlan {
  offerReset: boolean
  preview: string | null
  body: { reset_peak: boolean }
}

export function resumePlan(state: TradingState, book: Pick<Positions, 'equity' | 'peak' | 'drawdown' | 'as_of'> | null,
  resetPeak: boolean, market: Market, others: string[] = []): ResumePlan {
  if (!state.dd_stop && others.length === 0) return { offerReset: false, preview: null, body: { reset_peak: false } }
  const also = others.length ? `${others.join('·')}도 −20% 정지 — 같은 선택이 그 시장에도 적용됩니다` : null
  let own: string | null = null
  if (state.dd_stop) {
    own = !book ? '장부를 읽는 중'
      : resetPeak ? `피크 ${money(book.peak, market)} → ${money(book.equity, market)}(${book.as_of} 종가 평가액) · 드로다운 ${pct(book.drawdown)} → 0.0%`
        : `피크 ${money(book.peak, market)} 유지 · 드로다운 ${pct(book.drawdown)} — −20% 아래면 다음 실행에서 다시 정지합니다`
  }
  return { offerReset: true, preview: [own, also].filter(Boolean).join(' · '), body: { reset_peak: resetPeak } }
}
