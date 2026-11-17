import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import type { Health, LoopStatus, Order, Positions, RunRow } from '../api/types'
import { DecisionCard } from './DecisionCard'
import { FailureBand, RiskGauges } from './parts'
import { failureBand, runChipText, sectorGauges } from './partsLogic'

const run = (o: Partial<RunRow>): RunRow => ({
  market: 'KRX', stage: 'decide', stage_ko: '결정', trade_date: '2026-11-13', started_at: '2026-11-13T06:35:00Z',
  ended_at: '2026-11-13T06:36:00Z', status: 'ok', status_ko: '완료', reason: null, error: null, counts: {}, notes: [], ...o,
})
const loop = (o: Partial<LoopStatus>): LoopStatus => ({
  status: 'ok', status_ko: '정상', last: {}, failed: [], retry_at_kst: null,
  next: { at_kst: '2026-11-16T15:35:00+09:00', stage: 'decide', stage_ko: '결정', label_ko: '11-16 15:35 KST · 결정' }, ...o,
})
const t = (iso: string) => iso.slice(11, 16)

describe('runChipText: the server\'s next stage, or the retry of a failed one', () => {
  it('loading: no time made up on the client', () => {
    expect(runChipText(undefined, t)).toEqual({ text: '다음 실행 확인 중', failed: false })
  })
  it('fine: the scheduler\'s next stage as the server labels it', () => {
    expect(runChipText(loop({}), t)).toEqual({ text: '다음 실행 11-16 15:35 KST · 결정', failed: false })
  })
  it('a failed stage the scheduler retries first: the retry time, flagged', () => {
    const l = loop({ status: 'failed', failed: [run({ status: 'failed' })], retry_at_kst: '2026-11-16T11:00:00+09:00' })
    expect(runChipText(l, t)).toEqual({ text: '실행 실패 · 재시도 11:00 KST', failed: true })
  })
  it('a failed stage past its day (no retry): the next stage, still flagged', () => {
    expect(runChipText(loop({ status: 'failed', failed: [run({ status: 'failed' })] }), t))
      .toEqual({ text: '다음 실행 11-16 15:35 KST · 결정', failed: true })
  })
})

describe('failureBand (mockup ④)', () => {
  const refresh = { at: '2026-11-13T21:30:15+09:00', source: 'kis', symbols: 504, last_final_day: '2026-11-12',
    failed_count: 0, failed_symbols: [], failed_error: null, resolved_count: 0, resolved_symbols: [],
    last_success_at: '2026-11-13T21:30:15+09:00' }
  it('nothing failed, or nothing loaded: no band', () => {
    expect(failureBand(undefined)).toEqual([])
    expect(failureBand({ loop: loop({}), refresh })).toEqual([])
    expect(failureBand({ loop: loop({}), refresh: null })).toEqual([])
  })
  it('a failed stage: its stage, day and recorded error', () => {
    const lines = failureBand({ loop: loop({ status: 'failed', failed: [run({ status: 'failed', stage_ko: '체결', error: 'VendorUnavailableError: KIS 503' })] }), refresh })
    expect(lines).toEqual([{ kind: 'loop', title: '일일 루프 체결 실패 · 2026-11-13', detail: 'VendorUnavailableError: KIS 503', since: '2026-11-13T06:36:00Z' }])
  })
  it('a refresh that left symbols out: count of all, the names, the last full success', () => {
    const lines = failureBand({ loop: loop({}), refresh: { ...refresh, failed_count: 2, failed_symbols: ['BRK-B', 'BF-B'], failed_error: 'NoMarketDataError: x', last_success_at: null } })
    expect(lines).toEqual([{ kind: 'refresh', title: '캐시 갱신 실패 · 2 / 504 심볼', detail: 'BRK-B · BF-B — NoMarketDataError: x', since: null }])
  })
  it('failures a later update resolved: counted in the title, never a band of their own', () => {
    const lines = failureBand({ loop: loop({}), refresh: { ...refresh, failed_count: 1, failed_symbols: ['BF-B'], failed_error: 'NoMarketDataError: x',
      resolved_count: 1, resolved_symbols: ['BRK-B'], last_success_at: null } })
    expect(lines).toEqual([{ kind: 'refresh', title: '캐시 갱신 실패 · 1 / 504 심볼 (이후 해결 1)', detail: 'BF-B — NoMarketDataError: x', since: null }])
    expect(failureBand({ loop: loop({}), refresh: { ...refresh, resolved_count: 2, resolved_symbols: ['BRK-B', 'BF-B'], last_success_at: null } })).toEqual([])
  })
  it('more failures than names served: an ellipsis', () => {
    const lines = failureBand({ loop: loop({}), refresh: { ...refresh, failed_count: 12, failed_symbols: ['A'] } })
    expect(lines[0].detail).toBe('A …')
  })
  it('renders under the bar with the link to the cache status, and the retry time of a failed stage', () => {
    const h = { loop: loop({ status: 'failed', failed: [run({ status: 'failed', error: 'boom' })], retry_at_kst: '2026-11-16T11:00:00+09:00' }), refresh } as unknown as Health
    const out = renderToStaticMarkup(<MemoryRouter><FailureBand health={h} to="/settings?market=KRX" /></MemoryRouter>)
    expect(out).toContain('class="fail-band" role="alert"')
    expect(out).toContain('일일 루프 결정 실패 · 2026-11-13')
    expect(out).toContain('재시도 11-16 11:00 KST')
    expect(out).toContain('href="/settings?market=KRX"')
    expect(renderToStaticMarkup(<MemoryRouter><FailureBand health={{ loop: loop({}), refresh } as unknown as Health} to="/settings" /></MemoryRouter>)).toBe('')
  })
})

