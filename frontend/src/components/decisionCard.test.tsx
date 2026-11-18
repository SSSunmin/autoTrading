import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { money } from '../api/client'
import type { Decision, MemoCard, Order } from '../api/types'
import { actionWord, bodySlot, execSlot, NONE, priceCells, reasonChips, sections, sourceTag } from './cardLogic'
import { DecisionCard } from './DecisionCard'
import { ExceptionButtons } from './decisionParts'

// 결정 카드 v0.8 (mockup DecisionCard "확정 사항"): six fixed slots, "—" keeping a missing value's place, the card
// opening in place, reason chips, evidence source tags. Fixtures are made up — shapes as the server serves them.

const amt = (v: number | null | undefined) => money(v, 'US')
const MEMO: MemoCard = {
  kind: 'memo', business: '결제망 운영.', thesis: '결제 건수 성장이 이어진다.', bull: '해외 결제가 빨라진다', bear: '수수료 규제',
  valuation: 'PER 27배', price: 400, target: 460, downside: 365, catalysts: [{ event: '1분기 실적', date: '2027-01-28' }, { event: '연말 결제', date: null }],
  invalidation: ['결제 성장 한 자릿수', '법안 통과'], conviction: 0.55, sizing_pct: 8, horizon: '12개월', exit_plan: '목표가에서 절반',
  missing: [], extra: [], complete: true,
}
const order = (o: Partial<Order>): Order => ({
  order_id: 'paper-2026-11-17-V', ticker: 'V', name: '비자', trade_date: '2026-11-17', placed_at: '2026-11-17T21:10:00Z', side: 'Buy', side_ko: '매수',
  quantity: 40, limit_price: null, filled_quantity: 40, filled_price: 401, fill_date: '2026-11-18', amount: 16_040, status: 'filled', status_ko: '체결',
  exception_kind: null, reason: '', rules_applied: [], market: 'US', fill_at_local: null, fill_at_kst: null, fill_planned: false, plan: null,
  exception_action: null, exception_resolved: false, reruns: 0, retries: 0, retry_on: null, rerun_answer: null, ...o,
})
const dec = (d: Partial<Decision> = {}): Decision => ({
  ticker: 'V', name: '비자', trade_date: '2026-11-17', recorded_at: '', screen_run_id: null, rating: 'Buy', rating_ko: '매수', action: 'Buy',
  entry_price: 400, stop_loss: null, position_sizing: '8% of equity', confidence: 0.55, thesis: '**Rating**: Buy\n\n**Thesis**: 결제 건수 성장이 이어진다.',
  evidence: [{ source: 'market', summary: '종가 400', as_of: '2026-11-17', ref: null }, { source: 'other', summary: '선별 통과', as_of: '2026-11-17', ref: null },
    { source: 'portfolio', summary: '현금 90%', as_of: '2026-11-17', ref: null }],
  hard_rule_overrides: [], settings: {}, tradeable: true, source: 'agent', source_ko: '에이전트 결정', source_kind: 'agent', source_kind_ko: '에이전트 결정',
  has_intervention: false, market: 'US', outcome: null, orders: [], review: null, card: MEMO,
  price_ctx: { close: 400, avg_price: null, stop_loss_pct: -0.08, realized: {} }, ...d,
})
const html = (d: Decision, defaultOpen = false) =>
  renderToStaticMarkup(<MemoryRouter initialEntries={['/decisions?market=US']}><DecisionCard d={d} defaultOpen={defaultOpen} /></MemoryRouter>)
const slots = (out: string) => [...out.matchAll(/class="(dc-(?:head|price|exec|body|memo|ev|xa|foot))[ "]/g)].map((m) => m[1])

