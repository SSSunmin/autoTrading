import { useState } from 'react'
import { Link } from 'react-router-dom'
import { AgentAlert, AgentButton, AgentCheckbox, AgentModal, AgentTextField } from '@bv-ds/ui'

import { announceStateChange, api, money, pct } from '../api/client'
import type { Market, PaperExpectedRow, PaperProgress, PaperViolation, TradingState } from '../api/types'
import { areaPolygon, CHART, diffText, drawdownChart, modalReady, PILL_CLASS, polyline, verdictTone } from '../pages/paperLogic'
import { ExitReasonBadge } from './parts'

/** Screen 8 parts (docs/02 v0.7 §11-1, mockup Paper · PaperUS · PaperGo · PaperGoUS). Every verdict, count and
 *  value comes from /api/paper/progress; these only lay them out. `day` writes a market date (US: "… ET"). */

type Day = (d: string | null | undefined) => string

export function VerdictBanner({ p, day }: { p: PaperProgress; day: Day }) {
  const tone = verdictTone(p)
  return (
    <div className={`verdict ${tone}`} role="status">
      <span className="verdict-t">{p.verdict_ko}</span>
      <span>{p.summary_ko}</span>
      <span className="verdict-r">
        {p.started ? `모의 시작 ${day(p.start)} · 모의 자본 ${money(p.initial_cash, p.market)} · 마지막 갱신 ${day(p.as_of)} 장 마감`
          : '모의 운용 기록 없음 · 첫 모의 주문(또는 모의투자 모드 전환)부터 기간을 셉니다'}
      </span>
    </div>
  )
}

export function Scorecard({ p }: { p: PaperProgress }) {
  return (
    <div className="matrix"><table className="paper-t">
      <thead><tr><th>기준</th><th className="r">기준값</th><th className="r">현재값</th><th>판정</th><th>남은 기간 · 메모</th></tr></thead>
      <tbody>{p.criteria.map((c) => (
        <tr key={c.id} className={c.status === 'unmet' ? 'row-unmet' : undefined}>
          <td><b>{c.label_ko}</b> <span className="muted mono">{c.id}</span></td>
          <td className="r num">{c.threshold_ko}</td>
          <td className="r num"><b>{c.value_ko}</b></td>
          <td><span className={PILL_CLASS[c.status]}>{c.status_ko}</span></td>
          <td className="wrap memo">{c.note_ko}</td>
        </tr>
      ))}</tbody>
    </table></div>
  )
}

export function DrawdownSvg({ p, day }: { p: PaperProgress; day: Day }) {
  const chart = drawdownChart(p.series, p.limits)
  const dd = p.drawdown
  const aria = dd ? `모의 기간 드로다운: 최저 ${dd.min_date ? `${day(dd.min_date)} ` : ''}${pct(dd.min)}, 현재 ${pct(dd.current)}, 기준 ${pct(p.limits.max_drawdown, 0)}`
    : '모의 기간 드로다운: 기록 없음'
  return (
    <svg className="dd-svg" viewBox={`0 0 ${CHART.width} ${CHART.height}`} role="img" aria-label={aria}>
      {chart.rules.map((r) => (
        <g key={r.value} className={`rule ${r.kind}`}>
          <line x1="0" y1={r.y} x2={CHART.width} y2={r.y} />
          <text x={CHART.width - 2} y={r.y - 3} textAnchor="end">{r.label}</text>
        </g>
      ))}
      {chart.points.length > 0 && <polygon className="dd-area" points={areaPolygon(chart.points)} />}
      {chart.points.length > 0 && <polyline className="dd-line" points={polyline(chart.points)} />}
      {chart.low && (
        <g className="dd-low">
          <circle cx={chart.low.x} cy={chart.low.y} r="4" />
          <text x={chart.low.x + (chart.low.x > CHART.width * 0.8 ? -8 : 8)} y={chart.low.y + 4} textAnchor={chart.low.x > CHART.width * 0.8 ? 'end' : 'start'}>
            {chart.low.date.slice(5)} 최저 {pct(chart.low.drawdown)}
          </text>
        </g>
      )}
      {chart.ticks.map((t) => <text key={t.x} className="tick" x={t.x + 2} y={CHART.height - 4}>{t.label}</text>)}
    </svg>
  )
}

const REF_TEXT: Record<PaperViolation['ref']['type'], string> = { decision: '결정·실행 →', positions: '가드 이력 →', order: '주문 →' }

