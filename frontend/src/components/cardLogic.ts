import { pct } from '../api/client'
import type { Decision, Evidence, Order } from '../api/types'
import { orderWords, sellSplit, splitInterventions } from './decisionLogic'

/** The decision card's six fixed slots (mockup v0.8 DecisionCard "확정 사항"): what each slot holds, worked out apart
 *  from the component so the tests read the rules directly. A slot always has the same number of rows; a missing value
 *  is "—", never a missing row. Numbers come from the server (``card`` · ``price_ctx`` · orders); only formatting here. */

export const NONE = '—'

export type Tone = 'up' | 'down' | 'mute' | 'fail' | ''

/** The seven sell reasons, the most binding first (mockup §2-2), with the mockup's words. */
export const REASON_ORDER = ['hard_rule', 'thesis_broken', 'target_reached', 'catalyst_gone', 'better_alternative', 'overweight',
  'would_not_rebuy'] as const
export const REASON_KO: Record<string, string> = {
  hard_rule: '하드 룰', thesis_broken: '논지 훼손', target_reached: '목표가 도달', catalyst_gone: '촉매 소멸',
  better_alternative: '더 나은 대안', overweight: '비중 과다', would_not_rebuy: '다시 사지 않음',
}

export interface Chip { text: string; cls: 'main' | 'sub' | 'hard' | 'rule' | 'more' }

/** A sell review's reasons as chips: in the mockup's order, the first filled (the main reason), 하드 룰 always amber,
 *  ``max`` shown and "+n" for the rest (a card 2, a table cell 1; undefined: all). None given: "사유 없음". An unknown
 *  reason keeps its value and goes last. */
export function reasonChips(reasons: string[], max?: number): Chip[] {
  if (reasons.length === 0) return [{ text: '사유 없음', cls: 'more' }]
  const rank = (r: string) => { const i = (REASON_ORDER as readonly string[]).indexOf(r); return i < 0 ? REASON_ORDER.length : i }
  const sorted = [...reasons].sort((a, b) => rank(a) - rank(b))
  const shown = max == null ? sorted : sorted.slice(0, max)
  const chips: Chip[] = shown.map((r, i) => ({ text: REASON_KO[r] ?? r, cls: r === 'hard_rule' ? 'hard' : i === 0 ? 'main' : 'sub' }))
  if (sorted.length > shown.length) chips.push({ text: `+${sorted.length - shown.length}`, cls: 'more' })
  return chips
}

/** Evidence source tags (mockup §1-3): a fixed colour and Korean name each; any other source is 기타. */
export const SOURCE_TAG: Record<string, string> = {
  market: '시장', portfolio: '포트폴리오', news: '뉴스', fundamentals: '재무', memory: '메모리',
}
export function sourceTag(source: Evidence['source'] | string): { label: string; cls: string } {
  return source in SOURCE_TAG ? { label: SOURCE_TAG[source], cls: source } : { label: '기타', cls: 'other' }
}

/** The head's first chip: who decided and what kind (mockup §2-3) — the agent's memo or sell review, the rules' exit. */
export function kindTag(d: Pick<Decision, 'source_kind' | 'card'>): { label: string; cls: 'agent' | 'rule' | 'human' } {
  if (d.source_kind === 'rule') return { label: '규칙 · 청산', cls: 'rule' }
  if (d.source_kind === 'baseline') return { label: '규칙 · 베이스라인', cls: 'rule' }
  if (d.source_kind === 'human') return { label: '사람 · 처리', cls: 'human' }
  return { label: d.card.kind === 'review' ? '에이전트 · 매도 검토' : '에이전트 · 투자 메모', cls: 'agent' }
}

/** The rating pill: a sell review shows its verdict, a rule exit 청산, REVIEW "⚠ 등급 못 읽음". */
export function ratingPill(d: Pick<Decision, 'rating' | 'rating_ko' | 'source_kind' | 'card'>): { rating: Decision['rating']; label: string } {
  if (d.rating === 'REVIEW') return { rating: 'REVIEW', label: '⚠ 등급 못 읽음' }
  if (d.source_kind === 'rule') return { rating: 'Sell', label: '청산' }
  if (d.card.kind === 'review' && d.card.action) {
    return ({ Hold: { rating: 'Hold', label: '유지' }, Trim: { rating: 'Underweight', label: '축소' }, Exit: { rating: 'Sell', label: '청산' } } as const)[d.card.action]
  }
  return { rating: d.rating, label: d.rating_ko }
}

