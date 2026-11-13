import type { ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import type { PaperProgress } from '../api/types'
import { DrawdownSvg, ExpectedTable, GoLiveSummary, Scorecard, VerdictBanner, ViolationList } from './paper'

const html = (el: ReactElement) => renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>)
const day = (d: string | null | undefined) => d ?? '–'
const usDay = (d: string | null | undefined) => (d ? `${d} ET` : '–')
const to = (p: string) => `${p}${p.includes('?') ? '&' : '?'}market=US`

// the server's response, written out by hand (no values worked out here)
const P: PaperProgress = {
  market: 'KRX', market_ko: '국내', currency: 'KRW', timezone: 'Asia/Seoul', benchmark_name_ko: '코스피',
  started: true, start: '2026-06-08', as_of: '2026-07-24', days: 46, weeks: 6, min_weeks: 8, judge_date: '2026-08-03',
  initial_cash: 100_000_000, eligible: false, counts: { met: 4, unmet: 1, in_progress: 1 },
  summary_ko: '충족 4 · 미충족 1 (REVIEW 결정 비율 상한) · 진행 중 1 (최소 모의 운용 기간(주))', verdict_ko: '전환 불가', weeks_badge_ko: '6/8주',
  criteria: [
    { id: 'min_weeks', label_ko: '최소 모의 운용 기간(주)', threshold: 8, threshold_ko: '≥ 8주', value: 6, value_ko: '6주 (2026-06-08 시작)', status: 'in_progress', status_ko: '진행 중', note_ko: '10일 남음 · 2026-08-03 장 마감 후 판정' },
    { id: 'max_drawdown', label_ko: '최대 드로다운 한도', threshold: -0.1, threshold_ko: '> −10%', value: -0.048, value_ko: '-4.8% (2026-07-09)', status: 'met', status_ko: '충족', note_ko: '현재 -3.4%' },
    { id: 'max_daily_loss_breaches', label_ko: '일일 손실 한도 위반 허용 횟수', threshold: 2, threshold_ko: '≤ 2회', value: 1, value_ko: '1회 (2026-07-09 -2.31%)', status: 'met', status_ko: '충족', note_ko: '1회 더 남음' },
    { id: 'max_stop_outs', label_ko: '−20% 정지 발동 허용 횟수', threshold: 0, threshold_ko: '0회', value: 0, value_ko: '0회', status: 'met', status_ko: '충족', note_ko: '' },
    { id: 'max_review_rate', label_ko: 'REVIEW 결정 비율 상한', threshold: 0.05, threshold_ko: '≤ 5%', value: 0.071, value_ko: '7.1% (2 / 28)', status: 'unmet', status_ko: '미충족', note_ko: 'REVIEW 2건 / 에이전트 결정 28건' },
    { id: 'max_unexplained_orders', label_ko: '결정 기록 없는 주문 허용 건수', threshold: 0, threshold_ko: '0건', value: 0, value_ko: '0건', status: 'met', status_ko: '충족', note_ko: '주문 41건 중 41건이 결정 기록과 연결' },
  ],
  violations: [
    { date: '2026-07-24', kind: 'review', kind_ko: 'REVIEW', ticker: '105560.KS', name: 'KB금융', value: null, text: '등급 읽기 실패 · 주문 안 함', ref: { type: 'decision', date: '2026-07-24', ticker: '105560.KS' } },
    { date: '2026-07-09', kind: 'daily_loss', kind_ko: '일일 손실 한도', ticker: null, name: null, value: -0.0231, text: '-2.31% ≤ -2% → 다음 세션 신규 매수 중단', ref: { type: 'positions', date: '2026-07-09' } },
  ],
  notes: [],
  series: [{ date: '2026-06-08', equity: 1e8, drawdown: 0 }, { date: '2026-06-09', equity: 9.5e7, drawdown: -0.05 }],
  drawdown: { current: -0.05, min: -0.05, min_date: '2026-06-09' },
  limits: { max_drawdown: -0.1, dd_halve: -0.1, dd_stop: -0.2 },
  go_live: { capital: 100_000_000, capital_source_ko: '모의 자본(실거래 자본 입력은 단계 5)', auto_order_max: 30_000_000, auto_daily_order_cap: 100_000_000,
    daily_loss_limit: -0.02, daily_loss_amount: -2_000_000, other_markets_ko: ['미국'], note_ko: '실주문 경로는 아직 없습니다' },
  expected: {
    reference_only: true, measurement_ko: '2차 실측', variant_ko: '하드 룰 + 국면 필터', start: '2026-06-08', end: '2026-07-24', covered_until: null, error: null,
    rows: [
      { id: 'mean_exposure', label_ko: '평균 노출', unit: 'pct', reference: 0.219, paper: 0.235, diff: 0.016 },
      { id: 'new_buys', label_ko: '신규 매수', unit: 'count', reference: 11, paper: 8, paper_open: 1, diff: -3 },
      { id: 'exits', label_ko: '청산', unit: 'count', reference: 11, paper: 2, diff: -9, reference_by_reason: { stop_loss: 7, trailing: 4 }, paper_by_reason: { stop_loss: 2 } },
      { id: 'daily_loss_guard', label_ko: '일일 손실 가드', unit: 'count', reference: 2, paper: 1, diff: -1, reference_dates: ['2026-06-08', '2026-07-09'], paper_dates: ['2026-07-09'] },
      { id: 'index_return', label_ko: '같은 기간 코스피', unit: 'pct', reference: -0.009, paper: -0.009, diff: null },
    ],
  },
}
const EXIT_KO = { stop_loss: '손절', trailing: '트레일링' }

