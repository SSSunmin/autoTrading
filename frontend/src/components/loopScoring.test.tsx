import type { ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { LoopColumn, LoopScoring } from '../api/types'
import { LoopPanel, PeriodNote, TrialBadge } from './loopScoring'
import { loopRows, pendingText, periodFrom, ratingsText, recordedText, seriesParams, shownSeries, trialBadge } from './loopScoringLogic'

/** the markup without the terms' ⓘ (glossary-tips; tested in glossary.test.tsx) */
const noTips = (s: string) => s.replace(/<span class="_root[^"]*"><button type="button" class="tip"[\s\S]*?<\/button><\/span>/g, '').replace(/<span class="tip-term">([^<]*)<\/span>/g, '$1')
const html = (el: ReactElement) => noTips(renderToStaticMarkup(el))

// the server's columns, written out by hand (no values worked out here)
const col = (o: Partial<LoopColumn>): LoopColumn => ({
  source: 'agent', label_ko: 'LLM 에이전트', decisions: 4, ratings: { Buy: 1, Hold: 1, REVIEW: 1, Underweight: 1 }, review: 1,
  stand_ins: 0, first: '2026-08-03', last: '2026-08-05', settled: 3, pending: 1,
  pending_by_reason: [{ id: 'window', label_ko: '5거래일이 아직 안 지남', count: 0 }, { id: 'no_price', label_ko: 'KIS 가격 파일 없음', count: 1 }],
  settle_failed: [], buy_settled: 1, mean_raw: 0.1, mean_alpha: 0.08, alpha_by_rating: {},
  directional: { n: 2, mean_return: -0.03, mean_alpha: 0.01, hit_rate: 0.5 }, hold: { n: 1, mean_alpha: -0.02 }, ...o,
})
const L = (o: Partial<LoopScoring> = {}): LoopScoring => ({
  market: 'KRX', source: 'all', from: null, to: null, holding_days: 5,
  columns: { agent: col({}), rule: col({ source: 'rule', label_ko: '규칙 (같은 날 재계산)', decisions: 4, ratings: { Buy: 2, Sell: 2 }, review: 0,
    mean_raw: -0.012, mean_alpha: null, pending: 0, pending_by_reason: [{ id: 'window', label_ko: '5거래일이 아직 안 지남', count: 0 },
      { id: 'no_price', label_ko: 'KIS 가격 파일 없음', count: 0 }] }) },
  agreement: { compared: 4, agree: 2, rate: 0.5, no_screen: 0 }, rule_recorded: { decisions: 0, first: null, last: null, stand_ins: 0 }, trial: null, human_excluded: 1, rule_exits_excluded: 2, note_ko: '정의', ...o,
})

describe('the two-column comparison', () => {
  it('puts the model left and the rule right, rising red and falling blue', () => {
    const out = html(<LoopPanel l={L()} />)
    expect(out).toMatch(/<th class="r">LLM 에이전트<\/th><th class="r">규칙 \(같은 날 재계산\)<\/th>/)
    expect(out).toContain('<td>기간 (결정일)</td><td class="r">2026-08-03 ~ 2026-08-05</td><td class="r">2026-08-03 ~ 2026-08-05</td>')
    expect(out).toContain('<td>매수 쪽 평균 수익</td><td class="r num up">+10.00%</td><td class="r num down">−1.20%</td>')
    expect(out).toContain('<td>매수 쪽 평균 알파</td><td class="r num up">+8.00%</td><td class="r num flat">–</td>')
    expect(out).toContain('<td>방향 적중률</td><td class="r num ">50%</td>')
    expect(out).toContain('같은 결정일 · 같은 종목에서 규칙 판단과 일치 <b class="num">2/4</b> (50%)')
    expect(out).toContain('사람 처리 1건 · 규칙 청산 2건은 채점 제외')
    expect(out).not.toContain('이 비교에 넣지 않았습니다')                      // nothing recorded apart
  })
  it('one source asked: one column, the agreement still said (the rule column is built on it)', () => {
    const rule = L({ source: 'rule', columns: { rule: L().columns.rule } })
    const out = html(<LoopPanel l={rule} />)
    expect(out).not.toContain('LLM 에이전트')
    expect(out).toContain('규칙 판단과 일치')
    expect(loopRows(rule.columns)[1].cells).toEqual([null, { kind: 'count', value: 4 }])
  })
  it('a single day is one date; no decision has no period', () => {
    expect(loopRows({ agent: col({ first: '2026-08-03', last: '2026-08-03' }) })[0].cells[0]).toEqual({ kind: 'text', value: '2026-08-03' })
    expect(loopRows({ agent: col({ first: null, last: null }) })[0].cells[0]).toEqual({ kind: 'text', value: '–' })
  })
  it("the rule's own records, its stand-ins and unscreened model calls are said to be left out", () => {
    expect(recordedText({ decisions: 0, first: null, last: null, stand_ins: 0 }, 0)).toBeNull()
    expect(recordedText({ decisions: 5, first: '2026-11-09', last: '2026-11-13', stand_ins: 1 }, 2)).toBe(
      '규칙이 직접 기록한 결정 5건(2026-11-09 ~ 2026-11-13) · 모델 대신 규칙이 정한 결정 1건 · 선별 기록이 없어 규칙과 비교하지 못한 모델 결정 2건은 이 비교에 넣지 않았습니다.')
    expect(recordedText({ decisions: 1, first: '2026-11-09', last: '2026-11-09', stand_ins: 0 }, 0)).toBe('규칙이 직접 기록한 결정 1건(2026-11-09)은 이 비교에 넣지 않았습니다.')
    const out = html(<LoopPanel l={L({ rule_recorded: { decisions: 0, first: null, last: null, stand_ins: 3 } })} />)
    expect(out).toContain('<p class="muted">모델 대신 규칙이 정한 결정 3건은 이 비교에 넣지 않았습니다.</p>')
  })
  it('no loop decision yet says so', () => {
    const none = L({ columns: { agent: col({ decisions: 0 }), rule: col({ decisions: 0 }) } })
    expect(html(<LoopPanel l={none} />)).toContain('일일 루프 결정이 없습니다')
  })
  it('ratings and pending reasons in words', () => {
    expect(ratingsText({ REVIEW: 1, Buy: 2, Sell: 1 })).toBe('매수 2 · 매도 1 · REVIEW 1')
    expect(ratingsText({})).toBe('–')
    expect(pendingText(col({}))).toBe('1 (KIS 가격 파일 없음 1)')
    expect(pendingText(col({ pending: 0, pending_by_reason: [] }))).toBe('0')
  })
})

describe('trial badge', () => {
  it('counts the model days of the trial, says when it ended, shows nothing when off', () => {
    expect(trialBadge(null)).toBeNull()
    expect(trialBadge({ sessions: 5, days: ['a', 'b', 'c'], remaining: 2 })).toEqual({ text: 'LLM 시험 3/5일', done: false })
    expect(trialBadge({ sessions: 5, days: ['a', 'b', 'c', 'd', 'e'], remaining: 0 })).toEqual({ text: 'LLM 시험 종료 5/5일 → 규칙', done: true })
    expect(trialBadge({ sessions: null, days: ['a'], remaining: null })).toEqual({ text: 'LLM 시험 1일 (기한 없음)', done: false })
    expect(html(<TrialBadge trial={null} />)).toBe('')
    expect(html(<TrialBadge trial={{ sessions: 5, days: ['a'], remaining: 4 }} />)).toBe('<span class="trial-badge">LLM 시험 1/5일</span>')
    expect(html(<TrialBadge trial={{ sessions: 5, days: ['a', 'b', 'c', 'd', 'e'], remaining: 0 }} />)).toContain('class="trial-badge done"')
  })
})

describe('6-A period', () => {
  it("the page shows the period's own answer once one is picked, the whole book otherwise", () => {
    expect(shownSeries(undefined, 'whole', 'cut')).toBe('whole')
    expect(shownSeries('2026-11-05', 'whole', 'cut')).toBe('cut')
  })
  it('the request carries the period’s from, nothing for the whole book or before the book has a day', () => {
    expect(seriesParams('2026-11-12', 'week')).toEqual({ from: '2026-11-05' })
    expect(seriesParams('2026-11-12', 'month')).toEqual({ from: '2026-10-12' })
    expect(seriesParams('2026-11-12', 'all')).toBeUndefined()
    expect(seriesParams(null, 'month')).toBeUndefined()
  })
  it("the note under the header only for a period, in the market's day", () => {
    expect(html(<PeriodNote from={undefined} day={(d) => d} />)).toBe('')
    expect(html(<PeriodNote from="2026-11-05" day={(d) => `${d} ET`} />)).toBe(
      '<p class="muted period-note">2026-11-05 ET 이후만 표시 — 수익률 · 드로다운은 모의 시작일 기준 그대로입니다</p>')
  })
  it('a week back, a month back (clamped to the month end), or the whole book', () => {
    expect(periodFrom('2026-11-12', 'week')).toBe('2026-11-05')
    expect(periodFrom('2026-11-12', 'month')).toBe('2026-10-12')
    expect(periodFrom('2026-03-31', 'month')).toBe('2026-02-28')
    expect(periodFrom('2026-01-03', 'week')).toBe('2025-12-27')
    expect(periodFrom('2026-11-12', 'all')).toBeUndefined()
    expect(periodFrom(null, 'week')).toBeUndefined()
  })
})
