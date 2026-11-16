import type { ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { MarketSummary, PerformanceSeries, RatingBar, SeriesRow } from '../api/types'
import { NextRunRow } from '../pages/Home'
import { FallbackLines } from '../pages/Settings'
import { RatingBars, SeriesCharts, SettlementTable } from './perfCharts'
import { afterLeave, areaPath, fallbackLine, linePath, nearestIndex, niceTicks, PC, scale, signPct, stepIndex, tickLabel, tone } from './perfLogic'

const html = (el: ReactElement) => renderToStaticMarkup(el)
const day = (d: string | null | undefined) => d ?? '–'

// the server's rows, written out by hand (no values worked out here)
const row = (date: string, nav: number, dd: number, ex: number, base: number | null): SeriesRow =>
  ({ date, equity: 1_000_000 * (1 + nav), nav_pct: nav, drawdown_pct: dd, exposure_pct: ex, cash_pct: 1 - ex, baseline_pct: base })
const ROWS = [row('2026-06-01', 0, 0, 0, 0.01), row('2026-06-02', 0.1, 0, 0.5, 0.02), row('2026-06-03', -0.05, -0.136, 0.3, null)]
const S = (rows: SeriesRow[], o: Partial<PerformanceSeries> = {}): PerformanceSeries => ({
  market: 'KRX', currency: 'KRW', start: rows[0]?.date ?? null, as_of: '2026-06-03', from: null, to: null, initial_cash: 1_000_000,
  rows, reference: { variant: 'rules_on_regime', variant_ko: '하드 룰 + 국면 필터', measurement_ko: '2차 실측', covered_until: '2026-06-02', error: null },
  cache: 'miss', price_source: 'kis', price_as_of: '2026-06-03', price_provisional: false, note_ko: '', ...o,
})

describe('chart scale', () => {
  it('round ticks cover the range through 0', () => {
    expect(niceTicks(-0.05, 0.1)).toEqual([-0.05, 0, 0.05, 0.1])
    expect(niceTicks(-0.136, 0)).toEqual([-0.15, -0.1, -0.05, 0])
    expect(niceTicks(0, 1)).toEqual([0, 0.25, 0.5, 0.75, 1])
  })
  it('places the top tick at the top, the bottom at the bottom, days evenly', () => {
    const s = scale(ROWS, ROWS.map((r) => r.nav_pct))
    expect(s.y(0.1)).toBe(PC.top)
    expect(s.y(-0.05)).toBe(PC.bottom)
    expect(s.y(0)).toBe(+(PC.top + (0.1 / 0.15) * (PC.bottom - PC.top)).toFixed(1))
    expect([s.x(0), s.x(2)]).toEqual([PC.left, PC.width - PC.right])
    expect(s.x(1)).toBe((PC.left + PC.width - PC.right) / 2)
  })
  it('a flat series still gets a range; one day sits in the middle', () => {
    const one = scale([ROWS[0]], [0])
    expect(one.y(0)).toBeGreaterThan(PC.top)
    expect(one.y(0)).toBeLessThan(PC.bottom)
    expect(one.x(0)).toBe(PC.left + (PC.width - PC.left - PC.right) / 2)
  })
  it('a date label about weekly, the last day unless it crowds the one before', () => {
    const days = Array.from({ length: 9 }, (_, i) => ({ date: `2026-06-${String(i + 1).padStart(2, '0')}` }))
    expect(scale(days, [0]).dates.map((d) => d.i)).toEqual([0, 5, 8])
    expect(scale(days.slice(0, 7), [0]).dates.map((d) => d.i)).toEqual([0, 5])
    const long = Array.from({ length: 109 }, (_, i) => ({ date: `d-${i}` }))
    expect(scale(long, [0]).dates.map((d) => d.i)).toEqual([0, 15, 30, 45, 60, 75, 90, 105])
  })
  it('the reference line breaks where it has no value; the area closes on 0', () => {
    const s = { x: (i: number) => i * 10, y: (v: number) => -v * 100 }
    expect(linePath([0.01, 0.02, null], s)).toBe('M0,-1L10,-2')
    expect(linePath([null, 0.01, null, 0.02], s)).toBe('M10,-1M30,-2')
    expect(areaPath([0, -0.1], s)).toBe('M0,0L0,0L10,10L10,0Z')
    expect(areaPath([], s)).toBe('')
  })
  it('signs and labels: rising red, falling blue', () => {
    expect([signPct(0.0123, 2), signPct(-0.05), signPct(0), signPct(null)]).toEqual(['+1.23%', '−5.0%', '0.0%', '–'])
    expect([tickLabel(0.05, [0, 0.05]), tickLabel(0.0025, [0, 0.0025]), tickLabel(0.5, [0, 0.25], true)]).toEqual(['+5%', '+0.3%', '50%'])
    expect([tone(0.1), tone(-0.1), tone(0), tone(null)]).toEqual(['up', 'down', 'flat', 'flat'])
  })
})

describe('hover and keyboard', () => {
  it('the pointer picks the nearest day, clamped to the ends', () => {
    expect(nearestIndex(PC.left, 3)).toBe(0)
    expect(nearestIndex(PC.width - PC.right, 3)).toBe(2)
    expect(nearestIndex((PC.left + PC.width - PC.right) / 2 + 5, 3)).toBe(1)
    expect([nearestIndex(-50, 3), nearestIndex(5000, 3), nearestIndex(500, 1)]).toEqual([0, 2, 0])
  })
  it('the pointer leaving keeps a keyboard-picked day, clears a hovered one (review 1R #4)', () => {
    expect([afterLeave(5, true), afterLeave(5, false), afterLeave(null, true)]).toEqual([5, null, null])
  })
  it('← → step a day, Home End jump, other keys are left alone', () => {
    expect([stepIndex(null, 'ArrowLeft', 3), stepIndex(0, 'ArrowLeft', 3), stepIndex(1, 'ArrowRight', 3), stepIndex(2, 'ArrowRight', 3)])
      .toEqual([1, 0, 2, 2])
    expect([stepIndex(1, 'Home', 3), stepIndex(0, 'End', 3), stepIndex(1, 'Tab', 3), stepIndex(null, 'ArrowLeft', 0)]).toEqual([0, 2, null, null])
  })
  it('a chosen day draws its marker and the readout names it; with none the readout is the last day', () => {
    const chosen = html(<SeriesCharts s={S(ROWS)} market="KRX" day={day} initial={1} />)
    expect(chosen).toContain('06-02 모의 +10.00% · 백테스트 +2.00%')
    expect(chosen).toContain('06-02 노출 50.0% · 현금 50.0%')
    expect(chosen.match(/class="marker"/g)?.length).toBe(3)
    expect(chosen).toContain('<rect class="bar-ex on"')
    const none = html(<SeriesCharts s={S(ROWS)} market="KRX" day={day} />)
    expect(none).not.toContain('class="marker"')
    expect(none).toMatch(/<b class="num">2026-06-03<\/b> \(마지막 날\)/)
    expect(none).toContain('<span class="num down">−5.00%</span>')
    expect(none).toContain('백테스트 <span class="num flat">–</span>')
  })
  it('every chart is focusable and says its values without a pointer', () => {
    const out = html(<SeriesCharts s={S(ROWS)} market="KRX" day={day} />)
    expect(out.match(/tabindex="0"/g)?.length).toBe(3)
    expect(out).toContain('aria-label="수익률 2026-06-01 ~ 2026-06-03: 모의 −5.00%, 백테스트 –"')
    expect(out).toContain('aria-label="드로다운 2026-06-01 ~ 2026-06-03: 최저 −13.60%, 현재 −13.60%"')
    expect(out).toContain('aria-live="polite"')
  })
})

describe('empty states', () => {
  it('no book, or nothing in the range, says which', () => {
    expect(html(<SeriesCharts s={S([], { start: null })} market="KRX" day={day} />)).toContain('모의 장부 기록이 없습니다')
    const cut = html(<SeriesCharts s={S([], { start: '2026-06-01' })} market="KRX" day={day} />)
    expect(cut).toContain('고른 범위에 거래일이 없습니다')
    expect(cut).not.toContain('<svg')
  })
  it('the rating bars with nothing settled say so; a count of 0 shows a dash, not 0%', () => {
    const zero: RatingBar = { rating: 'Overweight', rating_ko: '비중확대', count: 0, mean_alpha: null, mean_raw: null, hit_rate: null }
    expect(html(<RatingBars bars={[zero]} />)).toContain('정산된 결정이 없습니다')
    const buy: RatingBar = { rating: 'Buy', rating_ko: '매수', count: 2, mean_alpha: 0.005, mean_raw: 0.02, hit_rate: 0.5 }
    const hold: RatingBar = { rating: 'Hold', rating_ko: '보유', count: 1, mean_alpha: -0.01, mean_raw: 0.005, hit_rate: null }
    const out = html(<RatingBars bars={[buy, zero, hold]} />)
    expect(out).toContain('<td class="r num up">+0.50%</td>')
    expect(out).toContain('<td class="r num down">−1.00%</td>')
    expect(out).toContain('방향 없음')
    expect(out).toContain('평균 알파 (±1% 스케일)')
    expect(out.match(/<td class="r num">–<\/td>/g)?.length).toBe(1)
  })
  it('the settlement table marks only a nonzero count', () => {
    const out = html(<SettlementTable st={{ failed: 1, waiting: 0, by_reason: [
      { id: 'no_price', label_ko: '가격 파일 없음', count: 1, tickers: ['X'] },
      { id: 'price_ended', label_ko: '시세 끊김', count: 0, tickers: [] }] }} />)
    expect(out).toContain('<td class="r num alert-t">1</td>')
    expect(out).toContain('<td class="r num ">0</td>')
  })
})

describe('price fallback warning', () => {
  const fb = { stage: 'fill', trade_date: '2026-06-04', ended_at: '2026-06-04T00:06:00Z',
    notes: ['가격 Yahoo 폴백(KIS 파일 없음): 005930.KS — 2026-06-04 실행, Yahoo 마지막 행 2026-06-04'] }
  it('one line from the last run, nothing without one', () => {
    expect(fallbackLine({ price_fallback: { KRX: null, US: null } }, 'KRX')).toBeNull()
    expect(fallbackLine({ price_fallback: { KRX: { ...fb, notes: [] }, US: null } }, 'KRX')).toBeNull()
    expect(fallbackLine({ price_fallback: { KRX: fb, US: null } }, 'KRX'))
      .toBe('마지막 루프 실행(2026-06-04 fill): 가격 Yahoo 폴백(KIS 파일 없음): 005930.KS — 2026-06-04 실행, Yahoo 마지막 행 2026-06-04')
    expect(fallbackLine({ price_fallback: { KRX: { ...fb, notes: [...fb.notes, 'b', 'c'] }, US: null } }, 'KRX')).toMatch(/ 외 2건$/)
  })
  it('the settings panel shows it per market, or renders nothing', () => {
    expect(html(<FallbackLines d={{ price_fallback: { KRX: null, US: null } }} />)).toBe('')
    const out = html(<FallbackLines d={{ price_fallback: { KRX: null, US: fb } }} />)
    expect(out.match(/class="fallback-warn"/g)?.length).toBe(1)
    expect(out).toContain('<b>미국</b> 마지막 루프 실행(2026-06-04 fill)')
  })
})

describe('home card next run (holiday calendar)', () => {
  const card = (o: Partial<MarketSummary>): MarketSummary => ({
    market: 'KRX', label_ko: '국내', currency: 'KRW', currency_symbol: '₩', benchmark: '^KS11', benchmark_name_ko: '코스피',
    timezone: 'Asia/Seoul', session: '09:00-15:30', session_kst: '10:00-16:30', name_style: 'name', as_of: null, equity: null,
    initial_cash: null, capital: null, day_return: null, drawdown: null, positions: null, state: null, flow: null, error: null,
    next_run_kst: '2026-11-19T16:35:00+09:00', next_run_stage: 'decide', next_run_label_ko: '11-19 16:35 KST · 결정', schedule_error: null, ...o,
  } as MarketSummary)
  it('a broken calendar shows its error, never a guessed session', () => {
    const bad = html(<table><tbody><NextRunRow m={card({ next_run_kst: null, next_run_stage: null, next_run_label_ko: '일정 계산 오류',
      session_kst: null, schedule_error: '국내 휴장 달력을 읽지 못함: JSONDecodeError' })} /></tbody></table>)
    expect(bad).toContain('<td class="r alert-t">일정 계산 오류</td>')
    expect(bad).toContain('국내 휴장 달력을 읽지 못함: JSONDecodeError')
    expect(bad).not.toContain('정규장')
    expect(html(<table><tbody><NextRunRow m={card({})} /></tbody></table>)).toContain('정규장 10:00-16:30 KST')
  })
})
