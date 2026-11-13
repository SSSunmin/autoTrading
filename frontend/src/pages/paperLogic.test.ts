import { describe, expect, it } from 'vitest'

import { areaPolygon, diffText, drawdownChart, modalReady, PILL_CLASS, polyline, switchState, verdictTone } from './paperLogic'

const LIMITS = { max_drawdown: -0.10, dd_halve: -0.10, dd_stop: -0.20 }

describe('the verdict pill and banner', () => {
  it('maps each server status to its pill, colour and text together', () => {
    expect(PILL_CLASS).toEqual({ met: 'pill met', unmet: 'pill unmet', in_progress: 'pill wait' })
  })
  it('is switchable only when eligible; an unmet criterion outranks waiting for the period', () => {
    expect(verdictTone({ eligible: true, counts: { met: 6, unmet: 0, in_progress: 0 } })).toBe('ok')
    expect(verdictTone({ eligible: false, counts: { met: 4, unmet: 1, in_progress: 1 } })).toBe('no')
    expect(verdictTone({ eligible: false, counts: { met: 5, unmet: 0, in_progress: 1 } })).toBe('wait')
  })
})

describe('switchState', () => {
  const paper = { account_mode: 'paper' as const }
  it('turns the live switch on only for an eligible market still on paper', () => {
    expect(switchState({ started: true, eligible: true }, paper)).toEqual({ enabled: true, why: '' })
    expect(switchState({ started: true, eligible: false }, paper)).toEqual({ enabled: false, why: '통과 기준 6개를 전부 충족해야 켜집니다' })
    expect(switchState({ started: false, eligible: false }, paper)).toEqual({ enabled: false, why: '모의 운용 기록이 없습니다' })
    expect(switchState({ started: true, eligible: true }, { account_mode: 'live' })).toEqual({ enabled: false, why: '이미 실거래 모드입니다' })
    expect(switchState(null, paper).enabled).toBe(false)
    expect(switchState({ started: true, eligible: true }, null).enabled).toBe(false)
  })
})

describe('modalReady', () => {
  const ok = { ack: true, reason: '모의 8주 통과', operator: '관리자', token: '', tokenRequired: false }
  it('needs the acknowledgement, a reason and an operator', () => {
    expect(modalReady(ok)).toBe(true)
    expect(modalReady({ ...ok, ack: false })).toBe(false)
    expect(modalReady({ ...ok, reason: '   ' })).toBe(false)
    expect(modalReady({ ...ok, operator: '' })).toBe(false)
  })
  it('needs the admin token only when the server asks for one', () => {
    expect(modalReady({ ...ok, tokenRequired: true })).toBe(false)
    expect(modalReady({ ...ok, tokenRequired: true, token: 's3cret' })).toBe(true)
  })
})

describe('drawdownChart', () => {
  const series = [{ date: '2026-06-01', drawdown: 0 }, { date: '2026-06-02', drawdown: -0.05 }, { date: '2026-06-03', drawdown: -0.04 }]
  it('puts 0% at the top and the −20% stop at the bottom of the 1000×200 box, as the mockup does', () => {
    const c = drawdownChart(series, LIMITS)
    // y = 8 + dd / −0.20 × 154.5: −5% → 46.6, −10% → 85.3, −15% → 123.9, −20% → 162.5 (the mockup's own lines)
    expect(c.rules.map((r) => [r.value, r.y, r.kind, r.label])).toEqual([
      [0, 8, 'grid', '0%'], [-0.05, 46.6, 'grid', '−5%'], [-0.1, 85.3, 'limit', '−10% 기준 · 반감'],
      [-0.15, 123.9, 'grid', '−15%'], [-0.2, 162.5, 'stop', '−20% 정지'],
    ])
    expect(c.points).toEqual([[0, 8], [500, 46.6], [1000, 38.9]])
    expect(c.low).toEqual({ x: 500, y: 46.6, date: '2026-06-02', drawdown: -0.05 })
    expect(c.ticks).toEqual([{ x: 0, label: '06-01' }])
    expect(polyline(c.points)).toBe('0,8 500,46.6 1000,38.9')
    expect(areaPolygon(c.points)).toBe('0,8 0,8 500,46.6 1000,38.9 1000,8')
  })
  it('stretches to a low past the stop line and keeps the stop line inside', () => {
    const c = drawdownChart([{ date: '2026-06-01', drawdown: 0 }, { date: '2026-06-02', drawdown: -0.25 }], LIMITS)
    expect(c.points[1]).toEqual([1000, 162.5])
    expect(c.rules.find((r) => r.kind === 'stop')?.y).toBe(131.6)                         // 8 + 0.2/0.25 × 154.5
    expect(c.rules.at(-1)).toEqual({ value: -0.25, y: 162.5, kind: 'grid', label: '−25%' })
  })
  it('names the pass line and the halving line apart when they differ, and marks no low on a flat book', () => {
    const c = drawdownChart([{ date: '2026-06-01', drawdown: 0 }], { max_drawdown: -0.05, dd_halve: -0.10, dd_stop: -0.20 })
    expect(c.rules.filter((r) => r.kind === 'limit').map((r) => r.label)).toEqual(['−5% 기준', '−10% 반감'])
    expect(c.points).toEqual([[0, 8]])
    expect(c.low).toBeNull()
    expect(areaPolygon([])).toBe('')
  })
})

describe('diffText', () => {
  it('writes rate differences in percentage points and counts signed', () => {
    expect(diffText({ unit: 'pct', diff: 0.016 })).toBe('+1.6%p')
    expect(diffText({ unit: 'pct', diff: -0.269 })).toBe('−26.9%p')
    expect(diffText({ unit: 'count', diff: 2 })).toBe('+2')
    expect(diffText({ unit: 'count', diff: -5 })).toBe('−5')
    expect(diffText({ unit: 'count', diff: 0 })).toBe('같음')
    expect(diffText({ unit: 'pct', diff: null })).toBe('–')
  })
})
