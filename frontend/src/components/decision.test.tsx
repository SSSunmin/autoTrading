import type { ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { Decision, ExceptionItem, OrderPlan } from '../api/types'
import { ExceptionBody, Interventions, PlanGrid, SourceBadge } from './decisionParts'
import {
  SOURCE_VARIANT, actionPreview, decisionInterventions, modalActions, modalVariant, orderWords, planRows, sellSplit,
  splitInterventions,
} from './decisionLogic'

const html = (el: ReactElement) => renderToStaticMarkup(el)
const won = (v: number) => `${Math.round(v).toLocaleString('ko-KR')}원`

// the server's plan for a buy the per-order cap shrank (tests/test_decision_cards.py, by hand)
const SHRUNK: OrderPlan = {
  proposed: { side: 'Buy', weight_pct: 1.0, qty: 99935, amount: 999_350_000 },
  final: { side: 'Buy', weight_pct: 0.03, qty: 3000, amount: 30_000_000 },
  interventions: [
    { rule: 'max_position_pct', before: '100.0%', after: '10.0%', note: 'max_position_pct: A.KS 100.0% → 10.0%' },
    { rule: 'auto_order_max', before: '9,993만', after: '3,000만', note: 'auto_order_max: 9,993만 → 3,000만 (9993주 → 3000주)' },
  ],
}
const BLOCKED: OrderPlan = {
  proposed: { side: 'Buy', weight_pct: 0.125, qty: 75, amount: 29_850_000 },
  final: { side: 'Hold', weight_pct: null, qty: 0, amount: 0 },
  interventions: [{ rule: 'Cooldown', before: null, after: null, note: 'Cooldown: 005380.KS 청산 후 5거래일 재진입 금지' }],
}

const item = (over: Partial<ExceptionItem>): ExceptionItem => ({
  kind: 'review', kind_ko: '등급 읽기 실패 (REVIEW)', ticker: '105560.KS', name: 'KB금융', trade_date: '2026-07-24',
  summary: '등급을 읽을 수 없어 주문하지 않았습니다', detail: 'Recommendation: …', ref: null, market: 'KRX', review: null,
  order_id: 'paper-2026-07-24-105560.KS', pending_action: null, pending_action_ko: null, reruns: 0, retries: 0,
  actions: [
    { action: 'rerun', label_ko: '재실행', used: 0, limit: 2, available: true },
    { action: 'hold', label_ko: '보유로 처리', used: null, limit: null, available: true },
    { action: 'dismiss', label_ko: '무시', used: null, limit: null, available: true },
  ], ...over,
})
const ORDER = item({ kind: 'luld_halt', kind_ko: 'LULD 거래 정지', ticker: 'CVX', name: '셰브론', market: 'US', order_id: 'paper-2026-07-23-CVX',
  actions: [{ action: 'retry_next', label_ko: '다음 세션 재시도', used: 1, limit: 2, available: true },
    { action: 'dismiss_order', label_ko: '무시', used: null, limit: null, available: true }] })
const EMPTY = { operator: '', reason: '', token: '' }
const body = (x: ExceptionItem, variant: ReturnType<typeof modalVariant>, form = EMPTY, tokenRequired = false) =>
  html(<ExceptionBody item={x} variant={variant} form={form} set={() => {}} tokenRequired={tokenRequired} error={null} />)

describe('원안 vs 최종 grid', () => {
  it('rows from the server plan, the changed ones flagged', () => {
    expect(planRows(SHRUNK, won)).toEqual([
      { label: '방향', a: '매수', b: '매수', diff: false },
      { label: '비중', a: '100.0%', b: '3.0%', diff: true },
      { label: '수량', a: '99,935주', b: '3,000주', diff: true },
      { label: '예상 금액', a: '999,350,000원', b: '30,000,000원', diff: true },
    ])
  })
  it('a blocked buy reads 보유 with no weight', () => {
    expect(planRows(BLOCKED, won).map((r) => [r.b, r.diff])).toEqual([['보유', true], ['–', true], ['0주', true], ['0원', true]])
  })
  it('only the differing final cells carry the highlight', () => {
    const h = html(<PlanGrid plan={SHRUNK} market="KRX" />)
    expect(h.match(/class="num diff"/g)).toHaveLength(3)
    expect(h).toContain('<td class="num">매수</td><td class="num">매수</td>')
  })
  it('cuts become chips, a lock its own line', () => {
    expect(splitInterventions([...SHRUNK.interventions, ...BLOCKED.interventions]).locks.map((i) => i.rule)).toEqual(['Cooldown'])
    const h = html(<Interventions ivs={[...SHRUNK.interventions, ...BLOCKED.interventions]} />)
    expect(h).toContain('auto_order_max 9,993만 → 3,000만')
    expect(h).toContain('잠금</span> Cooldown: 005380.KS 청산 후 5거래일 재진입 금지')
    expect(html(<Interventions ivs={[]} />)).toBe('')
  })
  it('a decision gathers its orders’ interventions', () => {
    const d = { orders: [{ plan: SHRUNK }, { plan: null }] } as unknown as Decision
    expect(decisionInterventions(d).map((i) => i.rule)).toEqual(['max_position_pct', 'auto_order_max'])
  })
})

// the server's plans for sells (tests/test_partial_sell.py, by hand): 999 held, a Trim to 5% sells 499
const TRIM: OrderPlan = {
  proposed: { side: 'Sell', weight_pct: 0.0999, qty: 999, amount: 9_990_000 },
  final: { side: 'Sell', weight_pct: 0.0499, qty: 499, amount: 4_990_000 }, interventions: [],
}
const AT_TARGET: OrderPlan = { proposed: TRIM.proposed, final: { side: 'Hold', weight_pct: null, qty: 0, amount: 0 }, interventions: [] }
const sell = (plan: OrderPlan | null, quantity: number, filled_quantity = 0, side: 'Buy' | 'Sell' = 'Sell') =>
  ({ side, side_ko: side === 'Sell' ? '매도' : '매수', plan, quantity, filled_quantity })

describe('partial sell on the card', () => {
  it('a sell that keeps part reads 부분 매도 with what stays, before and after the fill', () => {
    expect(sellSplit(sell(TRIM, 499))).toEqual({ held: 999, sold: 499, rest: 500 })
    expect(orderWords(sell(TRIM, 499))).toBe('부분 매도 499주 · 남은 500주')
    expect(orderWords(sell(TRIM, 499, 499))).toBe('부분 매도 499주 · 남은 500주')
  })
  it('a sized sell of the whole holding (Trim to 0%, Underweight of one share) reads as a sale', () => {
    const all: OrderPlan = { ...TRIM, final: { ...TRIM.proposed } }
    expect(sellSplit(sell(all, 999))?.rest).toBe(0)
    expect(orderWords(sell(all, 999, 999))).toBe('매도 999주')
  })
  it('a full Sell, a rule exit, a Trim at its target and a buy keep the side and shares', () => {
    expect(sellSplit(sell(null, 999))).toBeNull()
    expect(orderWords(sell(null, 999, 999))).toBe('매도 999주')
    expect(sellSplit(sell(AT_TARGET, 0))).toBeNull()
    expect(orderWords(sell(AT_TARGET, 0))).toBe('매도 0주')
    expect(orderWords(sell(SHRUNK, 3000, 3000, 'Buy'))).toBe('매수 3000주')
  })
})

describe('source badge', () => {
  it('four kinds, four colours, the server label', () => {
    expect(SOURCE_VARIANT).toEqual({ agent: 'primary', rule: 'hot', baseline: 'neutral', human: 'secondary' })
    expect(new Set(Object.values(SOURCE_VARIANT)).size).toBe(4)
    expect(html(<SourceBadge d={{ source_kind: 'human', source_kind_ko: '사람 처리' }} />)).toContain('사람 처리')
  })
})

describe('exception-action modal', () => {
  it('a REVIEW opens one variant per button; an order exception opens the two-button one', () => {
    expect(modalVariant(item({}), 'hold')).toBe('hold')
    expect(modalVariant(item({}), 'dismiss')).toBe('dismiss')
    expect(modalVariant(item({}), 'rerun')).toBe('rerun')
    expect(modalVariant(ORDER, 'retry_next')).toBe('order')
    expect(modalActions(ORDER, 'order').map((a) => a.action)).toEqual(['retry_next', 'dismiss_order'])
    expect(modalActions(item({}), 'hold').map((a) => a.action)).toEqual(['hold'])
  })
  it('the preview counts the attempt it would be', () => {
    expect(actionPreview(item({}), 'rerun')[0]).toContain('재실행 1/2회')
    expect(actionPreview(ORDER, 'order')[0]).toContain('재시도 2/2회')
    expect(actionPreview(item({}), 'hold')[1]).toContain('보유(Hold)만')
  })
  it('before the reason: the result list, the audit line and the reminder', () => {
    const h = body(item({}), 'hold')
    expect(h).toContain('처리 결과')
    expect(h).toContain('채점에서 제외')
    expect(h).toContain('exception.hold order_id=paper-2026-07-24-105560.KS')
    expect(h).toContain('reason=&quot;…&quot;')
    expect(h).toContain('사유를 적어야 처리 버튼이 켜집니다')
    expect(h).not.toContain('관리자 토큰')
  })
  it('after the reason: it shows in the audit line and the reminder goes', () => {
    const h = body(ORDER, 'order', { operator: '운영자A', reason: '일시 거래정지', token: '' }, true)
    expect(h).toContain('exception.retry_next order_id=paper-2026-07-23-CVX ticker=CVX')
    expect(h).toContain('by=운영자A reason=&quot;일시 거래정지&quot;')
    expect(h).not.toContain('사유를 적어야')
    expect(h).toContain('관리자 토큰')
    expect(h).toContain('LULD 거래 정지')
  })
  it('dismiss and rerun describe what they do', () => {
    expect(body(item({}), 'dismiss')).toContain('결정 없이 예외를 닫습니다')
    expect(body(item({}), 'rerun')).toContain('다음 일일 루프 decide에서 그날 결정자가')
    expect(body(item({}), 'rerun')).not.toContain('연결되지 않아')
  })
})