/** The card's own order: the first one that is not a cancelled leftover (a halt), else the first. */
export function mainOrder(d: Pick<Decision, 'orders'>): Order | undefined {
  return d.orders.find((o) => o.status !== 'cancelled') ?? d.orders[0]
}

/** A sell that keeps part of the holding: the book's replay says so (``full`` false), or the order's plan does. */
function partial(d: Pick<Decision, 'price_ctx'>, o: Order): boolean {
  const r = d.price_ctx.realized[o.order_id]
  if (r) return !r.full
  const s = sellSplit(o)
  return s != null && s.rest > 0
}

/** The head's action word: what the card's order does — 매수, 부분 매도, 전량 매도 — or 주문 없음 (none, or blocked). */
export function actionWord(d: Pick<Decision, 'orders' | 'price_ctx'>): string {
  const o = mainOrder(d)
  if (!o || o.status === 'blocked' || o.status === 'rejected' || o.status === 'cancelled') return '주문 없음'
  if (o.side === 'Buy') return '매수'
  return partial(d, o) ? '부분 매도' : '전량 매도'
}

export function confidenceText(c: number | null | undefined): string {
  return c == null ? NONE : c.toFixed(2)
}

export interface PriceCell { label: string; value: string; sub: string; tone: Tone }

const tone = (v: number | null | undefined): Tone => (v == null || v === 0 ? 'mute' : v > 0 ? 'up' : 'down')
const weight = (w: number | null | undefined) => (w == null ? NONE : `${(w * 100).toFixed(1)}%`)

/** ② 가격, always four cells: 진입가 (a memo) or 현재가 (a review, a rule exit, anything else — the decision day's close,
 *  against the holding's average when held) · 목표가 (upside) · 손절가 / 손절선 (the stop rule on the entry or the
 *  average) · 비중 (the order's final weight, the rules' cut beside it). A value the record does not have is "—". */
export function priceCells(d: Pick<Decision, 'card' | 'entry_price' | 'rating' | 'trade_date' | 'orders' | 'price_ctx'>,
  money: (v: number | null | undefined) => string): PriceCell[] {
  const ctx = d.price_ctx, o = mainOrder(d), plan = o?.plan
  const day = d.trade_date.slice(5)
  const cell = (label: string, value: number | null | undefined, sub = '', t: Tone = 'mute'): PriceCell =>
    ({ label, value: value == null ? NONE : money(value), sub: value == null ? '' : sub, tone: t })
  const planWeight = (): PriceCell | null => {
    if (!plan) return null
    if (plan.proposed.side === 'Sell') return { label: '비중', value: `${weight(plan.proposed.weight_pct)} → ${weight(plan.final.side === 'Sell' ? plan.final.weight_pct : plan.proposed.weight_pct)}`, sub: '', tone: 'mute' }
    const final = plan.final.side === 'Hold' ? 0 : plan.final.weight_pct
    const cut = final !== plan.proposed.weight_pct
    return { label: '비중', value: weight(final), sub: cut ? `원안 ${weight(plan.proposed.weight_pct)}` : '', tone: 'mute' }
  }
  if (d.card.kind === 'memo') {
    const entry = d.card.price ?? d.entry_price
    const buys = d.rating === 'Buy' || d.rating === 'Overweight'
    const stop = buys && entry != null ? entry * (1 + ctx.stop_loss_pct) : null
    const target = d.card.target
    return [
      cell('진입가', entry, `${day} 종가`),
      cell('목표가', target, target != null && entry ? pct(target / entry - 1) : '', 'up'),
      cell('손절가', stop, pct(ctx.stop_loss_pct, 1), 'down'),
      planWeight() ?? { label: '비중', value: buys && d.card.sizing_pct != null ? `${d.card.sizing_pct.toFixed(1)}%` : NONE, sub: '', tone: 'mute' },
    ]
  }
  const close = ctx.close, avg = ctx.avg_price
  const target = d.card.kind === 'review' ? d.card.revised_target : null
  const stop = avg != null ? avg * (1 + ctx.stop_loss_pct) : null
  const vsAvg = close != null && avg ? close / avg - 1 : null
  const trim = d.card.kind === 'review' && d.card.action === 'Trim' && d.card.trim_to_pct != null ? `→ ${d.card.trim_to_pct.toFixed(1)}%` : null
  return [
    cell('현재가', close, vsAvg != null ? `평단 대비 ${pct(vsAvg)}` : `${day} 종가`, vsAvg != null ? tone(vsAvg) : 'mute'),
    cell('목표가', target, target != null && close ? pct(target / close - 1) : '', 'up'),
    // the mockup: how far the close is above the stop, or 도달 once the close is at or under it
    cell('손절선', stop, stop != null && close ? (close <= stop ? '도달' : pct(stop / close - 1)) : '', 'down'),
    planWeight() ?? { label: '비중', value: trim ?? NONE, sub: '', tone: 'mute' },
  ]
}

