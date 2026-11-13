import type { ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { MarketSummary, ResumePreview, TradingState } from '../api/types'
import { ExitBar, ResumeBody } from './risk'
import { exitSegments, fillTimeLabel, modeBadge, previewFresh, resumeOffer, resumeReady } from './riskLogic'

const html = (el: ReactElement) => renderToStaticMarkup(el)

// the server's responses, written out by hand
const state = (over: Partial<TradingState>): TradingState => ({
  mode: 'paper', mode_ko: '모의투자', running: false, halted_at: '2026-07-27T06:40:00Z', halted_by: 'system',
  halted_by_ko: '시스템 자동 정지', halt_reason: '일일 손실 한도 초과: -2.31% (한도 -2%)', keep_exits: true, mode_changed_at: null,
  label_ko: '국내 정지됨', admin_token_required: false, market: 'KRX', market_ko: '국내',
  halt_target: 'daily_loss', halt_target_ko: '시스템 · 일일 손실 정지', dd_stop: false,
  auto_resume_at: '2026-07-28T00:00:00+09:00', auto_resumed: false, account_mode: 'paper', account_mode_ko: '모의투자',
  execution_mode: 'auto', execution_mode_ko: '자동 실행', halt_scope: 'KRX', halt_scope_ko: '국내만',
  auto_resume_open_kst: '2026-07-28T09:00:00+09:00', auto_resume_label_ko: '07-28(화) 09:00 KST', ...over,
})
const DAILY = state({})
const PERSON = state({ halted_by: '운영자A', halted_by_ko: '운영자A', halt_target: 'manual', halt_target_ko: '운영자 긴급 정지',
  halt_reason: '벤더 점검', auto_resume_at: null, auto_resume_open_kst: null, auto_resume_label_ko: null, halt_scope: 'all', halt_scope_ko: '전체 시장' })
const DD = state({ halt_target: 'dd_stop', halt_target_ko: '시스템 · 드로다운 정지', dd_stop: true, halt_reason: '드로다운 정지',
  auto_resume_at: null, auto_resume_open_kst: null, auto_resume_label_ko: null })

const preview = (over: Partial<ResumePreview>): ResumePreview => ({
  market: 'KRX', variant: 'manual', variant_ko: '사람 정지', can_resume: true, reset_peak: false, equity: null, as_of: null,
  peak_before: null, base: null, restops: false, lines: [], ...over,
})
const DD_ON = preview({ variant: 'dd_stop', variant_ko: '−20% 정지', reset_peak: true, equity: 72_986_500, as_of: '2026-06-12',
  peak_before: 100_000_000, base: 72_986_500, lines: [
    { id: 'peak', label_ko: '피크 · DD 0%', level: 0, amount: 72_986_500, vs_equity: 0 },
    { id: 'dd_halve', label_ko: '반감 −10%', level: -0.1, amount: 65_687_850, vs_equity: 0.1111 },
    { id: 'dd_stop', label_ko: '정지 −20%', level: -0.2, amount: 58_389_200, vs_equity: 0.25 }] })
const DD_OFF = preview({ variant: 'dd_stop', variant_ko: '−20% 정지', equity: 72_986_500, as_of: '2026-06-12', peak_before: 100_000_000,
  base: 100_000_000, restops: true, lines: [
    { id: 'peak', label_ko: '피크 · DD 0%', level: 0, amount: 100_000_000, vs_equity: -0.2701 },
    { id: 'dd_halve', label_ko: '반감 −10%', level: -0.1, amount: 90_000_000, vs_equity: -0.189 },
    { id: 'dd_stop', label_ko: '정지 −20%', level: -0.2, amount: 80_000_000, vs_equity: -0.0877 }] })
const FORM = { scope: 'market' as const, resetPeak: true, operator: '', reason: '', token: '' }
const body = (s: TradingState, p: ResumePreview, form = FORM, others: MarketSummary[] = [], previewError: string | null = null) =>
  html(<ResumeBody state={s} market="KRX" preview={p} others={others} form={form} set={() => {}} error={null} previewError={previewError} />)

describe('modeBadge', () => {
  it('a system daily-loss halt shows the server label it lifts at', () => {
    expect(modeBadge(DAILY)).toEqual({ variant: 'hot', text: '● 정지 · 시스템(일일 손실) · 07-28(화) 09:00 KST 자동 해제' })
  })
  it('a person halt and the −20% stop need a resume', () => {
    expect(modeBadge(PERSON)).toEqual({ variant: 'hot', text: '● 정지 · 운영자 · 재가동 필요' })
    expect(modeBadge(DD).text).toBe('● 정지 · 시스템(−20% 드로다운) · 재가동 필요')
  })
  it('running keeps the server label', () => {
    expect(modeBadge(state({ running: true, mode: 'auto', label_ko: '국내 자동 매매 실행 중' }))).toEqual({ variant: 'live', text: '● 국내 자동 매매 실행 중' })
  })
})

describe('resume modal', () => {
  it('daily loss: the release notice only — no scope, no reason, no reset', () => {
    const h = body(DAILY, preview({ variant: 'daily_loss', can_resume: false }))
    expect(h).toContain('07-28(화) 09:00 KST에 자동 해제됩니다')
    expect(h).not.toContain('재가동 범위')
    expect(h).not.toContain('재가동 사유')
    expect(h).not.toContain('드로다운 피크')
    expect(resumeOffer(preview({ variant: 'daily_loss', can_resume: false }), true, 1, true)).toEqual(
      { canResume: false, showReset: false, showTable: false, resetPeak: false })
  })
  it('a person halt: scope and reason, no reset and no table', () => {
    const h = body(PERSON, preview({}))
    expect(h).toContain('재가동 범위')
    expect(h).toContain('재가동 사유')
    expect(h).toContain('사람</span> 운영자 운영자A')
    expect(h).not.toContain('드로다운 피크')
    expect(h).not.toContain('<table')
  })
  it('−20% stop: reset on by default with the server lines', () => {
    const h = body(DD, DD_ON)
    expect(h).toContain('드로다운 피크를 지금 평가액으로 재설정')
    expect(h).toContain('새 피크 ₩72,986,500')
    expect(h).toContain('재설정 후 기준 금액')
    expect(h).toContain('₩58,389,200')
    expect(h).toContain('+25.0%')
    expect(h).not.toContain('alert-t')
  })
  it('−20% stop with the reset off: the old peak and the stop row flagged', () => {
    const h = body(DD, DD_OFF, { ...FORM, resetPeak: false })
    expect(h).toContain('옛 피크 기준 금액')
    expect(h).toContain('지금 이미 -27.0%라 재가동 직후 다시 정지됩니다')
    expect(h).toMatch(/<tr class="alert-t"><td>정지 −20%<\/td>/)
    expect(resumeOffer(DD_OFF, false, 0, false).resetPeak).toBe(false)
  })
  it('right after the checkbox changes, the last preview is not shown under the other labels', () => {
    expect(previewFresh(DD_ON, false)).toBe(false)
    expect(previewFresh(DD_ON, true)).toBe(true)
    expect(previewFresh(preview({}), false)).toBe(true)               // no lines to go stale
    const h = body(DD, DD_ON, { ...FORM, resetPeak: false })
    expect(h).not.toContain('<table')
    expect(h).toContain('미리보기를 다시 읽는 중')
    expect(h).not.toContain('₩58,389,200')
    expect(body(DD, DD_ON, { ...FORM, resetPeak: false }, [], 'API 오류')).toContain('미리보기 실패')
  })
  it('the all-markets resume offers the reset for another stopped market, and never sends it unseen', () => {
    expect(resumeOffer(preview({}), true, 1, true)).toMatchObject({ showReset: true, showTable: false, resetPeak: true })
    expect(resumeOffer(preview({}), false, 1, true)).toMatchObject({ showReset: false, resetPeak: false })
  })
  it('operator, reason and (when set) the token are required', () => {
    expect(resumeReady({ operator: '관리자', reason: '재개', token: '', tokenRequired: false })).toBe(true)
    expect(resumeReady({ operator: '관리자', reason: ' ', token: '', tokenRequired: false })).toBe(false)
    expect(resumeReady({ operator: '', reason: '재개', token: '', tokenRequired: false })).toBe(false)
    expect(resumeReady({ operator: '관리자', reason: '재개', token: '', tokenRequired: true })).toBe(false)
  })
})

describe('exit-reason bar', () => {
  it('widths are the counts over their sum, rule reasons in their fixed order', () => {
    expect(exitSegments({ trailing: 1, stop_loss: 3, delisted: 0 })).toEqual([
      { reason: 'stop_loss', n: 3, share: 0.75 }, { reason: 'trailing', n: 1, share: 0.25 }])
    expect(exitSegments({})).toEqual([])
    const h = html(<ExitBar exits={{ stop_loss: 3, trailing: 1 }} labels={{ stop_loss: '손절', trailing: '트레일링' }} />)
    expect(h).toContain('role="img"')
    expect(h).toContain('aria-label="청산 사유 분포: 손절 3건, 트레일링 1건"')
    expect(h).toContain('style="width:75%"')
    expect(h).toContain('style="width:25%"')
    expect(h).toContain('3건')
    expect(h).not.toMatch(/>\d+%</)                                      // the legend shows counts, no share
  })
})

describe('fill time in two zones', () => {
  it('US summer and winter from the server ISO, no offset worked out here', () => {
    expect(fillTimeLabel({ fill_at_kst: '2026-07-01T22:30:00+09:00', fill_at_local: '2026-07-01T09:30:00-04:00', fill_planned: false }, 'US', 'America/New_York'))
      .toBe('체결 2026-07-01 22:30 KST (09:30 ET)')
    expect(fillTimeLabel({ fill_at_kst: '2026-12-01T23:30:00+09:00', fill_at_local: '2026-12-01T09:30:00-05:00', fill_planned: true }, 'US', 'America/New_York'))
      .toBe('체결 예정 2026-12-01 23:30 KST (09:30 ET)')
  })
  it('KRX is KST only; an order that does not fill has none', () => {
    expect(fillTimeLabel({ fill_at_kst: '2026-07-01T09:00:00+09:00', fill_at_local: '2026-07-01T09:00:00+09:00', fill_planned: false }, 'KRX', 'Asia/Seoul'))
      .toBe('체결 2026-07-01 09:00 KST')
    expect(fillTimeLabel({ fill_at_kst: null, fill_at_local: null, fill_planned: false }, 'US', 'America/New_York')).toBeNull()
  })
})
