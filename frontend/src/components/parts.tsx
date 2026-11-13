import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { pct } from '../api/client'
import type { Positions } from '../api/types'
import { EXIT_REASONS, gaugeMagnitude, gaugeState, SOURCE_KO, type Source } from './partsLogic'

/** v0.6 common parts (docs/10 §3 단계 1), on the mockup's markup and app.css tokens. Values are the server's; these
 *  only place them (docs/02 §0: no numbers worked out on screen). */

/** One row of the mockup's limit gauge: bar, black limit line, red stop line, "value / limit · stop". The scale is
 *  1.25× the furthest line so a breach still shows past it. */
export function LimitGauge({ label, value, limit, stop, fmt = (v) => pct(v), note }: {
  label: string; value: number | null; limit: number; stop?: number; fmt?: (v: number) => string; note?: string
}) {
  const state = gaugeState(value, limit)
  const max = 1.25 * Math.max(Math.abs(limit), Math.abs(stop ?? 0))
  const at = (v: number) => `${Math.min(100, (Math.abs(v) / max) * 100)}%`
  const fill = value == null ? 0 : gaugeMagnitude(value, limit)
  const limText = stop == null ? fmt(limit) : `${fmt(limit)} · ${fmt(stop)}`
  const valText = value == null ? '–' : fmt(value)
  const aria = `${label} ${valText}, 한도 ${fmt(limit)}${stop == null ? '' : `, 정지 ${fmt(stop)}`}${state === 'over' ? ', 한도 초과' : state === 'near' ? ', 한도 근접' : ''}`
  return (
    <div className={`gauge ${state}`} title={note}>
      <span className="lbl">{label}</span>
      <div className="track" role="img" aria-label={aria}>
        <div className="fill" style={{ width: at(fill) }} />
        <div className="line" style={{ left: at(limit) }} />
        {stop != null && <div className="line stop" style={{ left: at(stop) }} />}
      </div>
      <span className="val tnum">{valText} <span className="lim">/ {limText}</span></span>
    </div>
  )
}

/** The book's limits as gauges (screens 1 · 5): drawdown with the −20% stop, the day's loss, the largest holding,
 *  gross exposure, the session's buys against the daily cap. A sector row waits for sector data (docs/10 D9). */
export function RiskGauges({ p, amt }: { p: Positions; amt: (v: number | null | undefined) => string }) {
  const frac = (v: number) => pct(v).replace('+', '')
  const top = p.positions.find((r) => r.ticker === p.max_position_ticker)
  return (
    <div className="gauges">
      <LimitGauge label="드로다운" value={p.drawdown} limit={p.limits.dd_halve} stop={p.limits.dd_stop}
        note="한도부터 신규 비중 절반, 정지선에서 전량 청산 후 시스템 정지(관리자 재가동)" />
      <LimitGauge label="일일 손실" value={p.day_return} limit={p.limits.daily_loss_limit} note="넘으면 그날 세션 정지, 다음 거래일 자동 해제" />
      <LimitGauge label="종목 최대" value={p.max_position_weight} limit={p.limits.max_position_pct} fmt={frac}
        note={top ? `가장 큰 보유: ${top.name ?? top.ticker}` : '보유 없음'} />
      <LimitGauge label="그로스 노출" value={p.gross_exposure} limit={p.limits.max_gross_pct} fmt={frac} />
      <LimitGauge label="오늘 매수 금액" value={p.today_buy_amount} limit={p.limits.auto_daily_order_cap} fmt={amt}
        note={`일일 상한은 매수만(매도는 위험 축소) · ${p.today_session ?? '다음 거래일'} 시가 체결분`} />
    </div>
  )
}

/** The rule's exit reason (not the agent's sell reason): 5 kinds, colour plus the server's label. An unknown reason
 *  keeps its id and the neutral look. */
export function ExitReasonBadge({ reason, labels }: { reason: string; labels: Record<string, string> | undefined }) {
  const kind = (EXIT_REASONS as readonly string[]).includes(reason) ? reason : 'expired'
  return <span className={`xr ${kind}`}>{labels?.[reason] ?? reason}</span>
}

/** "● 국면 · 위험 자산 매수 가능" / "신규 매수 없음", the index and its distance from the moving average (server). */
export function RegimeBadge({ on, gap, index, days }: { on: boolean | null; gap: number | null; index: string; days: number }) {
  const cls = on == null ? 'none' : on ? 'on' : 'off'
  const text = on == null ? '○ 국면 필터 꺼짐' : on ? '● 국면 · 위험 자산 매수 가능' : '● 국면 · 신규 매수 없음'
  const where = gap == null ? `${index} ${days}일선 데이터 부족` : `${index} ${days}일선 대비 ${pct(gap)}`
  return <span className={`regime ${cls}`}>{text} <span className="tnum">· {where}</span></span>
}

/** "as-of YYYY-MM-DD" at a card's top right; amber when it is not the page's reference date (docs/02 §4). */
export function AsOfStamp({ asOf, expected }: { asOf: string | null | undefined; expected?: string | null }) {
  const stale = !!asOf && !!expected && asOf !== expected
  return (
    <span className={`asof tnum ${stale ? 'stale' : ''}`} title={stale ? `기준일 ${expected}과 다른 날짜의 데이터` : undefined}>
      as-of {asOf ?? '–'}{stale ? ' · 기준일과 다름' : ''}
    </span>
  )
}

/** Where a number comes from (mockup .ex). The app shows only measured records; the other two are for later stages. */
export function SourceBadge({ source }: { source: Source }) {
  return <span className="ex">{SOURCE_KO[source]}</span>
}

/** Page head: title, one line of what the page answers, and at the right when it was read and from what. */
export function PageHeader({ title, description, updated, source = 'measured', children }: {
  title: string; description: string; updated?: string; source?: Source; children?: ReactNode
}) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      <span className="meta-r">{children}{updated && <span className="tnum">{updated}</span>}<SourceBadge source={source} /></span>
    </div>
  )
}

/** Status-bar guard chip (docs/02 §1): how many of the market's four Protections are active, to the positions screen. */
export function ProtectionsChip({ active, failed, to }: { active: number | null; failed?: boolean; to: string }) {
  const cls = active == null ? 'unknown' : active > 0 ? 'on' : 'ok'
  const text = active == null ? (failed ? 'Protections 읽기 실패' : 'Protections 확인 중')
    : active > 0 ? `Protections ${active}건 발동` : 'Protections 정상'
  return <Link className={`prot-chip ${cls}`} to={to}>{text}</Link>
}