describe('six fixed slots, in the mockup order', () => {
  it('a folded memo card: head · prices · execution · body · evidence · buttons, nothing else', () => {
    const out = html(dec({ orders: [order({})] }))
    expect(slots(out)).toEqual(['dc-head', 'dc-price', 'dc-exec', 'dc-body', 'dc-ev', 'dc-foot'])
    expect(out).toContain('<span class="kind agent">에이전트 · 투자 메모</span><span class="rating Buy">매수</span><span class="act">매수</span>')
    expect(out).toContain('확신도 <b class="num">0.55</b>')
    expect(out.match(/class="pc"/g)).toHaveLength(4)
    expect(out.match(/class="dc-row c1"/g)).toHaveLength(2)
    expect(out).toContain('<b class="up">▲ 강세</b> 해외 결제가 빨라진다')
    expect(out).toContain('>전체 보기</button>')
  })

  it('a card with nothing to show keeps every slot and row, each missing value a "—"', () => {
    const bare: MemoCard = { ...MEMO, thesis: null, bull: null, bear: null, price: null, target: null, sizing_pct: null }
    const out = html(dec({ rating: 'Hold', rating_ko: '보유', confidence: null, entry_price: null, card: bare, evidence: [] }))
    expect(slots(out)).toEqual(['dc-head', 'dc-price', 'dc-exec', 'dc-body', 'dc-ev', 'dc-foot'])
    expect(out).toContain(`확신도 <b class="num">${NONE}</b>`)
    expect(out.match(/<b class="pv num">—<\/b>/g)).toHaveLength(4)            // all four price cells
    expect(out.match(/class="dc-row c1"/g)).toHaveLength(2)
    expect(out).toContain('<span class="xs none">주문 없음</span>')
    expect(out).toContain('증거 0건')
  })

  it('an exception on the queue adds its row below the evidence, the slots above unchanged', () => {
    const item = { kind: 'review', ticker: 'V', trade_date: '2026-11-17', order_id: null, actions: [] } as never
    const out = renderToStaticMarkup(<MemoryRouter><DecisionCard d={dec({ rating: 'REVIEW', rating_ko: '검토 필요', tradeable: false,
      card: { kind: 'raw', reason: '등급 줄이 없음', raw: 'Rating: Overweight-to-Hold', missing: [], extra: [], complete: true } })} exception={item} /></MemoryRouter>)
    expect(slots(out)).toEqual(['dc-head', 'dc-price', 'dc-exec', 'dc-body', 'dc-ev', 'dc-xa', 'dc-foot'])
    expect(out).toContain('<span class="rating REVIEW">⚠ 등급 못 읽음</span>')
    expect(out).toContain('<span class="lb">원문</span>')
    expect(out).toContain('예외 처리 · 하나를 고르세요')
  })
})

describe('the exception row: one button per action, an order exception too (mockup: 다음 세션 재시도 · 무시)', () => {
  const act = (action: string, label_ko: string) => ({ action, label_ko, used: action === 'retry_next' ? 0 : null, limit: action === 'retry_next' ? 2 : null, available: true })
  const item = (o: object) => ({ kind: 'vendor_unavailable', ticker: 'V', trade_date: '2026-11-17', order_id: 'paper-2026-11-17-V', pending_action_ko: null,
    actions: [act('retry_next', '다음 세션 재시도'), act('dismiss_order', '무시')], ...o }) as never
  const buttons = (x: never) => [...renderToStaticMarkup(<MemoryRouter><ExceptionButtons item={x} tokenRequired={false} /></MemoryRouter>)
    .matchAll(/<button([^>]*)>([^<]+)<\/button>/g)].map((m) => `${m[2]}${/disabled/.test(m[1]) ? ' (off)' : ''}`)
  it('two buttons for an order exception; off until the executor writes the order', () => {
    expect(buttons(item({}))).toEqual(['다음 세션 재시도', '무시'])
    expect(buttons(item({ order_id: null }))).toEqual(['다음 세션 재시도 (off)', '무시 (off)'])
  })
  it('a REVIEW keeps its three, live without an order id', () => {
    expect(buttons(item({ kind: 'review', order_id: null, actions: [act('rerun', '재실행'), act('hold', '보유로 처리'), act('dismiss', '무시')] })))
      .toEqual(['재실행', '보유로 처리', '무시'])
  })
})

