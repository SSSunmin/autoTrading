import type { ReactNode } from 'react'

import { pct } from './api/client'
import type { Market, RulesSettings } from './api/types'

/** glossary-tips (2026-11-19, the user is new to investing: "드로다운, 그로스 노출 … 옆에 설명 달아줘"): one entry per word a
 *  screen shows, the ⓘ beside its first place on that screen (Tip). A rule's value comes from /api/settings/rules
 *  (``Rules``), never from here — with no rules loaded the text is the general meaning alone. */

/** the rule values one market runs with: the hard rules, its auto-execution limits, the paper criteria, its index */
export interface Rules {
  n: (id: string) => number | undefined
  index: string
}

export function rulesOf(s: RulesSettings, market: Market): Rules {
  const all: Record<string, unknown> = {
    ...Object.fromEntries(s.risk_rules.map((r) => [r.id, r.value])), ...s.auto_by_market[market],
    ...Object.fromEntries(s.paper_criteria.map((c) => ['paper_' + c.id, c.value])),
  }
  return { n: (id) => (typeof all[id] === 'number' ? all[id] : undefined), index: s.regime_index[market]?.name_ko ?? '' }
}

const p0 = (v: number) => pct(v, 0).replace('+', '')
/** `text` when every value is there, else '' (a rule the server does not send leaves its sentence out) */
const say = (r: Rules | null, ids: string[], text: (...v: number[]) => string) => {
  const v = ids.map((id) => r?.n(id))
  return v.every((x): x is number => x != null) ? ' ' + text(...v) : ''
}

interface Entry { title: string; text: (r: Rules | null) => string }

