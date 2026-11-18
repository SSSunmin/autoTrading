import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import type { Order, SellReviewInfo, TickerDetail } from '../api/types'
import { REASON_KO } from './cardLogic'
import { DecisionCard } from './DecisionCard'
import { ReviewBadges, TickerFacts } from './ticker'
import { earningsLine, reviewOrderLine, SELL_REASONS } from './tickerLogic'

// the server's labels (krx_names.SELL_REASON_KO, the v0.8 words since sell-label-unify), written out by hand
const KO: Record<string, string> = {
  target_reached: '목표가 도달', thesis_broken: '논지 훼손', better_alternative: '더 나은 대안', overweight: '비중 과다',
  catalyst_gone: '촉매 소멸', would_not_rebuy: '다시 사지 않음', hard_rule: '하드 룰',
}
const review = (over: Partial<SellReviewInfo>): SellReviewInfo => ({
  action: 'Trim', verdict: 'trim', verdict_ko: '축소', reasons: ['thesis_broken', 'overweight'], reasons_ko: ['논지 훼손', '비중 과다'],
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
    orders: [], review: null, settings: {}, card: { kind: 'text', label: null, text: '', missing: [], extra: [], complete: true },
    price_ctx: { close: 10_500, avg_price: 10_000, stop_loss_pct: -0.08, realized: {} }, ...d } as never} />
</MemoryRouter>)

describe('sell reason badges', () => {
  it('shows all seven reasons in the v0.8 order and words: 하드 룰 amber, the first other filled, the rest outlined', () => {
    const r = review({ reasons: [...SELL_REASONS], reasons_ko: SELL_REASONS.map((x) => KO[x]) })
    const out = renderToStaticMarkup(<ReviewBadges r={r} />)
    const chips = [...out.matchAll(/<span class="rc (\w+)">([^<]+)<\/span>/g)].map((m) => `${m[1]}:${m[2]}`)
    expect(chips).toEqual(['hard:하드 룰', 'sub:논지 훼손', 'sub:목표가 도달', 'sub:촉매 소멸', 'sub:더 나은 대안', 'sub:비중 과다', 'sub:다시 사지 않음'])
    expect(REASON_KO).toEqual(KO)                                       // the cards say what the server says
    // a table cell (positions): the main reason and "+n"
    const cell = renderToStaticMarkup(<ReviewBadges r={review({})} max={1} />)
    expect(cell).toContain('<span class="rc main">논지 훼손</span><span class="rc more">+1</span>')
    expect(cell).not.toContain('비중 과다')
  })

  it('names its source unless told not to, and uses the 논지 점검 verdict chip', () => {
    const out = renderToStaticMarkup(<ReviewBadges r={review({})} />)
    expect(out).toContain('<span class="tag review-src">에이전트 매도 검토</span>')
    expect(out).toContain('<span class="rating Underweight">축소</span>')
    expect(out).toContain('목표 비중 10%')
    expect(renderToStaticMarkup(<ReviewBadges r={review({})} source={false} />)).not.toContain('review-src')
    const hold = renderToStaticMarkup(<ReviewBadges r={review({ action: 'Hold', verdict: 'keep', verdict_ko: '논지 유지', reasons: [], reasons_ko: [], trim_to_pct: null })} />)
    expect(hold).toContain('<span class="rating Hold">논지 유지</span>')
    expect(hold).not.toContain('class="rc')
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
  it('a review card carries its kind tag, verdict, reasons and the sale it made', () => {
    const out = card({ review: review({}), orders: [order({})],
      card: { kind: 'review', action: 'Trim', trim_to_pct: 10, reasons: ['overweight', 'thesis_broken'], checks: [], reasoning: '비중이 커졌다',
        revised_target: null, missing: [], extra: [], complete: true } })
    expect(out).toContain('<span class="kind agent">에이전트 · 매도 검토</span>')
    expect(out).toContain('<span class="rating Underweight">축소</span>')
    expect(out).toContain('<span class="rc main">논지 훼손</span><span class="rc sub">비중 과다</span>')
    expect(out).toContain('매도 54주')
  })
  it('a rule exit card has no agent sell-review badge', () => {
    const out = card({ source: 'rule', source_kind: 'rule', source_kind_ko: '규칙 청산', rating: 'Sell', rating_ko: '매도',
      orders: [order({ order_id: 'paper-2026-06-02-A.KS-exit', rules_applied: ['stop_loss'] })] })
    expect(out).not.toContain('class="rc main"')
    expect(out).not.toContain('에이전트 · 매도 검토')
    expect(out).toContain('<span class="kind rule">규칙 · 청산</span>')
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