export type ExecKind = 'filled' | 'blocked' | 'exception' | 'open' | 'none'

export interface ExecSlot { kind: ExecKind; label: string; cells: { label: string; value: string; tone: Tone }[] }

/** ③ 실행 상태: the badge and three fixed cells by state (mockup §1-1) — 매수 체결 주문 · 체결가 · 금액, 매도 체결
 *  주문 · 체결가 · 실현 손익, 차단 막은 규칙 · 원안 · 최종, 예외 사유 · 주문 · 처리, 미체결 주문 · 체결 예정 · 예상 금액,
 *  주문 없음 사유 · 주문 · 처리. The whole sentence is on the open card, below. */
export function execSlot(d: Pick<Decision, 'orders' | 'price_ctx' | 'rating' | 'source' | 'card' | 'tradeable'>,
  money: (v: number | null | undefined) => string, labels: { exceptionKind?: Record<string, string>; action?: Record<string, string> } = {}): ExecSlot {
  const o = mainOrder(d)
  const c = (label: string, value: string | null | undefined, t: Tone = '') => ({ label, value: value || NONE, tone: t })
  const qty = (x: Order) => x.filled_quantity || x.quantity
  const sideWords = (x: Order) => (x.side === 'Sell' && partial(d, x) ? '부분 매도' : x.side_ko)
  const plan = o?.plan
  const planSide = (s: NonNullable<typeof plan>['proposed']) => `${s.side === 'Hold' ? '0.0%' : weight(s.weight_pct)} · ${s.qty.toLocaleString('ko-KR')}주`
  if (!o) {
    const why = d.rating === 'REVIEW' ? '등급 못 읽음' : d.source === 'human' ? '사람 처리' : d.card.kind === 'review' ? '판정 유지'
      : d.rating === 'Hold' ? '등급 보유' : d.tradeable ? '실행 전' : '주문 없음'
    return { kind: 'none', label: '주문 없음', cells: [c('사유', why), c('주문', null), c('처리', null)] }
  }
  if (o.status === 'filled' || o.status === 'partial') {
    const r = d.price_ctx.realized[o.order_id]
    return { kind: 'filled', label: o.status_ko, cells: [c('주문', `${sideWords(o)} ${qty(o).toLocaleString('ko-KR')}주`), c('체결가', money(o.filled_price)),
      o.side === 'Sell' ? c('실현 손익', r ? `${r.pnl > 0 ? '+' : ''}${money(r.pnl)}` : null, r ? tone(r.pnl) : '') : c('금액', money(o.amount))] }
  }
  if (o.status === 'blocked' || o.status === 'rejected') {
    const rule = o.rules_applied[0] ?? plan?.interventions[0]?.rule
    return { kind: 'blocked', label: o.status_ko, cells: [c('막은 규칙', rule), c('원안', plan ? planSide(plan.proposed) : o.side_ko), c('최종', plan ? planSide(plan.final) : '0주')] }
  }
  if (o.status === 'exception') {
    const why = (o.exception_kind && labels.exceptionKind?.[o.exception_kind]) || (d.rating === 'REVIEW' ? '등급 못 읽음' : o.reason)
    const answer = o.rerun_answer ? `재실행 ${o.rerun_answer.attempt}회째 답 · ${o.rerun_answer.trade_date} ${o.rerun_answer.rating_ko}` : null
    const done = answer ?? (o.exception_resolved ? (o.exception_action && labels.action?.[o.exception_action]) || '처리됨' : null)
    return { kind: 'exception', label: o.status_ko, cells: [c('사유', why), c('주문', o.quantity > 0 ? `${o.side_ko} ${o.quantity.toLocaleString('ko-KR')}주 미체결` : null),
      done ? c('처리', done) : c('처리', '대기 중', 'fail')] }
  }
  if (o.status === 'open') {
    return { kind: 'open', label: o.status_ko, cells: [c('주문', `${sideWords(o)} ${o.quantity.toLocaleString('ko-KR')}주`), c('체결', o.fill_planned ? '다음 시가' : null),
      c('예상 금액', plan && plan.final.side !== 'Hold' ? money(plan.final.amount) : null)] }
  }
  return { kind: 'none', label: o.status_ko, cells: [c('사유', o.reason), c('주문', `${o.side_ko} ${o.quantity.toLocaleString('ko-KR')}주`), c('처리', null)] }
}

