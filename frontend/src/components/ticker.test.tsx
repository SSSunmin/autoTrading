import type { ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import type { ThesisCheck, TickerDetail, TickerHolding } from '../api/types'
import { HoldingPanel, ThesisPanel, TickerFacts } from './ticker'
import { earningsLine, latestReview, metLine, TICKER_TABS, verdictBadge } from './tickerLogic'

const html = (el: ReactElement) => renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>)

// the server's rows, written out by hand
const check = (over: Partial<ThesisCheck>): ThesisCheck => ({
  trade_date: '2026-06-04', recorded_at: '2026-06-04T07:00:00Z', rating: 'Hold', rating_ko: '보유', decider: 'm', kind: 'review',
  verdict: 'keep', verdict_ko: '논지 유지', met: 0, total: 2, action: 'Hold', trim_to_pct: null, reasons: [], reasons_ko: [],
  reasoning: '유지 판단', checks: [{ condition: '영업이익률 12% 미만', met: false, evidence: '14%' }, { condition: 'HBM 40% 아래', met: false, evidence: '52%' }],
  ...over,
})
const MEMO = check({ trade_date: '2026-06-01', kind: 'memo', verdict: 'memo', verdict_ko: '투자 메모', met: null, total: 1, rating: 'Overweight',
  action: undefined, reasoning: undefined, thesis: 'HBM 공급 부족', price: 10000, target: 14000, downside: 8500, horizon: '6-12 months',
  exit_plan: '목표가 절반 매도', checks: [{ condition: '영업이익률 12% 미만', met: null, evidence: null }] })
const TRIM = check({ trade_date: '2026-06-05', verdict: 'trim', verdict_ko: '축소', met: 1, action: 'Trim', trim_to_pct: 2,
  rating: 'Underweight', reasons: ['thesis_broken'], reasons_ko: ['논지 깨짐'],
  checks: [{ condition: '영업이익률 12% 미만', met: true, evidence: '11%' }, { condition: 'HBM 40% 아래', met: false, evidence: '52%' }] })
const EXIT = check({ trade_date: '2026-06-08', verdict: 'exit', verdict_ko: '청산', met: 2, action: 'Exit', rating: 'Sell' })

const HOLD: TickerHolding = {
  ticker: '005930.KS', quantity: 500, avg_price: 10010, entry_date: '2026-06-02', partial_sold: 499, last_price: 11000,
  last_date: '2026-06-05', market_value: 5_500_000, unrealized: 495_000, unrealized_pct: 0.0989, weight: 0.055, stop_price: 9309,
  stop_gap: 0.18, near_stop: false, trailing_active: false, trailing_high: 11000, trailing_stop_price: null, holding_days: 3,
  expiry_date: '2026-09-01', exit_signal: null, as_of: '2026-06-05',
  partial_sales: [{ date: '2026-06-05', quantity: 499, price: 10990, order_id: 'o2' }],
}

const detail = (over: Partial<TickerDetail>): TickerDetail => ({
  ticker: 'AAPL', name: 'Apple', screening: [], decisions: [], outcomes: [], orders: [], timeline: [], market: 'US',
  sector: 'Technology', exchange: '나스닥', holding: null, thesis_checks: [], earnings: null, earnings_note_ko: '', ...over,
})

describe('ticker tabs and verdicts', () => {
  it('has the six tabs in the mockup order', () => {
    expect(TICKER_TABS.map((t) => t.label)).toEqual(['결정 이력', '선별 이력', '결과·교훈', '검증 스냅샷', '논지 점검', '보유'])
  })

  it('maps each verdict to its chip: 유지 neutral, 축소 and 청산 the sell side, a memo pending', () => {
    expect(verdictBadge(check({}))).toEqual({ rating: 'Hold', label: '논지 유지' })
    expect(verdictBadge(TRIM)).toEqual({ rating: 'Underweight', label: '축소' })
    expect(verdictBadge(EXIT)).toEqual({ rating: 'Sell', label: '청산' })
    expect(verdictBadge(MEMO)).toEqual({ rating: 'pending', label: '투자 메모' })
  })

  it('reads the latest review for the summary, never a memo', () => {
    expect(latestReview([MEMO, TRIM, check({ trade_date: '2026-06-06' })])?.trade_date).toBe('2026-06-06')
    expect(latestReview([MEMO])).toBeNull()
    expect(latestReview([])).toBeNull()
    expect(metLine(TRIM)).toBe('2개 조건 중 1개 충족')
    expect(metLine(MEMO)).toBe('깨짐 조건 1개')
  })

  it('shows the earnings date with its source and fetch day, or the note alone', () => {
    expect(earningsLine(detail({ earnings: { date: '2026-07-28', date_to: '2026-07-30', source: 'yfinance calendar', fetched: '2026-06-03', error: null, failed: null },
      earnings_note_ko: '참고 · 결정 · 주문에 쓰지 않음' }))).toEqual({ date: '2026-07-28 ~ 2026-07-30', sub: 'yfinance calendar · 2026-06-03 조회 · 참고 · 결정 · 주문에 쓰지 않음', failed: false })
    expect(earningsLine(detail({ market: 'KRX', earnings_note_ko: '국내 실적 발표일 미제공' }))).toEqual({ date: null, sub: '국내 실적 발표일 미제공', failed: false })
    expect(earningsLine(detail({ earnings: { date: null, date_to: null, source: 'yfinance calendar', fetched: '2026-06-03', error: null, failed: null },
      earnings_note_ko: '출처에 예정일 없음' }))).toEqual({ date: null, sub: 'yfinance calendar · 2026-06-03 조회 · 출처에 예정일 없음', failed: false })
  })
})