function refPath(v: { kind: string; ref: PaperViolation['ref'] }): string {
  if (v.ref.type === 'positions') return '/positions'
  return v.kind === 'review' || v.kind === 'order_exception' ? '/decisions?tab=exceptions' : '/decisions'
}

function Row({ v, to, day }: { v: { date: string | null; kind: string; kind_ko: string; ticker: string | null; name: string | null; text: string; ref: PaperViolation['ref'] }; to: (p: string) => string; day: Day }) {
  const who = v.ticker ? (v.name ? `${v.name} ` : '') : ''
  return (
    <div className="vrow">
      <span className="num muted-2">{day(v.date)}</span>
      <span><span className={`xr ${v.kind}`}>{v.kind_ko}</span></span>
      <span className="wrap">{who}{v.ticker && <span className="tag mono">{v.ticker}</span>} {v.text}</span>
      <Link to={to(refPath(v))} aria-label={`${day(v.date)} ${v.kind_ko} · ${REF_TEXT[v.ref.type]}`}>{REF_TEXT[v.ref.type]}</Link>
    </div>
  )
}

export function ViolationList({ p, to, day }: { p: PaperProgress; to: (path: string) => string; day: Day }) {
  const n = (id: string) => p.criteria.find((c) => c.id === id)?.value ?? 0
  return (
    <>
      {p.violations.length === 0 ? <div className="v-empty">위반 0건</div>
        : p.violations.map((v, i) => <Row key={`${v.kind}-${v.date}-${i}`} v={v} to={to} day={day} />)}
      {p.started && <p className="muted v-foot">−20% 정지 발동 <b className="num">{n('max_stop_outs')}</b> · 결정 없는 주문 <b className="num">{n('max_unexplained_orders')}</b></p>}
      {p.notes.length > 0 && (
        <>
          <p className="muted v-sub">참고 · 위반은 아니지만 사람이 본 것</p>
          {p.notes.map((v, i) => <Row key={`${v.kind}-${v.date}-${i}`} v={v} to={to} day={day} />)}
        </>
      )}
    </>
  )
}

function cell(r: PaperExpectedRow, side: 'reference' | 'paper', exitKo: Record<string, string> | undefined, day: Day) {
  const v = r[side]
  if (v == null) return '–'
  if (r.id === 'exits') {
    const by = (side === 'reference' ? r.reference_by_reason : r.paper_by_reason) ?? {}
    return <>{v}건{Object.entries(by).map(([k, n]) => <span key={k}> <ExitReasonBadge reason={k} labels={exitKo} /> {n}</span>)}</>
  }
  if (r.id === 'daily_loss_guard') {
    const dates = (side === 'reference' ? r.reference_dates : r.paper_dates) ?? []
    return `${v}회${dates.length ? ` (${dates.map((d) => day(d).slice(5)).join(', ')})` : ''}`
  }
  if (r.id === 'new_buys') return `${v}건${side === 'paper' && r.paper_open ? ` (+ 미체결 ${r.paper_open})` : ''}`
  return r.unit === 'pct' ? pct(v) : `${v}`
}

export function ExpectedTable({ p, exitKo, day }: { p: PaperProgress; exitKo: Record<string, string> | undefined; day: Day }) {
  const e = p.expected
  if (!e) return <p className="muted">모의 운용 기록이 없어 비교할 기간이 없습니다.</p>
  return (
    <>
      <p className="muted">
        같은 기간({day(e.start)} → {day(e.end)}) {e.measurement_ko} {e.variant_ko} 재현값과 모의 결과. <b>참고용 비교 · 통과 판정에 쓰지 않음</b>(결정 2026-11-13, docs/10 D6).
        {e.covered_until && ` 재현 기록은 ${day(e.covered_until)}까지.`}
      </p>
      {e.error ? <p className="muted">{e.error}</p> : (
        <div className="matrix"><table className="paper-t">
          <thead><tr><th>항목</th><th>{e.measurement_ko.replace(' 실측', '')} 재현 <span className="muted">· 실측</span></th><th>모의</th><th>차이</th></tr></thead>
          <tbody>{e.rows.map((r) => (
            <tr key={r.id}><td><b>{r.label_ko}</b></td>
              <td className="r num wrap">{cell(r, 'reference', exitKo, day)}</td>
              <td className="r num wrap">{cell(r, 'paper', exitKo, day)}</td>
              <td className="r num muted-2">{diffText(r)}</td></tr>
          ))}</tbody>
        </table></div>
      )}
    </>
  )
}