describe('VerdictBanner', () => {
  it('shows the server verdict and summary in the tone of what is missing', () => {
    const out = html(<VerdictBanner p={P} day={day} />)
    expect(out).toContain('class="verdict no"')
    expect(out).toContain('<span class="verdict-t">전환 불가</span>')
    expect(out).toContain('충족 4 · 미충족 1 (REVIEW 결정 비율 상한) · 진행 중 1 (최소 모의 운용 기간(주))')
    expect(out).toContain('모의 시작 2026-06-08 · 모의 자본 ₩100,000,000 · 마지막 갱신 2026-07-24 장 마감')
    expect(html(<VerdictBanner p={{ ...P, eligible: true, verdict_ko: '전환 가능', counts: { met: 6, unmet: 0, in_progress: 0 } }} day={day} />)).toContain('class="verdict ok"')
    expect(html(<VerdictBanner p={{ ...P, started: false }} day={day} />)).toContain('모의 운용 기록 없음')
  })
})

describe('Scorecard', () => {
  it('writes each criterion as served, its pill in colour and words, the unmet row marked', () => {
    const out = html(<Scorecard p={P} />)
    expect(out).toContain('<span class="pill wait">진행 중</span>')
    expect(out).toContain('<span class="pill unmet">미충족</span>')
    expect((out.match(/<span class="pill met">충족<\/span>/g) ?? []).length).toBe(4)
    expect(out).toContain('<tr class="row-unmet"><td><b>REVIEW 결정 비율 상한</b>')
    expect(out).toContain('<b>7.1% (2 / 28)</b>')
    expect(out).toContain('10일 남음 · 2026-08-03 장 마감 후 판정')
  })
})

describe('DrawdownSvg', () => {
  it('is an image with its low, current and pass line in words, and the lines in their classes', () => {
    const out = html(<DrawdownSvg p={P} day={usDay} />)
    expect(out).toContain('role="img" aria-label="모의 기간 드로다운: 최저 2026-06-09 ET -5.0%, 현재 -5.0%, 기준 -10%"')
    expect(out).toContain('<g class="rule limit"><line x1="0" y1="85.3" x2="1000" y2="85.3"></line>')
    expect(out).toContain('<g class="rule stop"><line x1="0" y1="162.5" x2="1000" y2="162.5"></line>')
    expect(out).toContain('<polyline class="dd-line" points="0,8 1000,46.6"></polyline>')
    expect(out).toContain('06-09 최저 -5.0%')
  })
})

