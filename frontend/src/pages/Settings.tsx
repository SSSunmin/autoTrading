import { useState } from 'react'
import { AgentAlert, AgentButton, AgentCheckbox, AgentRadio, AgentRadioGroup, AgentSectionPanel, AgentSegmentedControl, AgentTextField } from '@bv-ds/ui'

import { announceStateChange, api, money, utc } from '../api/client'
import { resumePlan } from '../api/resumePlan'
import type { Market, Mode, OperationRow, RuleRow, RulesSettings, TradingState } from '../api/types'
import { useApi } from '../hooks/useApi'
import { MARKETS, useMarket } from '../hooks/useMarket'
import { ModeBadge, Loaded } from '../components/common'

const MODES: { value: Mode; label: string; desc: string }[] = [
  { value: 'auto', label: '자동', desc: '결정 후 다음 거래일 시가에 자동 주문. 예외(REVIEW · 한도 초과 · 벤더 장애)는 주문하지 않고 예외 큐로.' },
  { value: 'manual', label: '수동 승인', desc: '결정마다 운영자가 주문 요약을 확인하고 승인해야 주문.' },
  { value: 'paper', label: '모의투자', desc: '같은 자동 루프를 실제 주문 없이 돌림. 실거래 전환 전 검증 기간.' },
]

const MARKET_KO: Record<Market, string> = { KRX: '국내', US: '미국' }

/** 7 (v0.3): operating mode (admin, per market), the rule registry across layers, both markets' constants and limits,
 *  model settings, and the append-only change history that is both markets' operations logs. */
export default function Settings() {
  const [market] = useMarket()
  const [tick, setTick] = useState(0)
  const rules = useApi(() => api.rules(market), [market, tick])
  // the panel remounts on reload (loading in between), so the outcome line lives here
  const [notice, setNotice] = useState<string | null>(null)
  const reload = (msg?: string) => { setNotice(msg ?? null); setTick((t) => t + 1); announceStateChange() }
  return (
    <>
      <h1 className="page-title">설정·감사 로그</h1>
      <p className="page-sub">선별·하드룰·실행 규칙의 현재 파라미터와 모든 변경 기록. 조회는 전원, 긴급 정지는 운영자, 재가동·모드 변경·규칙 변경은 관리자만 할 수 있고, 변경은 지워지지 않는 기록으로 남습니다.</p>
      {notice && <div className="section"><AgentAlert tone="success" title="재가동">{notice}</AgentAlert></div>}
      <Loaded state={rules}>
        {(r) => (
          <div className="stack-gap">
            <ModePanel key={r.market_code} market={r.market_code} state={r.mode} onChanged={reload} />

            <AgentSectionPanel title="규칙 레지스트리" description="선별 규칙은 실측 파라미터, 자동 실행 규칙은 config.AUTO_EXECUTION 상수(금액 한도는 시장마다 그 통화로). 수정 UI는 규칙 변경이 운영 기록에 쓰이는 단계에서 열립니다">
              <div className="matrix"><table>
                <thead><tr><th>규칙</th><th>설명</th><th className="r">현재 파라미터</th><th>레이어</th><th>마지막 변경</th></tr></thead>
                <tbody>
                  {r.screen_rules.map((x) => (
                    <tr key={x.id}><td><b>{x.label_ko}</b> <span className="muted num">{x.id}</span></td><td className="wrap">{x.description_ko}</td>
                      <td className="r num">{paramsFor(x.id, r.screen_params)}</td><td>{x.layer_ko}</td><td className="muted">{lastChange(x.id, r.change_history)}</td></tr>
                  ))}
                  {r.risk_rules.map((x) => (
                    <tr key={x.id}><td><b>{x.label_ko}</b> <span className="muted num">{x.id}</span></td><td className="wrap muted">risk.py · 청산은 손절 → 트레일링 → 기간 만료 순, 가드는 포트폴리오 드로다운 기준</td>
                      <td className="r num">{fmtRule(x.id, x.value)}</td><td>{x.layer_ko}</td><td className="muted">{lastChange(x.id, r.change_history)}</td></tr>
                  ))}
                  {r.auto_rules.map((x) => (
                    <tr key={x.id}><td><b>{x.label_ko}</b> <span className="muted num">{x.id}</span></td><td className="wrap muted">한도를 넘으면 주문을 축소·차단하고 예외 큐에 기록</td>
                      <td className="r num">{autoValue(x, r)}</td><td>{x.layer_ko}</td><td className="muted">{lastChange(x.id, r.change_history)}</td></tr>
                  ))}
                </tbody>
              </table></div>
            </AgentSectionPanel>

            <AgentSectionPanel title="모의투자 통과 기준" description="전 기간 충족 시에만 실거래 전환(관리자). 2차 실측(성과·백테스트 화면)의 드로다운보다 보수적으로 잡았습니다">
              <div className="matrix"><table><tbody>
                {r.paper_criteria.map((c) => <tr key={c.id}><td>{c.label_ko} <span className="muted num">{c.id}</span></td><td className="r num">{fmtRule(c.id, c.value)}</td></tr>)}
              </tbody></table></div>
            </AgentSectionPanel>

            <div className="cards">
              <AgentSectionPanel title="모델 설정" description="DecisionRecord.settings와 같은 키">
                <div className="matrix"><table><tbody>
                  <tr><td className="num">decider</td><td className="r num">rule_momentum_baseline</td></tr>
                  <tr><td className="num">provider · quick_think_llm · deep_think_llm</td><td className="r muted">미연결 · LLM 에이전트 연결 시 표시</td></tr>
                  <tr><td className="num">output_language</td><td className="r num">ko</td></tr>
                  <tr><td className="num">approval_mode</td><td className="r num">{r.mode.mode}</td></tr>
                </tbody></table></div>
              </AgentSectionPanel>
            </div>
            <div className="cards">
              {MARKETS.map((m) => (
                <AgentSectionPanel key={m} title={`${MARKET_KO[m]} 시장 상수 · ${m}`} description={m === 'KRX' ? '가격제한폭·VI·거래세 (docs/01 §11-2에서 확인)' : '통화·지수·세션·수수료 (docs/07에서 확인)'}>
                  <div className="matrix"><table><tbody>
                    {Object.entries(r.markets[m]).map(([k, v]) => <tr key={k}><td className="num">{k}</td><td className="r num wrap">{v == null ? '–' : String(v)}</td></tr>)}
                  </tbody></table></div>
                </AgentSectionPanel>
              ))}
            </div>

            <AgentSectionPanel title="변경 이력" description="append-only. 두 시장의 긴급 정지·재가동·모드 변경·규칙 변경 전부(시장마다 따로 기록, 여기서 시각순으로 합침). 화면 2·3의 '마지막 변경일'이 여기를 참조합니다">
              {r.history_errors.map((e) => <AgentAlert key={e} tone="error" title="기록 파손">{e}</AgentAlert>)}
              {r.change_history.length === 0 ? <p className="muted">아직 변경 기록이 없습니다.</p> : (
                <div className="matrix"><table>
                  <thead><tr><th>시각</th><th>시장</th><th>종류</th><th>변경자</th><th>대상</th><th>이전 값</th><th>새 값</th><th>사유</th></tr></thead>
                  <tbody>{r.change_history.map((h, i) => (
                    <tr key={h.at + i}><td className="num">{utc(h.at)}</td><td>{h.market_ko}</td><td>{h.kind_ko}</td><td>{h.operator} <span className="muted">{h.role}</span></td>
                      <td className="num">{h.target}</td><td className="num muted">{fmtObj(h.before)}</td><td className="num">{fmtObj(h.after)}</td><td className="wrap">{h.reason}</td></tr>
                  ))}</tbody>
                </table></div>
              )}
            </AgentSectionPanel>
          </div>
        )}
      </Loaded>
    </>
  )
}