/** The modal's body (mockup PaperGo / PaperGoUS): the criteria summary, the rules at the capital, what cannot be undone. */
export function GoLiveSummary({ p }: { p: PaperProgress }) {
  const g = p.go_live
  const by = Object.fromEntries(p.criteria.map((c) => [c.id, c.value_ko]))
  return (
    <div className="stack-gap" style={{ gap: '1rem' }}>
      <div className={`go-sum ${p.eligible ? 'ok' : 'no'}`}>
        <b>통과 기준 {p.counts.met}/{p.criteria.length} 충족</b> · 모의 {by.min_weeks} · 최대 DD {by.max_drawdown} · 일일 손실 위반 {by.max_daily_loss_breaches} ·
        정지 {by.max_stop_outs} · REVIEW {by.max_review_rate} · 결정 없는 주문 {by.max_unexplained_orders}
      </div>
      <div>
        <b className="go-h">자본 {money(g.capital, p.market)}에서 걸리는 규칙 <span className="muted">· {g.capital_source_ko}</span></b>
        <ul className="effects">
          <li>주문당 상한 {money(g.auto_order_max, p.market)} · 일일 주문 상한(매수) {money(g.auto_daily_order_cap, p.market)}</li>
          <li>일일 손실 {money(g.daily_loss_amount, p.market)}({pct(g.daily_loss_limit, 0)})에서 자동 정지(다음 거래일 개장 시 해제) · 긴급 정지 그대로</li>
          <li>{g.note_ko}</li>
          {g.other_markets_ko.map((o) => <li key={o}><b>{o} 모의투자는 영향 없음</b> · {o} 시장은 따로 {p.min_weeks}주 기준을 통과해야 전환됩니다</li>)}
        </ul>
      </div>
      <p className="go-warn">모드는 관리자가 다시 모의투자로 돌릴 수 있지만, 이미 체결된 실주문은 되돌릴 수 없습니다.</p>
    </div>
  )
}

/** Live-switch confirm modal: one component for both markets. Sends the account axis only; the server checks the gate again. */
export function PaperGo({ p, state, market, open, onClose }: { p: PaperProgress; state: TradingState; market: Market; open: boolean; onClose: () => void }) {
  const [ack, setAck] = useState(false)
  const [operator, setOperator] = useState('')
  const [reason, setReason] = useState('')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const ready = modalReady({ ack, reason, operator, token, tokenRequired: state.admin_token_required }) && !busy
  const close = () => { setError(null); onClose() }
  const submit = async () => {
    setBusy(true); setError(null)
    try {
      const r = await api.setAccount({ operator: operator.trim(), account_mode: 'live', reason: reason.trim(), market, confirmed: ack }, token || undefined)
      setDone(`${r.state.label_ko}${r.note ? ` · ${r.note}` : ''}`)
      announceStateChange()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <AgentModal open={open} onClose={close} title={`${p.market_ko} 시장을 실거래로 전환할까요?`}
      description="계좌 축만 바꿉니다(실행 축 자동/수동 승인은 그대로). 서버가 통과 기준을 다시 판정합니다."
      actions={done ? <AgentButton variant="primary" onClick={close}>닫기</AgentButton> : (
        <>
          <AgentButton variant="ghost" onClick={close} disabled={busy}>취소</AgentButton>
          <AgentButton variant="primary" onClick={submit} disabled={!ready}>실거래 전환</AgentButton>
        </>
      )}>
      <div className="stack-gap" style={{ gap: '1rem' }}>
        <GoLiveSummary p={p} />
        <AgentCheckbox label="실계좌로 주문이 나간다는 것을 확인했습니다" checked={ack} onChange={(e) => setAck(e.target.checked)} />
        <AgentTextField label="관리자" size="sm" placeholder="이름 또는 ID" value={operator} onChange={(e) => setOperator(e.target.value)} />
        <AgentTextField label="전환 사유" size="sm" placeholder="감사 로그에 남습니다" value={reason} onChange={(e) => setReason(e.target.value)} />
        {state.admin_token_required && <AgentTextField label="관리자 토큰" size="sm" type="password" value={token} onChange={(e) => setToken(e.target.value)} />}
        {error && <AgentAlert tone="error" title="전환 거부">{error}</AgentAlert>}
        {done && <AgentAlert tone="success" title="전환됨">{done}</AgentAlert>}
      </div>
    </AgentModal>
  )
}