describe('ViolationList', () => {
  it('lists each violation with its kind badge and the screen it links to, and the two counts underneath', () => {
    const out = html(<ViolationList p={P} to={to} day={usDay} />)
    expect(out).toContain('<span class="xr review">REVIEW</span>')
    expect(out).toContain('KB금융 <span class="tag mono">105560.KS</span>')
    expect(out).toContain('href="/decisions?tab=exceptions&amp;market=US"')
    expect(out).toContain('href="/positions?market=US"')
    expect(out).toContain('2026-07-09 ET')
    expect(out).toContain('−20% 정지 발동 <b class="num">0</b> · 결정 없는 주문 <b class="num">0</b>')
    expect(out).not.toContain('참고 · 위반은 아니지만')
  })
  it('says there is none, and shows what a person looked at apart', () => {
    const notes = [{ date: '2026-07-24', kind: 'stop_loss' as const, kind_ko: '손절', ticker: 'GOOGL', name: '알파벳 A', text: '2026-07-20 진입 → 2026-07-24 시가 청산 -9.3%', ref: { type: 'positions' as const, date: '2026-07-24' } }]
    const out = html(<ViolationList p={{ ...P, violations: [], notes }} to={to} day={usDay} />)
    expect(out).toContain('<div class="v-empty">위반 0건</div>')
    expect(out).toContain('참고 · 위반은 아니지만 사람이 본 것')
    expect(out).toContain('<span class="xr stop_loss">손절</span>')
  })
})

describe('ExpectedTable', () => {
  it('is labelled a reference that never decides, with both sides as served and the difference in points', () => {
    const out = html(<ExpectedTable p={P} exitKo={EXIT_KO} day={day} />)
    expect(out).toContain('참고용 비교 · 통과 판정에 쓰지 않음')
    expect(out).toContain('같은 기간(2026-06-08 → 2026-07-24) 2차 실측 하드 룰 + 국면 필터 재현값과 모의 결과')
    expect(out).toContain('+1.6%p')
    expect(out).toContain('8건 (+ 미체결 1)')
    expect(out).toContain('11건<span> <span class="xr stop_loss">손절</span> 7</span><span> <span class="xr trailing">트레일링</span> 4</span>')
    expect(out).toContain('2회 (06-08, 07-09)')
  })
  it('says why a table is missing', () => {
    expect(html(<ExpectedTable p={{ ...P, expected: { ...P.expected!, rows: [], error: '2차 실측 결과를 읽지 못함' } }} exitKo={EXIT_KO} day={day} />))
      .toContain('2차 실측 결과를 읽지 못함')
    expect(html(<ExpectedTable p={{ ...P, expected: null }} exitKo={EXIT_KO} day={day} />)).toContain('비교할 기간이 없습니다')
  })
})

describe('GoLiveSummary', () => {
  it('names the criteria, the rules at the capital in its currency, and the other market left alone', () => {
    const out = html(<GoLiveSummary p={P} />)
    expect(out).toContain('class="go-sum no"')
    expect(out).toContain('통과 기준 4/6 충족')
    expect(out).toContain('자본 ₩100,000,000에서 걸리는 규칙')
    expect(out).toContain('주문당 상한 ₩30,000,000 · 일일 주문 상한(매수) ₩100,000,000')
    expect(out).toContain('일일 손실 -₩2,000,000(-2%)에서 자동 정지')
    expect(out).toContain('<b>미국 모의투자는 영향 없음</b> · 미국 시장은 따로 8주 기준을 통과해야 전환됩니다')
    expect(out).toContain('이미 체결된 실주문은 되돌릴 수 없습니다')
  })
})
