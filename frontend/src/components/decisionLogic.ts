import type { Decision, ExceptionAction, ExceptionItem, Intervention, Order, OrderPlan, PlanSide } from '../api/types'

/** The branching behind the decision card's 원안 vs 최종 grid, its source badge and the exception-action modal
 *  (decisionParts.tsx), apart from the components so the tests read the rules directly. Numbers come from the server. */

export type ChipVariant = 'primary' | 'secondary' | 'live' | 'neutral' | 'hot' | 'new'

/** The source badge: agent / rule exit / rule baseline / a person's Hold, each its own colour. */
export const SOURCE_VARIANT: Record<Decision['source_kind'], ChipVariant> = {
  agent: 'primary', rule: 'hot', baseline: 'neutral', human: 'secondary',
}

const SIDE_KO: Record<PlanSide['side'], string> = { Buy: '매수', Sell: '매도', Hold: '보유' }

/** The grid's four rows, each value as shown and whether 원안 and 최종 differ (the highlighted cell). A side that
 *  places nothing (Hold) shows no weight. */
export function planRows(plan: OrderPlan, money: (v: number) => string): { label: string; a: string; b: string; diff: boolean }[] {
  const w = (s: PlanSide) => (s.side === 'Hold' || s.weight_pct == null ? '–' : `${(s.weight_pct * 100).toFixed(1)}%`)
  const rows = [
    ['방향', SIDE_KO[plan.proposed.side], SIDE_KO[plan.final.side]],
    ['비중', w(plan.proposed), w(plan.final)],
    ['수량', `${plan.proposed.qty.toLocaleString('ko-KR')}주`, `${plan.final.qty.toLocaleString('ko-KR')}주`],
    ['예상 금액', money(plan.proposed.amount), money(plan.final.amount)],
  ]
  return rows.map(([label, a, b]) => ({ label, a, b, diff: a !== b }))
}

/** A sized sell (Trim / Underweight, docs/04): the holding before (``plan.proposed``), the shares sold and what stays.
 *  null for an order without a sell plan (a full Sell, a rule exit, a buy) or one that placed nothing (Trim at target). */
export function sellSplit(o: Pick<Order, 'side' | 'plan' | 'quantity' | 'filled_quantity'>): { held: number; sold: number; rest: number } | null {
  if (o.side !== 'Sell' || o.plan?.proposed.side !== 'Sell' || o.plan.final.side !== 'Sell') return null
  const sold = o.filled_quantity || o.quantity
  return { held: o.plan.proposed.qty, sold, rest: o.plan.proposed.qty - sold }
}

/** The execution strip's order words: "부분 매도 499주 · 남은 500주" when a sell keeps part of the holding, else the side
 *  and the shares as before. */
export function orderWords(o: Pick<Order, 'side' | 'side_ko' | 'plan' | 'quantity' | 'filled_quantity'>): string {
  const s = sellSplit(o)
  return s && s.rest > 0 ? `부분 매도 ${s.sold}주 · 남은 ${s.rest}주` : `${o.side_ko} ${o.filled_quantity || o.quantity}주`
}

/** A cut (before → after) is a chip; a rule with neither blocked the order — a lock line. */
export function splitInterventions(ivs: Intervention[]): { cuts: Intervention[]; locks: Intervention[] } {
  return { cuts: ivs.filter((i) => i.before != null && i.after != null), locks: ivs.filter((i) => i.before == null || i.after == null) }
}

/** Every intervention on the decision's orders, in order. */
export function decisionInterventions(d: Decision): Intervention[] {
  return d.orders.flatMap((o) => o.plan?.interventions ?? [])
}

/** The modal variant: one per REVIEW action, or the order-exception one with its two buttons. */
export type ModalVariant = 'rerun' | 'hold' | 'dismiss' | 'order'

export const MODAL_TITLE: Record<ModalVariant, string> = {
  rerun: '결정 에이전트를 다시 실행할까요?', hold: '보유(Hold)로 처리할까요?', dismiss: '예외를 무시하고 닫을까요?',
  order: '미체결 주문을 어떻게 할까요?',
}

export function modalVariant(item: ExceptionItem, action?: ExceptionAction): ModalVariant {
  if (item.kind !== 'review') return 'order'
  return action === 'hold' || action === 'dismiss' ? action : 'rerun'
}

/** "처리 결과" before the person confirms: what each action will do, with the attempt it would be (n/2). */
export function actionPreview(item: ExceptionItem, variant: ModalVariant): string[] {
  const next = (a: ExceptionAction) => {
    const x = item.actions.find((y) => y.action === a)
    return x?.used != null && x.limit != null ? `${x.used + 1}/${x.limit}회` : ''
  }
  switch (variant) {
    case 'rerun':
      return [`다음 일일 루프 decide에서 그날 결정자가 ${item.trade_date} 선별을 바탕으로 다시 판단합니다 · 재실행 ${next('rerun')} (상한 2회)`,
        '답이 정상 등급이면 그날 결정으로 주문되고 이 예외는 닫힙니다 — 닫힌 카드에서 답한 결정으로 이동할 수 있습니다',
        '다시 등급을 읽지 못하면 그날 예외로 큐에 돌아옵니다']
    case 'hold':
      return ['등급을 보유(Hold)로 기록하고 주문하지 않습니다', '수동 등급은 보유(Hold)만 지정할 수 있습니다 — 매수·매도는 지정할 수 없음',
        "채점에서 제외 — 감사 기록에만 남고, 채점 표에는 '사람 처리 n건'으로 따로 표시"]
    case 'dismiss':
      return ['결정 없이 예외를 닫습니다 · 주문 없음', '결정 기록이 없으므로 채점 대상에서 빠집니다 (정산 없음)', '감사 기록에만 남습니다']
    case 'order':
      return [`다음 세션 재시도: 다음 실행이 그날 한도 · 하드 룰을 다시 거쳐 처음 수량 이하로 주문하고 그다음 세션 시가에 체결합니다 · 재시도 ${next('retry_next')}`,
        '무시: 주문 없이 닫고 결정은 "미체결"로 남습니다 (5일 정산은 결정 기준으로 그대로)',
        '한도 초과 · 벤더 장애 · 거래 정지로 막힌 주문 모두 같은 두 버튼입니다']
  }
}

/** The buttons the modal offers: the server's available actions for this variant. */
export function modalActions(item: ExceptionItem, variant: ModalVariant): ExceptionItem['actions'] {
  return item.actions.filter((a) => (variant === 'order' ? a.action === 'retry_next' || a.action === 'dismiss_order' : a.action === variant))
}

/** The audit line the action writes, as the operations log will hold it. */
export function auditLine(item: ExceptionItem, action: ExceptionAction, operator: string, reason: string): string {
  return `exception.${action} order_id=${item.order_id ?? '–'} ticker=${item.ticker} trade_date=${item.trade_date} `
    + `by=${operator.trim() || '운영자'} reason="${reason.trim() || '…'}"`
}
