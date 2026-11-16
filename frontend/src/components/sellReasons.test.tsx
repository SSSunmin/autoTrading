import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import type { Order, SellReviewInfo, TickerDetail } from '../api/types'
import { DecisionCard } from './DecisionCard'
import { ReviewBadges, TickerFacts } from './ticker'
import { earningsLine, reviewOrderLine, SELL_REASONS } from './tickerLogic'

// the server's labels (krx_names.SELL_REASON_KO), written out by hand
const KO: Record<string, string> = {
  target_reached: '목표가 도달', thesis_broken: '논지 깨짐', better_alternative: '더 좋은 대안', overweight: '비중 초과',
  catalyst_gone: '촉매 소멸', would_not_rebuy: '재매수 테스트 실패', hard_rule: '하드룰 발동',
}
const review = (over: Partial<SellReviewInfo>): SellReviewInfo => ({
  action: 'Trim', verdict: 'trim', verdict_ko: '축소', reasons: ['thesis_broken', 'overweight'], reasons_ko: ['논지 깨짐', '비중 초과'],
  trim_to_pct: 10, met: 1, total: 2, ...over,
})
const order = (o: Partial<Order>): Order => ({
  order_id: 'paper-2026-06-02-A.KS', ticker: 'A.KS', name: '에이', trade_date: '2026-06-02', side: 'Sell', side_ko: '매도', quantity: 54,
  filled_quantity: 54, filled_price: 10_000, status: 'filled', status_ko: '체결', exception_kind: null, reason: '', rules_applied: [],
  market: 'KRX', fill_at_local: null, fill_at_kst: null, fill_planned: false, plan: null, exception_action: null,
  exception_resolved: false, reruns: 0, retries: 0, retry_on: null, rerun_answer: null, placed_at: '2026-06-02T07:00:00Z', ...o,
} as unknown as Order)
const card = (d: object) => renderToStaticMarkup(<MemoryRouter initialEntries={['/decisions?market=KRX']}>
  <DecisionCard d={{ ticker: 'A.KS', name: '에이', trade_date: '2026-06-02', rating: 'Underweight', rating_ko: '비중 축소', source: 'agent',
    source_kind: 'agent', source_kind_ko: '에이전트 결정', evidence: [], hard_rule_overrides: [], thesis: '', market: 'KRX', tradeable: true,
    orders: [], review: null, ...d } as never} />
</MemoryRouter>)

describe('sell reason badges', () => {
  it('shows all seven reasons with their labels, each its own class', () => {
    const r = review({ reasons: [...SELL_REASONS], reasons_ko: SELL_REASONS.map((x) => KO[x]) })
    const out = renderToStaticMarkup(<ReviewBadges r={r} />)
    for (const x of SELL_REASONS) expect(out).toContain(`<span class="tag sell-reason ${x}">${KO[x]}</span>`)
    expect(SELL_REASONS).toHaveLength(7)
  })

  it('names its source unless told not to, and uses the 논지 점검 verdict chip', () => {
    const out = renderToStaticMarkup(<ReviewBadges r={review({})} />)
    expect(out).toContain('<span class="tag review-src">에이전트 매도 검토</span>')
    expect(out).toContain('<span class="rating Underweight">축소</span>')
    expect(out).toContain('목표 비중 10%')
    expect(renderToStaticMarkup(<ReviewBadges r={review({})} source={false} />)).not.toContain('review-src')
    const hold = renderToStaticMarkup(<ReviewBadges r={review({ action: 'Hold', verdict: 'keep', verdict_ko: '논지 유지', reasons: [], reasons_ko: [], trim_to_pct: null })} />)
    expect(hold).toContain('<span class="rating Hold">논지 유지</span>')
    expect(hold).not.toContain('sell-reason')
    expect(hold).not.toContain('목표 비중')
  })

  it('links the review to its own order, never the rule exit of the day', () => {
    expect(reviewOrderLine(review({}), [order({})])).toBe('Trim 10% → 54주 매도 · 체결')
    expect(reviewOrderLine(review({ action: 'Exit', trim_to_pct: null }), [order({ order_id: 'x-exit', quantity: 9, filled_quantity: 9 }),
      order({ quantity: 120, filled_quantity: 120 })])).toBe('Exit → 120주 전량 매도 · 체결')
    expect(reviewOrderLine(review({ action: 'Exit' }), [order({ order_id: 'x-exit' })])).toBe('Exit → 주문 없음')
    expect(reviewOrderLine(review({ action: 'Hold' }), [])).toBe('보유 유지 · 주문 없음')
    expect(reviewOrderLine(review({ trim_to_pct: 20 }), [order({ quantity: 0, filled_quantity: 0, status: 'blocked', status_ko: '차단' })]))
      .toBe('Trim 20% → 매도 수량 없음 (차단)')
    expect(reviewOrderLine(review({ quantity: 0 } as never), [order({ status: 'open', status_ko: '미체결', filled_quantity: 0 })]))
      .toBe('Trim 10% → 54주 매도 · 미체결')
  })
})