export interface BodySlot { label: string; chips: Chip[]; sum: string; rows: { label: string; text: string; tone: Tone }[] }

/** ④ 본문: a memo's 논지 (three lines) with ▲ 강세 and ▼ 약세 (a line each); a sell review's reasons, its reasoning and
 *  the 점검 · 목표 lines; a rule exit's rule and note; REVIEW's text and why. Two rows always (blank when none). */
export function bodySlot(d: Pick<Decision, 'card' | 'thesis' | 'settings' | 'orders'>, open: boolean, exitKo: Record<string, string> = {}): BodySlot {
  const k = d.card
  const pad = (rows: BodySlot['rows']) => [...rows, { label: '', text: '', tone: '' as Tone }, { label: '', text: '', tone: '' as Tone }].slice(0, 2)
  if (k.kind === 'memo') {
    return { label: '논지', chips: [], sum: k.thesis ?? NONE,
      rows: [{ label: '▲ 강세', text: k.bull ?? NONE, tone: 'up' }, { label: '▼ 약세', text: k.bear ?? NONE, tone: 'down' }] }
  }
  if (k.kind === 'review') {
    const met = k.checks.filter((x) => x.met).length
    const goal = k.action === 'Trim' ? `축소 후 비중 ${k.trim_to_pct != null ? `${k.trim_to_pct.toFixed(1)}%` : NONE}` : k.action === 'Exit' ? '전량 매도' : '비중 유지'
    return { label: '사유', chips: reasonChips(k.reasons, open ? undefined : 2), sum: k.reasoning ?? NONE,
      rows: [{ label: '점검', text: `무효화 조건 ${k.checks.length}개 중 ${met}개 해당`, tone: '' }, { label: '목표', text: goal, tone: '' }] }
  }
  if (k.kind === 'raw') {
    return { label: '원문', chips: [], sum: k.raw ?? d.thesis, rows: pad([{ label: '사유', text: k.reason ?? NONE, tone: '' }]) }
  }
  const reason = typeof d.settings.exit_reason === 'string' ? d.settings.exit_reason : null
  if (reason) {
    // the parser's own heading, when the text had one, is its only place on the card (review 1R #2)
    return { label: k.label ?? '규칙 사유', chips: [{ text: exitKo[reason] ?? reason, cls: 'rule' }], sum: k.text,
      rows: [{ label: '규칙', text: d.orders.flatMap((o) => o.rules_applied).join(' · ') || reason, tone: '' },
        { label: '주문', text: d.orders[0] ? orderWords(d.orders[0]) : NONE, tone: '' }] }
  }
  return { label: k.label ?? '내용', chips: [], sum: k.text || NONE, rows: pad([]) }
}

export interface Section { label: string; tone?: Tone; text?: string; items?: { chip?: string; chipCls?: string; text: string; sub?: string }[]; pre?: boolean }

/** The open card's fields under Korean headings (mockup §1-2), less what the slots above already show in full once open
 *  (card-dedupe, 2026-11-18 — the user saw the same text twice): a memo's 사업 · 하락 시 가격 · 밸류에이션 · 촉매 (date
 *  chips) · 무효화 조건 · 청산 계획 · 기간 (논지 · 강세 · 약세 are ④); a review's 무효화 점검 (해당 / 해당 없음) (판정 사유 and
 *  the 목표 line are ④, 수정 목표가 is ②); a REVIEW's and a rule exit's text is ④ alone. Then any field the parser did not
 *  use, and — when it did not read every field — the whole text (원문 전체), unless ④ shows that very text. Rating,
 *  conviction, prices and the weight stay in the head and the price slot. */