export const GLOSSARY = {
  drawdown: { title: '드로다운', text: (r) => '지금까지 가장 높았던 평가액(고점)에서 얼마나 내려와 있는지를 퍼센트로 보여 줍니다. 고점에 있으면 드로다운이 없고, 마이너스가 클수록 고점에서 많이 잃은 상태입니다.'
    + say(r, ['dd_halve', 'dd_stop'], (h, s) => `이 앱은 ${p0(h)}가 되면 새로 사는 비중을 절반으로 줄이고, ${p0(s)}가 되면 전부 팔고 매매를 멈춥니다.`) },
  max_drawdown: { title: '최대 드로다운', text: (r) => '기간 중 고점에서 가장 깊게 떨어졌던 폭입니다. 그 기간에 겪은 최악의 하락이라 위험을 재는 첫 번째 지표로 씁니다.'
    + say(r, ['paper_max_drawdown'], (x) => `모의투자는 이 값이 ${p0(x)}이거나 그보다 나쁘면 통과하지 못합니다.`) },
  gross: { title: '그로스 노출', text: (r) => '가진 주식의 평가액이 전체 자산(현금 포함)에서 차지하는 비율입니다. 현금 없이 전부 주식이면 꽉 찬 상태이고, 낮을수록 현금이 많습니다.'
    + say(r, ['max_gross_pct'], (x) => `이 앱의 상한은 ${p0(x)}입니다.`) },
  exposure: { title: '노출', text: () => '자산 중 주식에 들어가 있는 비율입니다. 평균 노출은 기간 내내 이 비율의 평균이라, 낮으면 그만큼 현금으로 쉬어 간 날이 많았다는 뜻입니다.' },
  regime: { title: '국면 필터', text: (r) => `시장 전체가 하락 흐름일 때 새로 사지 않게 막는 규칙입니다. 지수${r?.index ? `(${r.index})` : ''}가 긴 기간의 평균 가격(이동평균선) 아래로 내려가면 신규 매수를 멈춥니다.`
    + say(r, ['regime_ma_days'], (d) => `이 앱은 최근 ${d}거래일 평균선을 기준으로 봅니다.`) },
  stop_loss: { title: '손절', text: (r) => '산 가격보다 정해 둔 만큼 떨어지면 더 큰 손실을 막으려고 자동으로 파는 것입니다. 손절선은 그 기준 가격입니다.'
    + say(r, ['stop_loss_pct'], (x) => `이 앱은 산 가격 대비 ${p0(x)}에서 팝니다. 다음 날 시가에 체결되므로 가격이 크게 벌어지면 더 잃을 수 있습니다.`) },
  trailing: { title: '트레일링 스톱', text: (r) => '오른 종목의 이익을 지키는 매도 규칙입니다. 보유 중 가장 높았던 가격(트레일링 고점)을 따라 기준선이 올라가고, 그 고점에서 일정 폭 떨어지면 팝니다.'
    + say(r, ['trailing_activate_pct', 'trailing_pct'], (a, w) => `이 앱은 ${p0(a)} 이상 오르면 켜지고, 고점에서 ${p0(w)} 떨어지면 팝니다.`) },
  expiry: { title: '기간 만료', text: (r) => '정해 둔 보유 기간이 끝나면 수익과 상관없이 파는 규칙입니다. 만료일은 그 날짜입니다.'
    + say(r, ['max_holding_days'], (d) => `이 앱은 산 날부터 ${d}거래일이 되면 팝니다(다음 날 시가에 체결).`) },
  protections: { title: 'Protections · 보호 장치', text: (r) => '신규 매수를 잠시 막는 안전장치 네 가지입니다. StoplossGuard(손절이 잦으면 전체 잠금) · MaxDrawdown(고점에서 크게 떨어지면 비중을 줄이거나 멈춤) · Cooldown(판 종목은 한동안 다시 안 삼) · LowProfit(손실이 이어진 종목 잠금)입니다.'
    + say(r, ['stoploss_guard_lookback_days', 'stoploss_guard_n', 'stoploss_guard_lock_days'], (l, n, d) => `예: 최근 ${l}거래일에 손절이 ${n}번 이상이면 ${d}거래일 동안 잠급니다.`) },
  cooldown: { title: 'Cooldown · 쿨다운', text: (r) => '판 종목을 곧바로 다시 사지 않도록 기다리게 하는 규칙입니다. 팔자마자 되사는 실수를 막습니다.'
    + say(r, ['cooldown_days'], (d) => `이 앱은 ${d}거래일 동안 다시 사지 않습니다.`) },
  hard_rule: { title: '하드 룰', text: () => '에이전트(AI)의 판단과 상관없이 무조건 지키는 위험 관리 규칙입니다. 비중 상한 · 손절 · 드로다운 정지 같은 것이 주문을 줄이거나 막습니다.' },
  alpha: { title: '알파', text: (r) => `같은 기간 시장 지수(벤치마크${r?.index ? ` ${r.index}` : ''})보다 얼마나 더 벌었는지입니다. 플러스면 시장보다 잘한 것, 마이너스면 못한 것입니다.` },
  benchmark: { title: '벤치마크', text: () => '성과를 비교하는 기준 지수입니다. 이 지수를 그냥 사서 들고 있었을 때보다 나았는지를 봅니다.' },
  cagr: { title: 'CAGR · 연평균 수익률', text: () => '여러 해의 수익을 매년 같은 비율로 불어났다고 쳤을 때의 한 해 수익률입니다. 기간이 다른 결과를 비교할 때 씁니다.' },
  volatility: { title: '변동성', text: () => '가격이 하루하루 얼마나 크게 오르내리는지를 연 단위로 나타낸 값입니다. 클수록 등락이 심한 종목 · 전략입니다.' },
  sector: { title: '섹터 상한', text: (r) => '섹터는 반도체 · 은행처럼 같은 업종 묶음입니다. 한 업종에 돈이 몰리면 함께 떨어질 수 있어 업종별로 담을 수 있는 비중에 상한을 둡니다.'
    + say(r, ['max_sector_pct'], (x) => `이 앱은 한 섹터에 자산의 ${p0(x)}까지 담습니다.`) },
  weight: { title: '비중', text: (r) => '한 종목이 전체 자산에서 차지하는 비율입니다. 한 종목이 망가져도 전체가 크게 다치지 않도록 상한을 둡니다.'
    + say(r, ['max_position_pct'], (x) => `이 앱은 종목당 ${p0(x)}까지 담습니다.`) },
  rating: { title: '등급', text: (r) => '에이전트가 종목마다 매기는 판단입니다. 매수(Buy) · 비중확대(Overweight) · 보유(Hold) · 비중축소(Underweight) · 매도(Sell) 다섯 단계이고, 판단을 못 내리면 검토 필요(REVIEW)로 사람에게 넘깁니다.'
    + say(r, ['underweight_sell_fraction'], (x) => (x >= 1 ? '비중축소는 보유 전량을 팝니다.' : `비중축소는 보유 수량의 ${p0(x) === '100%' ? pct(x, 1).replace('+', '') : p0(x)}를 팝니다.`)) },   // 1 = all (docs/13); just under 1 is not "100%"
  trim: { title: '축소(Trim) · 부분 매도', text: () => '보유한 종목을 전부 팔지 않고 일부만 파는 것입니다. 나머지는 처음 산 가격 · 손절선을 그대로 갖고 계속 보유합니다.' },
  momentum: { title: '모멘텀', text: () => '최근 오른 종목이 한동안 더 오르는 경향입니다. 선별은 최근 상승 흐름이 강한 종목(등락 폭을 감안해 점수화)을 앞에 둡니다.' },
  universe: { title: '유니버스', text: () => '선별을 시작하는 후보 종목 전체 목록입니다. 예를 들어 지수에 들어 있는 대형주들이고, 여기서 규칙으로 걸러 살 종목을 고릅니다.' },
  baseline: { title: '베이스라인', text: () => 'AI 없이 정해진 규칙만으로 낸 비교용 결정입니다. 에이전트가 이 단순한 방법보다 나은지 재는 기준입니다.' },
  realized: { title: '실현 손익', text: () => '이미 팔아서 확정된 이익이나 손실입니다. 판 가격과 산 가격의 차이로 계산합니다.' },
  unrealized: { title: '미실현 손익', text: () => '아직 팔지 않은 주식의 지금 가격 기준 이익이나 손실입니다. 팔기 전까지는 가격에 따라 계속 바뀝니다.' },
  avg_price: { title: '평단 · 평균 단가', text: () => '여러 번 나눠 산 주식의 평균 매수 가격입니다. 현재가가 평단보다 높으면 이익, 낮으면 손실입니다.' },
  daily_loss: { title: '일일 손실 한도', text: (r) => '하루 동안 자산이 줄어도 되는 최대 폭입니다. 이 폭에 닿으면 새로 사는 것을 멈춰 손실이 하루에 몰리지 않게 합니다.'
    // the risk limit (next session's buys) and the auto halt (that market's loop, exits kept) are one sentence when
    // they are the same value (review 1R #1: "-2% … -2%" read as two steps); both are `≤ limit` on the server, so "이상"
    + say(r, ['daily_loss_limit', 'auto_daily_loss_limit'], (x, a) => (x === a
      ? `이 앱은 하루 ${p0(x)} 이상 잃으면 그 시장의 새 매수를 멈추고 다음 거래일에 풉니다.`
      : `이 앱은 하루 ${p0(x)} 이상 잃으면 다음 거래일 새 매수를 멈추고, ${p0(a)} 이상 잃으면 그 시장의 자동 실행을 정지합니다(다음 거래일에 풀림).`)
      + ' 가진 종목의 손절 · 트레일링 매도는 계속합니다.') },
  exception_queue: { title: '예외 큐', text: () => '자동으로 처리하지 못해 사람이 봐야 하는 일만 모아 둔 목록입니다. 등급을 못 읽은 결정, 한도에 걸린 주문, 데이터 장애 같은 것이 올라옵니다.' },
  paper: { title: '모의투자', text: (r) => '실제 돈 없이 가상의 계좌로 똑같이 매매해 보는 것입니다. 기준을 다 통과해야 실제 계좌로 바꿀 수 있습니다.'
    + say(r, ['paper_min_weeks'], (w) => `최소 ${w}주 동안 운용합니다.`) },
  confidence: { title: '확신도', text: () => '에이전트가 자기 판단을 얼마나 믿는지입니다. 높을수록 근거가 분명하다고 본 것이지, 오를 확률을 약속하는 숫자는 아닙니다.' },
  thesis: { title: '논지', text: () => '이 종목을 사는 이유를 한 줄로 정리한 주장입니다. 이 주장이 깨지면 팔 이유가 됩니다.' },
  invalidation: { title: '무효화 조건', text: () => '이런 일이 생기면 논지가 틀렸다고 보고 파는, 미리 정해 둔 조건입니다. 매도 검토 때 하나씩 점검합니다.' },
  catalyst: { title: '촉매', text: () => '주가를 움직일 만한 예정된 사건입니다. 실적 발표 · 신제품 출시 같은 것이고, 날짜가 있으면 함께 적습니다.' },
  open_fill: { title: '시가 체결', text: () => '결정은 장 마감 뒤에 하고, 주문은 다음 거래일 장이 열릴 때의 첫 가격(시가)으로 체결됩니다. 그래서 결정 때 본 가격과 체결가가 다를 수 있습니다.' },
  slippage: { title: '슬리피지', text: () => '주문할 때 생각한 가격과 실제 체결된 가격의 차이입니다. 백테스트는 이 차이를 비용으로 미리 빼고 계산합니다.' },
  hit_rate: { title: '방향 적중률', text: () => '산다고 한 종목이 시장보다 더 오르고, 판다고 한 종목이 덜 오른 비율입니다. 방향을 맞힌 결정의 비율입니다.' },
  settlement: { title: '정산', text: () => '결정을 내리고 정해 둔 기간이 지난 뒤 실제 수익과 알파로 결과를 매기는 것입니다. 기간이 안 지났으면 정산 대기입니다.' },
} satisfies Record<string, Entry>