describe('전체 보기 opens the card in place', () => {
  it('folded: two pieces of evidence and "+n건"; open: the memo under Korean headings, all evidence, 접기', () => {
    const folded = html(dec())
    expect(folded.match(/class="dc-ev-row"/g)).toHaveLength(2)
    expect(folded).toContain('+1건은 펼치면 보임')
    expect(folded).not.toContain('dc-memo')
    const open = html(dec(), true)
    expect(open).toContain('class="dc open"')
    expect(open.match(/class="dc-ev-row"/g)).toHaveLength(3)
    const heads = [...open.matchAll(/<section><div class="lb ?(?:up|down)? ?">([^<]+)<\/div>/g)].map((m) => m[1])
    expect(heads).toEqual(['사업', '논지', '▲ 강세', '▼ 약세', '밸류에이션', '촉매', '무효화 조건', '청산 계획 · 기간', '5일 정산'])
    expect(open).toContain('<span class="rc date">2027-01-28</span><span>1분기 실적</span>')
    expect(open).toContain('하락 시 가격 $365.00')
    expect(open).toContain('목표가에서 절반 · 12개월')
    expect(open).toContain('>접기</button>')
    expect(open).not.toContain('원문 전체')                                   // every field read
  })

  it('a text the parser did not read in full also shows the whole text, and every leftover field — nothing lost', () => {
    const part: MemoCard = { ...MEMO, bull: null, missing: ['Bull case'], extra: [{ label: 'Note', text: '추가 메모' }, { label: null, text: '떨어진 줄' }], complete: false }
    const s = sections(dec({ card: part, thesis: '**Thesis**: 원래 텍스트 전부' }), amt)
    expect(s.map((x) => x.label)).toEqual(['사업', '논지', '▼ 약세', '밸류에이션', '촉매', '무효화 조건', '청산 계획 · 기간', 'Note', '기타', '원문 전체'])
    expect(s.at(-1)).toEqual({ label: '원문 전체', text: '**Thesis**: 원래 텍스트 전부', pre: true })
    expect(bodySlot(dec({ card: part }), false).rows[0]).toEqual({ label: '▲ 강세', text: NONE, tone: 'up' })
  })
})

describe('REVIEW and the 하드룰 개입 grid (review 1R #1 · #3)', () => {
  it('REVIEW: 원문 전체 is the whole model text the server cut, or the record whole when it has none', () => {
    const raw = '첫 문단\n\n둘째 문단\n**Thesis**: y'
    const s = sections(dec({ card: { kind: 'raw', reason: 'x', raw, missing: [], extra: [], complete: true } }), amt)
    expect(s.filter((x) => x.label === '원문 전체')).toEqual([{ label: '원문 전체', text: raw, pre: true }])
    const none = sections(dec({ thesis: '**Rating**: REVIEW\n\n**사유**: x', card: { kind: 'raw', reason: 'x', raw: null, missing: ['원문'], extra: [], complete: false } }), amt)
    expect(none.filter((x) => x.label === '원문 전체')).toEqual([{ label: '원문 전체', text: '**Rating**: REVIEW\n\n**사유**: x', pre: true }])
  })
  it('the 원안 vs 최종 grid of a folded card sits below the evidence, every slot above it in place', () => {
    const plan = { proposed: { side: 'Buy', weight_pct: 0.08, qty: 15, amount: 6000 }, final: { side: 'Hold', weight_pct: null, qty: 0, amount: 0 },
      interventions: [{ rule: 'sector_cap', note: '섹터 한도' }] }
    const d = dec({ orders: [order({ status: 'blocked', status_ko: '차단', quantity: 0, filled_quantity: 0, plan } as never)] })
    const folded = renderToStaticMarkup(<MemoryRouter><DecisionCard d={d} showPlan /></MemoryRouter>)
    expect(folded.indexOf('class="plan"')).toBeGreaterThan(folded.indexOf('class="dc-ev"'))
    expect(folded.indexOf('class="plan"')).toBeLessThan(folded.indexOf('class="dc-foot"'))
    const open = html(d, true)
    expect(open.indexOf('class="plan"')).toBeLessThan(open.indexOf('class="dc-ev"'))    // open: in the mockup's place
    expect(html(d)).not.toContain('class="plan"')
  })
})

describe('reason chips (mockup §2-2)', () => {
  it('orders by force, fills the main one, keeps 하드 룰 amber, shows max and "+n"', () => {
    expect(reasonChips(['would_not_rebuy', 'thesis_broken', 'catalyst_gone'], 2)).toEqual([
      { text: '논지 훼손', cls: 'main' }, { text: '촉매 소멸', cls: 'sub' }, { text: '+1', cls: 'more' }])
    expect(reasonChips(['overweight', 'hard_rule'], 1)).toEqual([{ text: '하드 룰', cls: 'hard' }, { text: '+1', cls: 'more' }])
    expect(reasonChips(['overweight', 'hard_rule']).map((c) => c.cls)).toEqual(['hard', 'sub'])
    expect(reasonChips([])).toEqual([{ text: '사유 없음', cls: 'more' }])
    // a review card: two and "+n" folded, every reason open
    const rv = dec({ card: { kind: 'review', action: 'Exit', trim_to_pct: null, reasons: ['overweight', 'thesis_broken', 'catalyst_gone'], checks: [],
      reasoning: '청산', revised_target: null, missing: [], extra: [], complete: true } })
    expect(bodySlot(rv, false).chips.map((c) => c.text)).toEqual(['논지 훼손', '촉매 소멸', '+1'])
    expect(bodySlot(rv, true).chips.map((c) => c.text)).toEqual(['논지 훼손', '촉매 소멸', '비중 과다'])
    expect(reasonChips(['new_reason', 'overweight']).map((c) => c.text)).toEqual(['비중 과다', 'new_reason'])
  })
})

