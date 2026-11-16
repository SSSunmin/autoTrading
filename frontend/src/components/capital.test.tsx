import type { ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { money } from '../api/client'
import type { CapitalInfo, CapitalPreview, TradingState } from '../api/types'
import { CapitalModalBody, CapitalRows, PreviewBody } from './capital'
import { axesMode, capitalAuditLine, capitalProblem, changePreview, modeAxes, parseAmount } from './capitalLogic'

const html = (el: ReactElement) => renderToStaticMarkup(el)
const won = (v: number) => money(v, 'KRX')

const info = (o: Partial<CapitalInfo> = {}): CapitalInfo => ({
  market: 'KRX', market_ko: '국내', currency: 'KRW',
  paper: { account: 'paper', label_ko: '모의투자 자본', amount: 10_000_000, start: 10_000_000, source: 'default', set: null, small_account: false },
  live: { account: 'live', label_ko: '실거래 자본', amount: null, source: 'unset', set: null, small_account: null },
  cash_flows: [], next_paper_change: 'start', next_paper_change_ko: '시작 자본 교체', small_threshold: 1_000_000,
  max_amount: 100_000_000_000, unit: 1, account_mode: 'paper', admin_token_required: false, note_ko: '', ...o,
})
const state = (o: Partial<TradingState>): TradingState => ({
  mode: 'paper', mode_ko: '모의투자', running: true, halted_at: null, halted_by: null, halted_by_ko: null, halt_reason: null,
  keep_exits: true, mode_changed_at: null, label_ko: '국내 모의투자 · 자동 실행 중', admin_token_required: false, market: 'KRX',
  market_ko: '국내', halt_target: null, halt_target_ko: null, dd_stop: false, auto_resume_at: null, auto_resumed: false,
  account_mode: 'paper', account_mode_ko: '모의투자', execution_mode: 'auto', execution_mode_ko: '자동 실행', halt_scope: null,
  halt_scope_ko: null, auto_resume_open_kst: null, auto_resume_label_ko: null, ...o,
} as TradingState)
const form = (o = {}) => ({ operator: '', reason: '', token: '', ...o })

describe('parseAmount: whole won, up to cents', () => {
  it('separators dropped; KRX takes no decimals, US up to two', () => {
    expect(parseAmount('₩5,000,000', 'KRX')).toBe(5_000_000)
    expect(parseAmount('1.5', 'KRX')).toBeNull()
    expect(parseAmount('$999.99', 'US')).toBe(999.99)
    expect(parseAmount('1.005', 'US')).toBeNull()
    expect(parseAmount('', 'US')).toBeNull()
    expect(parseAmount('-5', 'KRX')).toBeNull()
  })
})

describe('capitalProblem: the change button stays off with a reason', () => {
  it('no amount, zero, over the cap, the same amount; a new amount goes', () => {
    expect(capitalProblem(null, info(), 'paper', won)).toMatch('원 단위')
    expect(capitalProblem(0, info(), 'paper', won)).toBe('0보다 커야 합니다')
    expect(capitalProblem(100_000_000_001, info(), 'paper', won)).toMatch('상한')
    expect(capitalProblem(10_000_000, info(), 'paper', won)).toBe('지금 금액과 같습니다')
    expect(capitalProblem(10_000_000, info(), 'live', won)).toBeNull()                // live is unset
    expect(capitalProblem(20_000_000, info(), 'paper', won)).toBeNull()
  })
})

describe('changePreview: what the change does, from the server\'s next_paper_change', () => {
  it('no order yet: a new starting cash', () => {
    expect(changePreview(info(), 'paper', 20_000_000, won)[0]).toBe('모의 시작 자본을 ₩10,000,000 → ₩20,000,000으로 바꿉니다')
  })
  it('running: a deposit or a withdrawal at today\'s close, time-weighted; a withdrawal warns of the cash check', () => {
    const dep = changePreview(info({ next_paper_change: 'flow' }), 'paper', 15_000_000, won)
    expect(dep[0]).toBe('오늘 종가에 입금 ₩5,000,000 — 모의 자본 ₩10,000,000 → ₩15,000,000')
    expect(dep.some((t) => t.includes('409'))).toBe(false)
    const wd = changePreview(info({ next_paper_change: 'flow' }), 'paper', 8_000_000, won)
    expect(wd[0]).toMatch('출금 ₩2,000,000'); expect(wd.some((t) => t.includes('409'))).toBe(true)
  })
  it('live: stored only; under the threshold the small-account line', () => {
    const live = changePreview(info(), 'live', 500_000, won)
    expect(live[0]).toBe('실거래 자본을 ₩500,000으로 저장합니다')
    expect(live.at(-1)).toMatch('소액 1종목 모드')
  })
})

describe('the confirm modal: required inputs and the audit line', () => {
  it('before operator and reason: the need line; after: gone, and the audit line names them', () => {
    const empty = html(<CapitalModalBody info={info()} account="paper" amount={20_000_000} form={form()} set={() => {}} error={null} />)
    expect(empty).toContain('관리자 · 사유을 적어야 변경 버튼이 켜집니다')
    expect(empty).not.toContain('관리자 토큰')
    const filled = html(<CapitalModalBody info={info()} account="paper" amount={20_000_000} form={form({ operator: 'kim', reason: '증액' })}
      set={() => {}} error={null} />)
    expect(filled).not.toContain('변경 버튼이 켜집니다')
    expect(filled).toContain('capital_set · KRX · paper · ₩10,000,000 → ₩20,000,000 · kim: 증액')
  })
  it('the server has a token: the field shows and is required', () => {
    const t = info({ admin_token_required: true })
    const html1 = html(<CapitalModalBody info={t} account="live" amount={500_000} form={form({ operator: 'kim', reason: 'r' })} set={() => {}} error={null} />)
    expect(html1).toContain('관리자 토큰'); expect(html1).toContain('관리자 · 사유 · 토큰을 적어야')
    expect(capitalAuditLine(t, 'live', 500_000, '', '', won)).toBe('capital_set · KRX · live · 미설정 → ₩500,000 · (운영자): (사유)')
  })
  it('a server error shows in the modal', () => {
    expect(html(<CapitalModalBody info={info()} account="paper" amount={1} form={form()} set={() => {}} error="장부 현금보다 큽니다" />))
      .toContain('장부 현금보다 큽니다')
  })
})

describe('CapitalRows: both accounts and the flows', () => {
  it('unset live, a running paper book with a pending deposit', () => {
    const r = html(<CapitalRows info={info({
      paper: { ...info().paper, amount: 15_000_000, source: 'record', set: { at: '2026-11-16T01:00:00Z', operator: 'kim', role: 'admin', reason: '증액' } },
      cash_flows: [{ date: '2026-11-16', amount: 5_000_000, pending: true, operator: 'kim', reason: '증액' }],
    })} />)
    expect(r).toContain('미설정'); expect(r).toContain('시작 ₩10,000,000 + 입출금 1건')
    expect(r).toContain('+₩5,000,000'); expect(r).toContain('그날 종가가 가격 데이터에 들어오면')
    expect(r).not.toContain('소액 1종목 모드')
  })
})

const preview = (o: Partial<CapitalPreview> = {}): CapitalPreview => ({
  market: 'KRX', currency: 'KRW', account: 'paper', account_ko: '모의투자 자본', amount: 5_000_000, small_account: false,
  small_threshold: 1_000_000, as_of: '2026-11-13',
  universe: { id: 'loop', label_ko: '지금 루프 유니버스', label: 'KOSPI200 sample (20)', size: 20, priced: 20, missing: [], missing_count: 0, sector_ready: 20, name_ready: 20 },
  buyable: 12, none_buyable: false, cheapest: null, over_order_cap: [], over_order_cap_count: 0,
  rules: [{ id: 'max_position_pct', label_ko: '종목당 비중 상한', setting_ko: '10%', amount: 500_000, value_ko: '₩500,000', note_ko: '', warn: false }],
  note_ko: '', ...o,
})

describe('PreviewBody: the server\'s preview by branch', () => {
  it('normal: the readiness line and the rule rows, no warnings', () => {
    const p = html(<PreviewBody p={preview()} />)
    expect(p).toContain('가격 20/20 · 섹터 20/20 · 한글명 20/20'); expect(p).toContain('₩500,000')
    expect(p).not.toContain('소액 1종목 모드'); expect(p).not.toContain('살 수 있는 종목 없음')
  })
  it('small account and none buyable: both warnings, the cheapest named', () => {
    const p = html(<PreviewBody p={preview({ small_account: true, none_buyable: true, buyable: 0,
      cheapest: { ticker: '000660.KS', name: 'SK하이닉스', price: 1_200_000 } })} />)
    expect(p).toContain('소액 1종목 모드'); expect(p).toContain('가장 싼 종목이 SK하이닉스 ₩1,200,000')
  })
  it('names over the per-order cap and missing prices are listed', () => {
    const p = html(<PreviewBody p={preview({ over_order_cap: [{ ticker: 'BRK-A', name: null, price: 700_000 }], over_order_cap_count: 1,
      universe: { ...preview().universe, priced: 19, missing: ['035720.KS'], missing_count: 1 } })} />)
    expect(p).toContain('1주가 주문당 상한을 넘는 종목 1개: BRK-A'); expect(p).toContain('가격 없음 035720.KS')
  })
})

describe('two axes on the one stored mode (docs/10 D4)', () => {
  it('stored mode → axes, axes → stored mode, round trip', () => {
    expect(modeAxes(state({}))).toEqual({ account: 'paper', execution: 'auto', executionLocked: expect.stringContaining('자동 실행만') })
    expect(modeAxes(state({ mode: 'manual', account_mode: 'live', execution_mode: 'manual' })).executionLocked).toBeNull()
    expect([axesMode('paper', 'manual'), axesMode('live', 'auto'), axesMode('live', 'manual')]).toEqual(['paper', 'auto', 'manual'])
  })
})
