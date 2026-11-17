import { useState } from 'react'
import { AgentAlert, AgentButton, AgentSectionPanel, AgentSegmentedControl, AgentTextField } from '@bv-ds/ui'

import { announceStateChange, api, money, utc } from '../api/client'
import type { DataStatus, Market, Mode, OperationRow, RuleRow, RulesSettings, TradingState } from '../api/types'
import { useApi } from '../hooks/useApi'
import { MARKETS, useMarket } from '../hooks/useMarket'
import { ModeBadge, Loaded } from '../components/common'
import { ResumeButton } from '../components/risk'
import { CapitalPanel } from '../components/capital'
import { ACCOUNTS, axesMode, EXECUTIONS, modeAxes } from '../components/capitalLogic'
import { fallbackLine } from '../components/perfLogic'

const MODE_DESC: Record<Mode, string> = {
  paper: '같은 자동 루프를 실제 주문 없이 돌림. 실거래 전환 전 검증 기간.',
  auto: '결정 후 다음 거래일 시가에 자동 주문. 예외(REVIEW · 한도 초과 · 벤더 장애)는 주문하지 않고 예외 큐로.',
  manual: '결정마다 운영자가 주문 요약을 확인하고 승인해야 주문.',
}

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
            <CapitalPanel market={r.market_code} />

            <AgentSectionPanel title="규칙 레지스트리" description="선별 규칙은 실측 파라미터, 자동 실행 규칙은 config.AUTO_EXECUTION 상수(금액 한도는 시장마다 그 통화로). 수정 UI는 규칙 변경이 운영 기록에 쓰이는 단계에서 열립니다">
              <div className="matrix"><table>
                <thead><tr><th>규칙</th><th>설명</th><th className="r">국내</th><th className="r">미국</th><th>레이어</th><th>마지막 변경</th></tr></thead>
                <tbody>
                  {r.screen_rules.map((x) => (
                    <tr key={x.id}><td><b>{x.label_ko}</b> <span className="muted num">{x.id}</span></td><td className="wrap">{x.description_ko}</td>
                      {MARKETS.map((m) => <td key={m} className="r num">{paramsFor(x.id, r.screen_params_by_market[m])}</td>)}<td>{x.layer_ko}</td><td className="muted">{lastChange(x.id, r.change_history)}</td></tr>
                  ))}
                  {r.risk_rules.map((x) => (
                    <tr key={x.id}><td><b>{x.label_ko}</b> <span className="muted num">{x.id}</span></td><td className="wrap muted">하드룰 · 청산은 손절 → 트레일링 → 기간 만료 순, 가드는 포트폴리오 드로다운 기준</td>
                      <td className="r num" colSpan={2}>{fmtRule(x.id, x.value)} <span className="muted">· 두 시장 같음</span></td><td>{x.layer_ko}</td><td className="muted">{lastChange(x.id, r.change_history)}</td></tr>
                  ))}
                  {r.auto_rules.map((x) => (
                    <tr key={x.id}><td><b>{x.label_ko}</b> <span className="muted num">{x.id}</span></td><td className="wrap muted">한도를 넘으면 주문을 축소·차단하고 예외 큐에 기록</td>
                      {autoCells(x, r)}<td>{x.layer_ko}</td><td className="muted">{lastChange(x.id, r.change_history)}</td></tr>
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
              <ModelPanel market={r.market_code} mode={r.mode.mode} />
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

            <DataPanel />

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

/** 데이터 · 캐시 상태 (mockup v0.7.2 Config · States ⑤): the cache by source, each market's last refresh, and each
 *  vendor's state and whether its keys are set — never a key's value (the server sends booleans only). */
function DataPanel() {
  const status = useApi(() => api.dataStatus(), [])
  return (
    <AgentSectionPanel title="데이터 · 캐시 상태" description="시세 캐시(data/cache)는 KIS 파일이 기본, KIS 파일이 없는 종목만 Yahoo 파일. 벤더 도달은 마지막 갱신 기록으로 판단합니다(네트워크 확인 없음)">
      <Loaded state={status}>{(d) => <DataTables d={d} />}</Loaded>
    </AgentSectionPanel>
  )
}

export function FallbackLines({ d }: { d: Pick<DataStatus, 'price_fallback'> }) {
  const lines = MARKETS.flatMap((m) => { const t = fallbackLine(d, m); return t ? [{ m, t }] : [] })
  if (!lines.length) return null
  return <>{lines.map(({ m, t }) => <p key={m} className="fallback-warn" role="status"><b>{MARKET_KO[m]}</b> {t}</p>)}</>
}

function DataTables({ d }: { d: DataStatus }) {
  return (
    <div className="stack-gap" style={{ gap: '1rem' }}>
      <FallbackLines d={d} />
      <div className="matrix"><table>
        <thead><tr><th>캐시</th><th className="r">파일</th><th className="r">크기</th></tr></thead>
        <tbody>
          <tr><td>KIS <span className="muted num">*-KIS-data.csv</span></td><td className="r num">{d.cache.kis.files}</td><td className="r num">{d.cache.kis.kb.toLocaleString()} KB</td></tr>
          <tr><td>Yahoo <span className="muted num">*-YFin-data.csv</span></td><td className="r num">{d.cache.yahoo.files}</td><td className="r num">{d.cache.yahoo.kb.toLocaleString()} KB</td></tr>
        </tbody>
      </table></div>
      <div className="matrix"><table>
        <thead><tr><th>시장</th><th>가격 출처</th><th>가격 기준일</th><th>마지막 갱신</th><th className="r">심볼</th><th className="r">실패</th><th>마지막 전체 성공</th></tr></thead>
        <tbody>
          {MARKETS.map((m) => {
            const r = d.refresh[m], p = d.price_source[m]
            return (
              <tr key={m}><td>{MARKET_KO[m]}</td><td className="num">{p.price_source ?? '없음'}</td>
                <td className="num">{p.price_as_of ?? '–'}{p.price_provisional ? ' · 잠정' : ''}</td>
                <td className="num">{r ? utc(r.at) : '기록 없음'}</td><td className="r num">{r?.symbols ?? '–'}</td>
                <td className={`r num ${r && r.failed_count > 0 ? 'alert-t' : ''}`} title={r?.failed_symbols.join(' · ')}>{r?.failed_count ?? '–'}
                  {r && r.resolved_count > 0 && <span className="muted" title={r.resolved_symbols.join(' · ')}> · 이후 해결 {r.resolved_count}</span>}</td>
                <td className="num">{r?.last_success_at ? utc(r.last_success_at) : '–'}</td></tr>
            )
          })}
        </tbody>
      </table></div>
      <div className="matrix"><table>
        <thead><tr><th>데이터 소스</th><th>쓰는 곳</th><th>상태</th><th>키</th></tr></thead>
        <tbody>
          {d.vendors.map((v) => (
            <tr key={v.id}><td><b>{v.label_ko}</b></td><td className="wrap">{v.use_ko}</td>
              {/* not the price colours (red = up): a failure or a missing key in the alert text, no record muted */}
              <td className={v.state === 'reachable' ? '' : v.state === 'unknown' ? 'muted' : 'alert-t'}>{v.state === 'reachable' ? '●' : '○'} {v.state_ko}</td>
              <td className="num">{v.key_required ? Object.entries(v.keys).map(([k, set]) => `${k} ${set ? '있음' : '없음'}`).join(' · ') : '필요 없음'}</td></tr>
          ))}
        </tbody>
      </table></div>
      <p className="muted">{d.note}</p>
    </div>
  )
}

function ModePanel({ market, state, onChanged }: { market: Market; state: TradingState; onChanged: (msg?: string) => void }) {
  const axes = modeAxes(state)
  const [account, setAccount] = useState(axes.account)
  const [execution, setExecution] = useState(axes.execution)
  const mode = axesMode(account, execution)
  const [operator, setOperator] = useState('')
  const [reason, setReason] = useState('')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
          <span className="muted">{MODE_DESC[state.mode]}</span>
        </div>
        {!state.running && (
          <AgentAlert tone="warning" title={`정지됨 · ${utc(state.halted_at)} · ${state.halt_target_ko ?? state.halted_by_ko ?? state.halted_by}`}>
            사유: {state.halt_reason} · 손절·트레일링 청산 {state.keep_exits ? '계속 작동' : '중단'}.{' '}
            {state.auto_resume_label_ko ? `${state.auto_resume_label_ko} 개장 때 자동 해제됩니다.` : '다시 켜기는 관리자만 할 수 있습니다.'}
            <div style={{ marginTop: '0.8rem' }}><ResumeButton state={state} market={market}
              label={state.auto_resume_label_ko ? `${state.auto_resume_label_ko} 자동 해제 · 재가동 불필요` : undefined} /></div>
          </AgentAlert>
        )}
        <div className="toolbar mode-axes" style={{ marginBottom: 0 }}>
          <span className="muted">계좌</span>
          <AgentSegmentedControl value={account} onChange={(v) => setAccount(v as 'paper' | 'live')} options={ACCOUNTS} />
          <span className="muted">실행</span>
          {account === 'paper' ? <span className="mode-locked">자동 실행 <span className="muted">· {axes.executionLocked ?? PAPER_LOCKED}</span></span>
            : <AgentSegmentedControl value={execution} onChange={(v) => setExecution(v as 'auto' | 'manual')} options={EXECUTIONS} />}
        </div>
        {account === 'live' && state.account_mode === 'paper' && <div className="muted">모의투자 → 실거래는 모의 판정을 통과해야 서버가 받습니다(409) — 성과 화면의 실거래 전환 모달을 쓰세요.</div>}
        <div className="toolbar form-row" style={{ marginBottom: 0, alignItems: 'flex-end' }}>
          <AgentTextField label="관리자" placeholder="이름 또는 ID" value={operator} onChange={(e) => setOperator(e.target.value)} />
          <AgentTextField label="사유" placeholder="변경 이력에 남습니다" value={reason} onChange={(e) => setReason(e.target.value)} style={{ minWidth: '28rem' }} />
          {state.admin_token_required && <AgentTextField label="관리자 토큰" type="password" value={token} onChange={(e) => setToken(e.target.value)} />}
          <AgentButton variant="primary" disabled={!ready || mode === state.mode}
            onClick={() => run(async () => { await api.setMode({ operator: operator.trim(), mode, reason: reason.trim(), market }, token || undefined) })}>
            {mode === state.mode ? '현재 모드' : `${axesLabel(mode)}으로 전환`}
          </AgentButton>
        </div>
        {error && <AgentAlert tone="error" title="실패">{error}</AgentAlert>}
      </div>
    </AgentSectionPanel>
  )
}

const PAPER_LOCKED = '모의투자는 자동 실행만 — 수동 승인은 실거래 계좌에서 고릅니다'
function axesLabel(mode: Mode): string {
  const a = ACCOUNTS.find((x) => x.value === (mode === 'paper' ? 'paper' : 'live'))?.label
  const e = EXECUTIONS.find((x) => x.value === (mode === 'manual' ? 'manual' : 'auto'))?.label
  return `${a} · ${e}`
}

const PARAMS: Record<string, string[]> = {
  price_min: ['price_min'], listed_days_min: ['listed_days_min'], dollar_volume_top_n: ['dollar_volume_top_n'],
  volatility_band: ['volatility_min', 'volatility_max'], range_stability_min: ['range_stability_min'], sector_quota: ['sector_max'],
  performance_filter: ['performance_min_alpha', 'performance_n'], momentum_top_k: ['top_k'],
}
function paramsFor(rule: string, params: Record<string, number | string>): string {
  return (PARAMS[rule] ?? []).map((k) => `${k}=${fmt(params[k])}`).join(' · ') || '–'
}
/** Money limits per market, each in its own currency (docs/08 §4 Q3); a shared key spans both columns. */
function autoCells(x: RuleRow, r: RulesSettings) {
  if (!x.currency) return <td className="r num" colSpan={2}>{fmtRule(x.id, x.value)} <span className="muted">· 두 시장 같음</span></td>
  return MARKETS.map((m) => <td key={m} className="r num">{money(Number(r.auto_by_market[m][x.id]), m)}</td>)
}

/** 모델 설정: the server's config.LLM (GET /api/model), read only — nothing hardcoded here. */
function ModelPanel({ market, mode }: { market: Market; mode: Mode }) {
  const model = useApi(() => api.model(market), [market])
  return (
    <AgentSectionPanel title="모델 설정" description="config.LLM 읽기 전용 · 바꾸는 곳은 코드 설정">
      <Loaded state={model}>
        {(x) => (
          <div className="matrix"><table><tbody>
            <tr><td className="num">decider</td><td className="r num">{x.decider_ko} · {x.decider_id}</td></tr>
            <tr><td className="num">model · effort</td><td className="r num">{x.model ?? '–'} · {x.effort ?? '–'}</td></tr>
            <tr><td className="num">max_calls_per_run · timeout</td><td className="r num">{fmt(x.max_calls_per_run)} · {fmt(x.timeout)}s</td></tr>
            <tr><td className="num">시험 모드</td><td className="r num">{x.trial ? `켜짐 · 모델 결정 ${x.trial.days.length}/${x.trial.sessions ?? '∞'}거래일 · 남은 ${x.trial.remaining ?? '∞'}` : '꺼짐'}</td></tr>
            <tr><td className="num">ANTHROPIC_API_KEY</td><td className="r num">{x.keys.ANTHROPIC_API_KEY ? '설정됨' : '없음'}</td></tr>
            <tr><td className="num">output_language</td><td className="r num">{x.output_language}</td></tr>
            <tr><td className="num">approval_mode</td><td className="r num">{mode}</td></tr>
          </tbody></table></div>
        )}
      </Loaded>
      {model.data && <div className="muted" style={{ marginTop: '0.6rem' }}>{model.data.note_ko}</div>}
    </AgentSectionPanel>
  )
}
function fmt(v: unknown): string {
  return typeof v === 'number' ? v.toLocaleString('ko-KR') : v == null ? '–' : String(v)
}
/** Fractions named *_pct / *_limit / dd_* / max_drawdown / *_rate render as percentages; counts stay counts. */
function fmtRule(id: string, v: unknown): string {
  if (typeof v === 'number' && /(_pct|_limit|_fraction|^dd_|drawdown|_rate)$|^dd_|drawdown|_rate/.test(id) && !/days|_n$/.test(id)) return `${(v * 100).toFixed(v * 100 % 1 ? 1 : 0)}%`
  return fmt(v)
}
function fmtObj(o: Record<string, unknown>): string {
  return Object.entries(o).map(([k, v]) => `${k}: ${Array.isArray(v) ? `[${v.length}]` : fmt(v)}`).join(', ') || '–'
}
function lastChange(target: string, history: OperationRow[]): string {
  const h = history.find((x) => x.target === target || x.target.startsWith(target + '.'))
  return h ? `${utc(h.at)} · ${h.operator}` : '–'
}