describe('논지 점검 tab', () => {
  it('heads with the latest verdict and lists every record oldest first', () => {
    const out = html(<ThesisPanel checks={[MEMO, TRIM]} market="KRX" />)
    expect(out).toContain('<span class="rating Underweight">축소</span><span class="muted">2026-06-05 검토 · 2개 조건 중 1개 충족</span>')
    expect(out.indexOf('2026-06-01')).toBeLessThan(out.indexOf('2026-06-05</span><span class="review-badges'))
    expect(out).toContain('목표 비중 2%')
    expect(out).toContain('<span class="rc main">논지 훼손</span>')          // v0.8 chips: every reason here, the mockup's words
    expect(out).toContain('<li class="met"><span class="tag">충족</span> 영업이익률 12% 미만')
    expect(out).toContain('HBM 공급 부족')
    expect(out).toContain('₩14,000')
    expect(out).not.toContain('아직 매도 검토 기록이 없습니다')
  })

  it('says there is no sell review yet when only a memo or nothing is recorded', () => {
    const memoOnly = html(<ThesisPanel checks={[MEMO]} market="KRX" />)
    expect(memoOnly).toContain('이 종목은 아직 매도 검토 기록이 없습니다')
    expect(memoOnly).toContain('투자 메모만 있습니다')
    expect(memoOnly).toContain('HBM 공급 부족')
    const none = html(<ThesisPanel checks={[]} market="KRX" />)
    expect(none).toContain('이 종목은 아직 매도 검토 기록이 없습니다')
    expect(none).toContain('투자 메모도 없습니다')
    expect(none).not.toContain('thesis-list')
  })
})

describe('보유 tab and header', () => {
  it('shows the holding with its partial sells, the gain in red', () => {
    const out = html(<HoldingPanel h={HOLD} market="KRX" />)
    expect(out).toContain('500주<span class="muted"> · 부분 매도 499주</span>')
    expect(out).toContain('₩10,010 / ₩11,000')
    expect(out).toContain('<span class="num up">₩495,000</span>')
    expect(out).toContain('<td class="num">2026-06-05</td><td class="r num">499주</td><td class="r num">₩10,990</td>')
    expect(out).not.toContain('부분 매도 없음')
  })

  it('has an empty state when not held, and one for no partial sells', () => {
    expect(html(<HoldingPanel h={null} market="KRX" />)).toContain('보유하지 않은 종목입니다')
    const whole = html(<HoldingPanel h={{ ...HOLD, partial_sold: 0, partial_sales: [] }} market="KRX" />)
    expect(whole).toContain('부분 매도 없음')
    expect(whole).not.toContain('· 부분 매도 0주')
  })

  it('header carries sector, exchange, holding and the earnings badge, with fallbacks', () => {
    const us = html(<TickerFacts d={detail({ holding: HOLD, earnings: { date: '2026-07-30', date_to: null, source: 'yfinance calendar', fetched: '2026-06-03', error: null, failed: null } })} />)
    expect(us).toContain('업종 <b>Technology</b>')
    expect(us).toContain('거래소 <b>나스닥</b>')
    expect(us).toContain('보유 <b>500주 · 2026-06-02 진입</b>')
    expect(us).toContain('<span class="tag">참고</span> <b>2026-07-30</b>')
    const bare = html(<TickerFacts d={detail({ sector: null, exchange: null, market: 'KRX', earnings_note_ko: '국내 실적 발표일 미제공' })} />)
    expect(bare).toContain('업종 <b>미분류</b>')
    expect(bare).toContain('거래소 <b>미확인</b>')
    expect(bare).toContain('보유 <b>없음</b>')
    expect(bare).toContain('<b>–</b> <span class="muted">국내 실적 발표일 미제공</span>')
  })
})