function ModePanel({ market, state, onChanged }: { market: Market; state: TradingState; onChanged: (msg?: string) => void }) {
  const [mode, setMode] = useState<Mode>(state.mode)
  const [scope, setScope] = useState<'all' | 'market'>('all')
  const [resetPeak, setResetPeak] = useState(true)
  // the book the server resets the peak from; read only while a −20% stop stands
  const book = useApi(() => (state.dd_stop ? api.positions(market) : Promise.resolve(null)), [market, state.dd_stop])
  // the all-markets resume reaches the other markets too: their −20% stops are offered here as well
  const markets = useApi(() => (state.running ? Promise.resolve([]) : api.markets()), [market, state.running])
  const others = scope === 'all' ? (markets.data ?? []).filter((x) => x.market !== market && x.state?.dd_stop).map((x) => x.label_ko) : []
  const plan = resumePlan(state, book.data, resetPeak, market, others)
  const [operator, setOperator] = useState('')
  const [reason, setReason] = useState('')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const current = MODES.find((m) => m.value === state.mode)

  const run = async (fn: () => Promise<string | void>) => {
    setBusy(true); setError(null)
    try { const msg = await fn(); setReason(''); onChanged(msg ?? undefined) } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  const ready = operator.trim() && reason.trim() && !busy

  return (
    <AgentSectionPanel title={`운용 모드 · ${MARKET_KO[market]}`} description="모드는 시장마다 따로. 전환은 관리자만. 변경 이력에 남습니다">
      <div className="stack-gap" style={{ gap: '1rem' }}>
        <div className="toolbar" style={{ marginBottom: 0 }}>
          <ModeBadge state={state} />
          <span className="muted">{current?.desc}</span>
        </div>
        {!state.running && (
          <AgentAlert tone="warning" title={`정지됨 · ${utc(state.halted_at)} · ${state.halt_target_ko ?? state.halted_by_ko ?? state.halted_by}`}>
            사유: {state.halt_reason} · 손절·트레일링 청산 {state.keep_exits ? '계속 작동' : '중단'}.{' '}
            {state.halt_target === 'daily_loss' ? '다음 거래일 실행기가 자동으로 다시 켭니다(관리자가 먼저 켤 수도 있습니다).' : '다시 켜기는 관리자만 할 수 있습니다.'}
          </AgentAlert>
        )}
        <AgentSegmentedControl size="sm" value={mode} onChange={(v) => setMode(v as Mode)} options={MODES.map((m) => ({ value: m.value, label: m.label }))} />
        <div className="toolbar" style={{ marginBottom: 0, alignItems: 'flex-end' }}>
          <AgentTextField label="관리자" size="sm" placeholder="이름 또는 ID" value={operator} onChange={(e) => setOperator(e.target.value)} />
          <AgentTextField label="사유" size="sm" placeholder="변경 이력에 남습니다" value={reason} onChange={(e) => setReason(e.target.value)} style={{ minWidth: '28rem' }} />
          {state.admin_token_required && <AgentTextField label="관리자 토큰" size="sm" type="password" value={token} onChange={(e) => setToken(e.target.value)} />}
          <AgentButton size="sm" variant="primary" disabled={!ready || mode === state.mode}
            onClick={() => run(async () => { await api.setMode({ operator: operator.trim(), mode, reason: reason.trim(), market }, token || undefined) })}>
            {mode === state.mode ? '현재 모드' : `${MODES.find((m) => m.value === mode)?.label} 모드로 전환`}
          </AgentButton>
          {!state.running && (
            <AgentButton size="sm" variant="secondary" disabled={!ready}
              onClick={() => run(async () => {
                const r = await api.resume({ operator: operator.trim(), reason: reason.trim(), scope: scope === 'all' ? 'all' : market, ...plan.body }, token || undefined)
                const resets = Object.entries(r.peak_resets).map(([m, x]) => `${MARKET_KO[m as Market]} 피크 재설정 ${money(x.to, m as Market)}(${x.at} 종가)`)
                return (`${r.scope_ko}: ${[...r.resumed.map((m) => `${MARKET_KO[m]} 재가동`), ...resets, ...r.skipped.map((x) => `${MARKET_KO[x.market]} 건너뜀(${x.reason})`)].join(' / ')}`)
              })}>
              {scope === 'all' ? '전체 시장 다시 켜기' : `${MARKET_KO[market]}만 다시 켜기`}
            </AgentButton>
          )}
        </div>
        {!state.running && (
          <AgentRadioGroup label="재가동 범위" value={scope} onChange={(e) => setScope(e.target.value as 'all' | 'market')}>
            <AgentRadio value="all" label="전체 시장 (정지된 시장만 다시 켭니다)" />
            <AgentRadio value="market" label={`이 시장만 (${MARKET_KO[market]}) — 다른 시장은 그대로 둡니다`} />
          </AgentRadioGroup>
        )}
        {plan.offerReset && (
          <AgentCheckbox label="피크 재설정 (−20% 드로다운 정지 뒤)" description={plan.preview ?? undefined}
            checked={resetPeak} onChange={(e) => setResetPeak(e.target.checked)} />
        )}
        {error && <AgentAlert tone="error" title="실패">{error}</AgentAlert>}
      </div>
    </AgentSectionPanel>
  )
}

const PARAMS: Record<string, string[]> = {
  price_min: ['price_min'], listed_days_min: ['listed_days_min'], dollar_volume_top_n: ['dollar_volume_top_n'],
  volatility_band: ['volatility_min', 'volatility_max'], range_stability_min: ['range_stability_min'], sector_quota: ['sector_max'],
  performance_filter: ['performance_min_alpha', 'performance_n'], momentum_top_k: ['top_k'],
}
function paramsFor(rule: string, params: Record<string, number | string>): string {
  return (PARAMS[rule] ?? []).map((k) => `${k}=${fmt(params[k])}`).join(' · ') || '–'
}
/** Money limits show both markets, each in its own currency (docs/08 §4 Q3); shared keys stay plain. */
function autoValue(x: RuleRow, r: RulesSettings): string {
  if (!x.currency) return fmtRule(x.id, x.value)
  return MARKETS.map((m) => `${MARKET_KO[m]} ${money(Number(r.auto_by_market[m][x.id]), m)}`).join(' · ')
}
function fmt(v: unknown): string {
  return typeof v === 'number' ? v.toLocaleString('ko-KR') : v == null ? '–' : String(v)
}
/** Fractions named *_pct / *_limit / dd_* / max_drawdown / *_rate render as percentages; counts stay counts. */
function fmtRule(id: string, v: unknown): string {
  if (typeof v === 'number' && /(_pct|_limit|^dd_|drawdown|_rate)$|^dd_|drawdown|_rate/.test(id) && !/days|_n$/.test(id)) return `${(v * 100).toFixed(v * 100 % 1 ? 1 : 0)}%`
  return fmt(v)
}
function fmtObj(o: Record<string, unknown>): string {
  return Object.entries(o).map(([k, v]) => `${k}: ${Array.isArray(v) ? `[${v.length}]` : fmt(v)}`).join(', ') || '–'
}
function lastChange(target: string, history: OperationRow[]): string {
  const h = history.find((x) => x.target === target || x.target.startsWith(target + '.'))
  return h ? `${utc(h.at)} · ${h.operator}` : '–'
}
