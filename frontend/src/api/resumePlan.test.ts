import { describe, expect, it } from 'vitest'

import { resumePlan } from './resumePlan'
import type { TradingState } from './types'

const halted = (over: Partial<TradingState>): TradingState => ({
  mode: 'paper', mode_ko: '모의투자', running: false, halted_at: '2026-11-13T05:00:00Z', halted_by: 'system',
  halted_by_ko: '시스템 자동 정지', halt_reason: '드로다운 정지', keep_exits: true, mode_changed_at: null,
  label_ko: '국내 정지됨', admin_token_required: true, market: 'KRX', market_ko: '국내',
  halt_target: 'dd_stop', halt_target_ko: '시스템 · 드로다운 정지', dd_stop: true, auto_resume_at: null, auto_resumed: false,
  account_mode: 'paper', account_mode_ko: '모의투자', execution_mode: 'auto', execution_mode_ko: '자동 실행', ...over,
})
// a book that fell from 25,000,000 to 12,486,000 (−50.1%)
const BOOK = { equity: 12_486_000, peak: 25_000_000, drawdown: 12_486_000 / 25_000_000 - 1, as_of: '2026-06-08' }

describe('resumePlan', () => {
  it('offers the reset after a −20% stop, on by default, previewing the new peak and a zero drawdown', () => {
    const plan = resumePlan(halted({}), BOOK, true, 'KRX')
    expect(plan.offerReset).toBe(true)
    expect(plan.body).toEqual({ reset_peak: true })
    expect(plan.preview).toBe('피크 ₩25,000,000 → ₩12,486,000(2026-06-08 종가 평가액) · 드로다운 -50.1% → 0.0%')
  })
  it('unchecked: sends reset_peak false and says the stop comes back while still below −20%', () => {
    const plan = resumePlan(halted({}), BOOK, false, 'KRX')
    expect(plan.body).toEqual({ reset_peak: false })
    expect(plan.preview).toBe('피크 ₩25,000,000 유지 · 드로다운 -50.1% — −20% 아래면 다음 실행에서 다시 정지합니다')
  })
  it('in dollars for the US book', () => {
    const plan = resumePlan(halted({ market: 'US' }), { equity: 79_000.5, peak: 100_000, drawdown: -0.21, as_of: '2026-06-08' }, true, 'US')
    expect(plan.preview).toBe('피크 $100,000.00 → $79,000.50(2026-06-08 종가 평가액) · 드로다운 -21.0% → 0.0%')
  })
  it('a person\'s halt or a daily-loss halt offers no reset and sends reset_peak false (not the server default true)', () => {
    for (const st of [halted({ halt_target: 'manual', dd_stop: false }), halted({ halt_target: 'daily_loss', dd_stop: false })]) {
      expect(resumePlan(st, BOOK, true, 'KRX')).toEqual({ offerReset: false, preview: null, body: { reset_peak: false } })
    }
  })
  it('the all-markets resume offers the reset for another market\'s stop even when this one has none', () => {
    const plan = resumePlan(halted({ halt_target: 'manual', dd_stop: false }), null, false, 'KRX', ['미국'])
    expect(plan).toEqual({ offerReset: true, preview: '미국도 −20% 정지 — 같은 선택이 그 시장에도 적용됩니다', body: { reset_peak: false } })
  })
  it('both stopped: this market\'s preview, then the other market\'s note', () => {
    expect(resumePlan(halted({}), BOOK, true, 'KRX', ['미국']).preview)
      .toBe('피크 ₩25,000,000 → ₩12,486,000(2026-06-08 종가 평가액) · 드로다운 -50.1% → 0.0% · 미국도 −20% 정지 — 같은 선택이 그 시장에도 적용됩니다')
  })
  it('a person halting over the stop still offers it (dd_stop, not the last target, decides)', () => {
    expect(resumePlan(halted({ halt_target: 'manual', dd_stop: true }), BOOK, true, 'KRX').offerReset).toBe(true)
  })
  it('while the book loads the reset is offered without a preview number', () => {
    expect(resumePlan(halted({}), null, true, 'KRX')).toEqual({ offerReset: true, preview: '장부를 읽는 중', body: { reset_peak: true } })
  })
})