export function sections(d: Pick<Decision, 'card' | 'thesis'>, money: (v: number | null | undefined) => string): Section[] {
  const k = d.card
  const out: Section[] = []
  const text = (label: string, t: string | null | undefined, tn?: Tone) => { if (t) out.push({ label, text: t, tone: tn }) }
  if (k.kind === 'memo') {
    text('사업', k.business)
    text('하락 시 가격', k.downside != null ? money(k.downside) : null, 'down')
    text('밸류에이션', k.valuation)
    if (k.catalysts.length) out.push({ label: '촉매', items: k.catalysts.map((c) => ({ chip: c.date ?? undefined, chipCls: 'date', text: c.event })) })
    if (k.invalidation.length) out.push({ label: '무효화 조건', items: k.invalidation.map((t) => ({ text: t })) })
    text('청산 계획 · 기간', [k.exit_plan, k.horizon].filter(Boolean).join(' · '))
  } else if (k.kind === 'review' && k.checks.length) {
    out.push({ label: '무효화 점검', items: k.checks.map((c) => ({ chip: c.met ? '해당' : '해당 없음', chipCls: c.met ? 'met' : 'unmet', text: c.condition, sub: c.evidence ?? undefined })) })
  }
  for (const x of k.extra) out.push({ label: x.label ?? '기타', text: x.text })
  // ④'s text on a REVIEW is the model's whole text, or the record whole when the server cut none (review 1R #1)
  const inBody = k.kind === 'raw' ? k.raw ?? d.thesis : k.kind === 'text' ? k.text : null
  if (!k.complete && d.thesis !== inBody) out.push({ label: '원문 전체', text: d.thesis, pre: true })
  return out
}

/** The open card's order lines under ③: each order's sentence (side and shares, fill price, amount, reason) and its
 *  처리됨 chip, less each piece ③'s cells (the card's own order), ④'s rows (a rule exit's first order) or the order's
 *  잠금 lines under its 원안 vs 최종 grid (a halt's reason is its lock note, measured on KB금융 · 셀트리온) already say —
 *  an order whose every piece is said has no line. A piece is said when a cell or row holds it as whole words, digit
 *  grouping aside ("매수 23주" in "매수 23주 미체결", "매도 1400주" in "부분 매도 1,400주" — ③ groups, orderWords not). */
export function execLines(d: Pick<Decision, 'orders'>, exec: ExecSlot, body: BodySlot, money: (v: number | null | undefined) => string,
  actionKo: Record<string, string> = {}): { id: string; text: string; done: string | null }[] {
  const main = mainOrder(d)
  return d.orders.flatMap((x) => {
    // ④ names an order only on a rule exit (its 주문 row, the first order); a memo's bull/bear is no order (review 1R #1)
    const ruleRow = body.rows.find((r) => r.label === '주문' && body.chips.some((c) => c.cls === 'rule'))
    const shown = [...(x === main ? exec.cells.map((c) => c.value) : []), ...(ruleRow && x === d.orders[0] ? ruleRow.text.split(' · ') : []),
      ...splitInterventions(x.plan?.interventions ?? []).locks.map((i) => i.note)]       // the open card's 잠금 line under its grid
    const words = (s: string) => ` ${s.replaceAll(',', '')} `
    const said = (p: string) => shown.some((v) => words(v).includes(words(p)))
    const fill = x.filled_price != null ? [money(x.filled_price), money(x.amount)] : []
    const text = [...orderWords(x).split(' · '), ...fill, x.reason].filter((p) => p && !said(p)).join(' · ')
    const act = x.exception_resolved && x.exception_action ? actionKo[x.exception_action] ?? x.exception_action : null
    const done = act && !said(act) ? `처리됨 · ${act}` : null
    return text || done ? [{ id: x.order_id, text, done }] : []
  })
}

/** ⑤ 증거: the count, the first two (all on the open card) and "+n건" for the rest. */
export function evidenceSlot(ev: Evidence[], open: boolean): { count: number; shown: Evidence[]; more: number } {
  const shown = open ? ev : ev.slice(0, 2)
  return { count: ev.length, shown, more: ev.length - shown.length }
}
