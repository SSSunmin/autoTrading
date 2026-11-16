import type { CapitalAccount, CapitalInfo, Market, Mode, TradingState } from '../api/types'

/** The amount typed in the capital field: separators dropped, whole won / up to cents. null: not an amount. */
export function parseAmount(text: string, market: Market): number | null {
  const t = text.replace(/[,\s원₩$]/g, '')
  if (!t) return null
  const re = market === 'KRX' ? /^\d+$/ : /^\d+(\.\d{1,2})?$/
  if (!re.test(t)) return null
  const v = Number(t)
  return Number.isFinite(v) ? v : null
}

/** Why the change button stays off (null: it can go). The server checks the same and answers 422 / 409. */
export function capitalProblem(amount: number | null, info: CapitalInfo, account: CapitalAccount, money: (v: number) => string): string | null {
  if (amount == null) return info.market === 'KRX' ? '금액을 원 단위 숫자로 입력하세요' : '금액을 달러로 입력하세요 (센트까지)'
  if (amount <= 0) return '0보다 커야 합니다'
  if (amount > info.max_amount) return `상한 ${money(info.max_amount)}을 넘습니다 (입력 오타 방지)`
  const now = account === 'paper' ? info.paper.amount : info.live.amount
  if (now === amount) return '지금 금액과 같습니다'
  return null
}

/** What the change will do — the confirm modal's result lines, from the server's ``next_paper_change``. */
export function changePreview(info: CapitalInfo, account: CapitalAccount, amount: number, money: (v: number) => string): string[] {
  const small = amount < info.small_threshold
    ? [`${money(info.small_threshold)} 미만 — 소액 1종목 모드: 종목당 상한 대신 1종목에 자본 전체를 씁니다`] : []
  if (account === 'live') {
    return [`실거래 자본을 ${money(amount)}으로 저장합니다`, '실주문 경로는 아직 없습니다 — 미리보기와 실거래 전환 모달에만 쓰입니다', ...small]
  }
  if (info.next_paper_change === 'start') {
    return [`모의 시작 자본을 ${money(info.paper.amount)} → ${money(amount)}으로 바꿉니다`, '장부에 주문이 없어 처음부터 이 금액으로 셉니다', ...small]
  }
  const flow = amount - info.paper.amount
  return [
    `오늘 종가에 ${flow > 0 ? '입금' : '출금'} ${money(Math.abs(flow))} — 모의 자본 ${money(info.paper.amount)} → ${money(amount)}`,
    '수익률 · 드로다운 · 모의 판정은 이 입출금을 빼고 셉니다(시간 가중) — 입금이 수익으로, 출금이 손실로 보이지 않습니다',
    ...(flow < 0 ? ['출금이 장부 현금보다 크면 서버가 거절합니다(409)'] : []),
    ...small,
  ]
}

/** The operations-log line the change writes (shown before it is written). */
export function capitalAuditLine(info: CapitalInfo, account: CapitalAccount, amount: number, operator: string, reason: string,
  money: (v: number) => string): string {
  const before = account === 'paper' ? info.paper.amount : info.live.amount
  return `capital_set · ${info.market} · ${account} · ${before == null ? '미설정' : money(before)} → ${money(amount)} · `
    + `${operator.trim() || '(운영자)'}: ${reason.trim() || '(사유)'}`
}

export const ACCOUNTS = [{ value: 'paper', label: '모의투자' }, { value: 'live', label: '실거래' }]
export const EXECUTIONS = [{ value: 'auto', label: '자동 실행' }, { value: 'manual', label: '수동 승인' }]

/** The one stored mode from the two axes (docs/10 D4): paper = 모의투자 + 자동, auto = 실거래 + 자동, manual = 실거래 + 수동. */
export function axesMode(account: 'paper' | 'live', execution: 'auto' | 'manual'): Mode {
  return account === 'paper' ? 'paper' : execution
}

/** The stored mode as two axes (docs/10 D4): account paper | live, execution auto | manual. Paper runs automatic only
 *  (the stored mode has no paper + manual), so the execution axis is locked there. */
export function modeAxes(state: TradingState): { account: 'paper' | 'live'; execution: 'auto' | 'manual'; executionLocked: string | null } {
  return {
    account: state.account_mode,
    execution: state.execution_mode,
    executionLocked: state.account_mode === 'paper' ? '모의투자는 자동 실행만 — 수동 승인은 실거래 계좌에서 고릅니다' : null,
  }
}