describe('decision card: the agent review apart from a rule exit', () => {
  it('a review card carries its badges and order line', () => {
    const out = card({ review: review({}), orders: [order({})] })
    expect(out).toContain('에이전트 매도 검토')
    expect(out).toContain('<span class="tag sell-reason thesis_broken">논지 깨짐</span>')
    expect(out).toContain('Trim 10% → 54주 매도 · 체결')
  })
  it('a rule exit card has no agent sell-review badge', () => {
    const out = card({ source: 'rule', source_kind: 'rule', source_kind_ko: '규칙 청산', rating: 'Sell', rating_ko: '매도',
      orders: [order({ order_id: 'paper-2026-06-02-A.KS-exit', rules_applied: ['stop_loss'] })] })
    expect(out).not.toContain('review-badges')
    expect(out).not.toContain('에이전트 매도 검토')
    expect(out).toContain('규칙 청산')
  })
})

describe('earnings: a failed lookup is not "no date"', () => {
  const d = (e: TickerDetail['earnings'], note: string) => ({ earnings: e, earnings_note_ko: note })
  const base = { date_to: null, source: 'yfinance calendar', fetched: '2026-11-16' }
  it('flags a failure, and leaves a missing date unflagged', () => {
    expect(earningsLine(d({ ...base, date: null, error: 'HTTPError: 404', failed: '2026-11-16' }, '마지막 갱신 실패')).failed).toBe(true)
    expect(earningsLine(d({ ...base, date: null, error: null, failed: null }, '출처에 예정일 없음')).failed).toBe(false)
  })
  it('the header says 조회 실패 for a failure and – for no date', () => {
    const facts = (e: TickerDetail['earnings'], note: string) => renderToStaticMarkup(<MemoryRouter><TickerFacts d={{ ticker: 'ZZZZQ', name: null,
      market: 'US', sector: null, exchange: null, holding: null, ...d(e, note) } as TickerDetail} /></MemoryRouter>)
    const failed = facts({ ...base, date: null, error: 'HTTPError: 404', failed: '2026-11-16' }, '마지막 갱신 실패(2026-11-16: HTTPError: 404)')
    expect(failed).toContain('<span class="tag fail-tag">조회 실패</span>')
    expect(failed).not.toContain('<b>–</b>')
    const never = earningsLine(d({ ...base, date: null, fetched: null, error: 'HTTPError: 404', failed: '2026-11-16' }, '마지막 갱신 실패'))
    expect(never.sub).toBe('yfinance calendar · 성공한 조회 없음 · 마지막 갱신 실패')
    const none = facts({ ...base, date: null, error: null, failed: null }, '출처에 예정일 없음')
    expect(none).toContain('<b>–</b>')
    expect(none).not.toContain('조회 실패')
    const old = facts({ ...base, date: '2026-07-30', error: 'HTTPError: 429', failed: '2026-11-16' }, '마지막 갱신 실패 — 이전 값')
    expect(old).toContain('<b>2026-07-30</b><span class="tag fail-tag">갱신 실패 · 이전 값</span>')
  })
})