describe('sector gauges', () => {
  const base = {
    drawdown: 0, day_return: 0, max_position_weight: 0.1, max_position_ticker: null, gross_exposure: 0.4, today_buy_amount: 0,
    today_session: null, positions: [],
    limits: { dd_halve: -0.10, dd_stop: -0.20, daily_loss_limit: -0.02, max_position_pct: 0.10, max_gross_pct: 1.0, auto_daily_order_cap: 1, max_sector_pct: 0.20 },
  }
  it('three sectors against the cap, the 미분류 sum apart without one', () => {
    const p = { ...base, sector_exposure: [{ sector: 'Consumer Cyclical', pct: 0.2, classified: true }, { sector: 'Technology', pct: 0.17, classified: true },
      { sector: 'Industrials', pct: 0.05, classified: true }, { sector: '미분류', pct: 0.08, classified: false }] } as unknown as Positions
    expect(sectorGauges(p)).toEqual({ capped: [{ label: '섹터 · Consumer Cyclical', value: 0.2 }, { label: '섹터 · Technology', value: 0.17 },
      { label: '섹터 · Industrials', value: 0.05 }], unclassified: 0.08, empty: false })
    const out = renderToStaticMarkup(<RiskGauges p={p} amt={String} />)
    expect(out.match(/class="gauge /g)).toHaveLength(8)
    expect(out).toContain('aria-label="섹터 · Consumer Cyclical 20.0%, 한도 20.0%, 한도 초과"')
    expect(out).toContain('aria-label="섹터 · Technology 17.0%, 한도 20.0%, 한도 근접"')
    expect(out).toContain('aria-label="섹터 · Industrials 5.0%, 한도 20.0%"')
    expect(out).toContain('섹터 · 미분류')
    expect(out).not.toContain('<span>섹터 노출</span>')
  })
  it('no holdings: one "보유 없음" row, no sector gauge', () => {
    const p = { ...base, sector_exposure: [] } as unknown as Positions
    expect(sectorGauges(p)).toEqual({ capped: [], unclassified: null, empty: true })
    const out = renderToStaticMarkup(<RiskGauges p={p} amt={String} />)
    expect(out.match(/class="gauge /g)).toHaveLength(5)
    expect(out).toContain('<span>섹터 노출</span><span class="muted">보유 없음</span>')
  })
  it('every holding unclassified: no capped gauge, only the 미분류 row', () => {
    const p = { ...base, sector_exposure: [{ sector: '미분류', pct: 0.3, classified: false }] } as unknown as Positions
    expect(sectorGauges(p)).toEqual({ capped: [], unclassified: 0.3, empty: false })
  })
})

describe('a rerun the loop closed links to its answer', () => {
  const order = (o: Partial<Order>): Order => ({
    order_id: 'paper-2026-11-12-A.KS', ticker: 'A.KS', name: '에이', trade_date: '2026-11-12', side: 'Buy', side_ko: '매수', quantity: 0,
    status: 'exception', status_ko: '예외', exception_kind: 'review', reason: '등급을 읽을 수 없어', rules_applied: [], market: 'KRX',
    fill_at_local: null, fill_at_kst: null, fill_planned: false, plan: null, exception_action: 'rerun', exception_resolved: true,
    reruns: 1, retries: 0, retry_on: null, rerun_answer: null, placed_at: '2026-11-12T07:00:00Z', ...o,
  } as unknown as Order)
  const card = (o: Order) => renderToStaticMarkup(<MemoryRouter initialEntries={['/decisions?market=KRX']}>
    <DecisionCard d={{ ticker: 'A.KS', name: '에이', trade_date: '2026-11-12', rating: 'REVIEW', rating_ko: '검토', source: 'agent', source_kind: 'agent',
      orders: [o], evidence: [], hard_rule_overrides: [], thesis: '', market: 'KRX', tradeable: false } as never} />
  </MemoryRouter>)
  it('the answer chip goes to that day\'s decisions', () => {
    const out = card(order({ rerun_answer: { ticker: 'A.KS', trade_date: '2026-11-13', rating: 'Buy', rating_ko: '매수', attempt: 1, same_day: false } }))
    expect(out).toContain('재실행 1회째 답 · 2026-11-13 매수 →')
    expect(out).toMatch(/href="\/decisions\?date=2026-11-13[^"]*"/)
  })
  it('the day\'s own decision as the answer says so', () => {
    const out = card(order({ rerun_answer: { ticker: 'A.KS', trade_date: '2026-11-13', rating: 'Hold', rating_ko: '보유', attempt: 1, same_day: true } }))
    expect(out).toContain('그날 결정이 이 종목을 이미 판단해 그 결정이 답입니다')
  })
  it('no answer (not a closed rerun): no chip', () => {
    expect(card(order({}))).not.toContain('재실행 1회째 답')
  })
})