describe('evidence source tags (mockup §1-3)', () => {
  it('maps each known source to its Korean tag, anything else to 기타', () => {
    expect(['market', 'portfolio', 'news', 'fundamentals', 'memory'].map((s) => sourceTag(s).label)).toEqual(['시장', '포트폴리오', '뉴스', '재무', '메모리'])
    for (const s of ['other', 'sentiment', 'macro', 'filing']) expect(sourceTag(s)).toEqual({ label: '기타', cls: 'other' })
    expect(html(dec())).toContain('<span class="src-tag other">기타</span><span class="c1">선별 통과</span>')
  })
})

describe('price slot: four cells, the stop from the rule, "—" where the record has none', () => {
  it('a memo that buys: entry, target with upside, the stop at −8%, the weight', () => {
    expect(priceCells(dec(), amt)).toEqual([
      { label: '진입가', value: '$400.00', sub: '11-17 종가', tone: 'mute' }, { label: '목표가', value: '$460.00', sub: '+15.0%', tone: 'up' },
      { label: '손절가', value: '$368.00', sub: '-8.0%', tone: 'down' }, { label: '비중', value: '8.0%', sub: '', tone: 'mute' }])
  })
  it('a memo that holds: no stop, no weight; a rule cut shows the final weight and the plan', () => {
    const hold = priceCells(dec({ rating: 'Hold' }), amt)
    expect([hold[2].value, hold[3].value]).toEqual([NONE, NONE])
    const cut = dec({ orders: [order({ status: 'blocked', status_ko: '차단', quantity: 0, filled_quantity: 0, rules_applied: ['sector_cap'],
      plan: { proposed: { side: 'Buy', weight_pct: 0.08, qty: 15, amount: 6000 }, final: { side: 'Hold', weight_pct: null, qty: 0, amount: 0 }, interventions: [] } })] })
    expect(priceCells(cut, amt)[3]).toEqual({ label: '비중', value: '0.0%', sub: '원안 8.0%', tone: 'mute' })
  })
  it('a held name: the close against the average, the stop line on the average', () => {
    const review = dec({ card: { kind: 'review', action: 'Hold', trim_to_pct: null, reasons: [], checks: [], reasoning: '유지', revised_target: null, missing: [], extra: [], complete: true },
      price_ctx: { close: 506, avg_price: 525, stop_loss_pct: -0.08, realized: {} } })
    const c = priceCells(review, amt)
    expect(c.map((x) => x.label)).toEqual(['현재가', '목표가', '손절선', '비중'])
    expect(c[0]).toEqual({ label: '현재가', value: '$506.00', sub: '평단 대비 -3.6%', tone: 'down' })
    expect([c[2].value, c[2].sub]).toEqual(['$483.00', '-4.5%'])
    expect([c[1].value, c[3].value]).toEqual([NONE, NONE])
    const under = priceCells({ ...review, price_ctx: { ...review.price_ctx, close: 480 } }, amt)
    expect(under[2].sub).toBe('도달')                                      // the close at or under the stop line
  })
  it('a rule exit: the body names the rule and the order (the record has no lock text)', () => {
    const exit = dec({ source: 'rule', source_kind: 'rule', rating: 'Sell', settings: { exit_reason: 'stop_loss' },
      card: { kind: 'text', label: null, text: '손절선 아래 마감', missing: [], extra: [], complete: true },
      orders: [order({ side: 'Sell', side_ko: '매도', rules_applied: ['stop_loss'] })] })
    const b = bodySlot(exit, false, { stop_loss: '손절' })
    expect([b.label, b.chips, b.sum]).toEqual(['규칙 사유', [{ text: '손절', cls: 'rule' }], '손절선 아래 마감'])
    expect(b.rows.map((r) => r.label)).toEqual(['규칙', '주문'])
    expect(b.rows[0].text).toBe('stop_loss')
    expect(b.rows[1].text).not.toBe(NONE)
  })
})