export type Term = keyof typeof GLOSSARY

/** a rule id (settings registry, screen funnel) → its term */
export const RULE_TERM: Record<string, Term> = {
  momentum_top_k: 'momentum', volatility_band: 'volatility', max_position_pct: 'weight', max_sector_pct: 'sector', max_gross_pct: 'gross',
  stop_loss_pct: 'stop_loss', trailing_activate_pct: 'trailing', trailing_pct: 'trailing', max_holding_days: 'expiry',
  daily_loss_limit: 'daily_loss', auto_daily_loss_limit: 'daily_loss', dd_halve: 'drawdown', dd_stop: 'drawdown',
  stoploss_guard_n: 'protections', cooldown_days: 'cooldown', regime_ma_days: 'regime', underweight_sell_fraction: 'rating',
}

/** each term's ⓘ on the first of these rule ids that has it (once a screen): id → term */
export function firstTerms(ids: string[]): Record<string, Term> {
  const seen = new Set<Term>()
  return Object.fromEntries(ids.flatMap((id) => {
    const k = RULE_TERM[id]
    if (!k || seen.has(k)) return []
    seen.add(k)
    return [[id, k]]
  }))
}

/** a DS title / label prop with its ⓘ: AgentSectionPanel `title` · `description` and AgentKpiCard `label` are typed
 *  string but rendered as a child. ponytail: a cast until the DS types them ReactNode — then drop `asText`. */
export const asText = (n: ReactNode) => n as unknown as string