describe('execution slot: the badge and three cells per state (mockup §1-1, §2-4)', () => {
  it('buy filled · sell partial with its realized P&L · blocked · exception · none', () => {
    expect(execSlot(dec({ orders: [order({})] }), amt).cells.map((c) => c.value)).toEqual(['매수 40주', '$401.00', '$16,040.00'])
    const sell = order({ order_id: 'paper-2026-11-17-MSFT', side: 'Sell', side_ko: '매도', quantity: 14, filled_quantity: 14, filled_price: 513.1 })
    const part = dec({ orders: [sell], price_ctx: { close: 512, avg_price: 468.2, stop_loss_pct: -0.08, realized: { [sell.order_id]: { pnl: 628.6, full: false } } } })
    expect(execSlot(part, amt).cells).toEqual([{ label: '주문', value: '부분 매도 14주', tone: '' }, { label: '체결가', value: '$513.10', tone: '' },
      { label: '실현 손익', value: '+$628.60', tone: 'up' }])
    expect(actionWord(part)).toBe('부분 매도')
    const blocked = dec({ orders: [order({ status: 'blocked', status_ko: '차단', quantity: 0, filled_quantity: 0, rules_applied: ['no_short'], side: 'Sell', side_ko: '매도' })] })
    expect(execSlot(blocked, amt)).toEqual({ kind: 'blocked', label: '차단', cells: [{ label: '막은 규칙', value: 'no_short', tone: '' },
      { label: '원안', value: '매도', tone: '' }, { label: '최종', value: '0주', tone: '' }] })
    expect(actionWord(blocked)).toBe('주문 없음')
    const exc = execSlot(dec({ orders: [order({ status: 'exception', status_ko: '예외', exception_kind: 'vendor_unavailable', quantity: 23, filled_quantity: 0 })] }), amt,
      { exceptionKind: { vendor_unavailable: '데이터 벤더 장애' } })
    expect(exc.cells).toEqual([{ label: '사유', value: '데이터 벤더 장애', tone: '' }, { label: '주문', value: '매수 23주 미체결', tone: '' }, { label: '처리', value: '대기 중', tone: 'fail' }])
    expect(execSlot(dec({ rating: 'Hold' }), amt).cells.map((c) => c.value)).toEqual(['등급 보유', NONE, NONE])
  })
})

describe('the slot heights are fixed in CSS while folded, minimums when open', () => {
  const css = readFileSync(join(__dirname, '..', 'app.css'), 'utf8')
  const rule = (sel: string) => css.match(new RegExp(`(?:^|\\n)${sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} \\{([^}]*)\\}`))?.[1] ?? ''
  it('holds the mockup heights: head 80 · prices 94 · execution 84 · body 148 · evidence 76', () => {
    for (const [sel, h] of [['.dc-head', '8rem'], ['.dc-price', '9.4rem'], ['.dc-exec', '8.4rem'], ['.dc-body', '14.8rem'], ['.dc-ev', '7.6rem']]) {
      expect(rule(sel).split(';').map((x) => x.trim())).toContain(`height: ${h}`)     // height itself, not min-height
      expect(rule(sel)).toContain('overflow: hidden')
      // open, a slot grows from its folded height — but the price cells stay as they are (one line each)
      if (sel === '.dc-price') expect(rule(`.dc.open ${sel}`)).toBe('')
      else expect(rule(`.dc.open ${sel}`)).toContain(`height: auto; min-height: ${h};`)
    }
    expect(css).toContain('.dc.open .dc-price .c1, .dc.open .dc-exec-cells .c1 { white-space: nowrap; overflow: hidden; }')
    expect(rule('.dc-sum')).toContain('-webkit-line-clamp: 3')
    expect(rule('.dc-grid')).toContain('minmax(34rem, 1fr)')
  })
  it('the tabs in the mockup order, the red badge on the queue alone', () => {
    const page = readFileSync(join(__dirname, '..', 'pages', 'Decisions.tsx'), 'utf8')
    expect([...page.matchAll(/\{ id: '(\w+)', label: <>/g)].map((m) => m[1])).toEqual(['all', 'reviews', 'exceptions', 'override'])
    expect(page.match(/count\([^)]*, true\)/g)).toEqual(['count(nExc, true)'])
  })
})
