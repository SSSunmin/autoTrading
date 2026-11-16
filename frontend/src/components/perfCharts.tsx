import { useState, type KeyboardEvent, type PointerEvent, type ReactNode, type RefObject } from 'react'

import { money } from '../api/client'
import type { Market, PerformanceSeries, RatingBar, SeriesRow, Settlement } from '../api/types'
import { useWidth } from '../hooks/useWidth'
import { AlphaBar, RatingBadge } from './common'
import { afterLeave, areaPath, linePath, nearestIndex, PC, scale, sharePct, signPct, stepIndex, tickLabel, tone, type Scale } from './perfLogic'

/** Performance 6-A · 6-B (docs/10 §3 stage 6). Every number comes from /api/performance/series and /api/performance;
 *  these draw them. A chart reads by pointer (hover) and by keyboard (focus, ← → Home End) — the same day shows in the
 *  readout line under the charts (aria-live), so neither needs a mouse. */

type Day = (d: string | null | undefined) => string
/** a day picked by the pointer, or by the keyboard (`byKey` — kept when the pointer leaves) */
type Pick = (i: number | null, byKey?: boolean) => void

function Axes({ s, w, share = false }: { s: Scale; w: number; share?: boolean }) {
  return (
    <g className="axes">
      {s.ticks.map((t) => (
        <g key={t} className={t === 0 ? 'tick zero' : 'tick'}>
          <line x1={PC.left} y1={s.y(t)} x2={w - PC.right} y2={s.y(t)} />
          <text x={PC.left - 6} y={s.y(t) + 4} textAnchor="end">{tickLabel(t, s.ticks, share)}</text>
        </g>
      ))}
      {s.dates.map((d) => <text key={d.i} className="date" x={d.x} y={PC.height - 8} textAnchor="middle">{d.label}</text>)}
    </g>
  )
}

/** The chosen day: a guide line, the values as dots and a small box (the readout line says the same in text). */
function Marker({ s, w, i, points, label }: { s: Scale; w: number; i: number | null; points: { v: number | null; cls: string }[]; label: string }) {
  if (i == null) return null
  const x = s.x(i)
  const right = x + 8 + 210 > w                                       // the 210px box would leave the chart
  return (
    <g className="marker">
      <line x1={x} y1={PC.top} x2={x} y2={PC.bottom} />
      {points.map((p) => p.v != null && <circle key={p.cls} className={p.cls} cx={x} cy={s.y(p.v)} r="4" />)}
      <g className="tip" transform={`translate(${right ? x - 8 : x + 8},${PC.top + 2})`}>
        <rect x={right ? -210 : 0} y="0" width="210" height="22" rx="4" />
        <text x={right ? -200 : 10} y="15">{label}</text>
      </g>
    </g>
  )
}

interface ChartProps {
  title: string
  /** the svg's measured width (useWidth): the viewBox is drawn in px */
  box: readonly [RefObject<SVGSVGElement | null>, number]
  rows: SeriesRow[]
  sel: number | null
  onSel: Pick
  aria: string
  children: ReactNode
}

/** One chart: focusable, pointer and keyboard pick a day. */
function Chart({ title, box: [ref, w], rows, sel, onSel, aria, children }: ChartProps) {
  const move = (e: PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    if (r.width > 0) onSel(nearestIndex(((e.clientX - r.left) / r.width) * w, rows.length, w))
  }
  const key = (e: KeyboardEvent<SVGSVGElement>) => {
    const next = stepIndex(sel, e.key, rows.length)
    if (next == null) return
    e.preventDefault()
    onSel(next, true)
  }
  return (
    <figure className="perf-chart">
      <figcaption>{title}</figcaption>
      <svg ref={ref} className="perf-svg" viewBox={`0 0 ${w} ${PC.height}`} role="img" aria-label={aria} tabIndex={0}
        onPointerMove={move} onFocus={() => onSel(sel ?? rows.length - 1, true)} onKeyDown={key}>
        {children}
      </svg>
    </figure>
  )
}

export function NavChart({ s: series, sel, onSel, day }: { s: PerformanceSeries; sel: number | null; onSel: Pick; day: Day }) {
  const rows = series.rows
  const nav = rows.map((r) => r.nav_pct), base = rows.map((r) => r.baseline_pct)
  const box = useWidth<SVGSVGElement>(PC.width)
  const sc = scale(rows, [...nav, ...base], [0], box[1])
  const last = rows[rows.length - 1]
  const at = sel == null ? null : rows[sel]
  return (
    <Chart title="수익률 — 모의 장부(시간 가중 NAV) vs 백테스트 재현" box={box} rows={rows} sel={sel} onSel={onSel}
      aria={`수익률 ${day(series.start)} ~ ${day(last.date)}: 모의 ${signPct(last.nav_pct, 2)}, 백테스트 ${signPct(last.baseline_pct, 2)}`}>
      <Axes s={sc} w={box[1]} />
      <path className="line-base" d={linePath(base, sc)} />
      <path className="line-nav" d={linePath(nav, sc)} />
      <Marker s={sc} w={box[1]} i={sel} label={at ? `${at.date.slice(5)} 모의 ${signPct(at.nav_pct, 2)} · 백테스트 ${signPct(at.baseline_pct, 2)}` : ''}
        points={[{ v: at?.nav_pct ?? null, cls: 'dot-nav' }, { v: at?.baseline_pct ?? null, cls: 'dot-base' }]} />
    </Chart>
  )
}

export function DrawdownChart({ s: series, sel, onSel, day }: { s: PerformanceSeries; sel: number | null; onSel: Pick; day: Day }) {
  const rows = series.rows
  const dd = rows.map((r) => r.drawdown_pct)
  const box = useWidth<SVGSVGElement>(PC.width)
  const sc = scale(rows, dd, [0], box[1])
  const low = Math.min(...dd)
  const at = sel == null ? null : rows[sel]
  return (
    <Chart title="드로다운 — NAV의 누적 최고 대비" box={box} rows={rows} sel={sel} onSel={onSel}
      aria={`드로다운 ${day(series.start)} ~ ${day(rows[rows.length - 1].date)}: 최저 ${signPct(low, 2)}, 현재 ${signPct(dd[dd.length - 1], 2)}`}>
      <Axes s={sc} w={box[1]} />
      <path className="dd-area" d={areaPath(dd, sc)} />
      <path className="dd-line" d={linePath(dd, sc)} />
      <Marker s={sc} w={box[1]} i={sel} label={at ? `${at.date.slice(5)} 드로다운 ${signPct(at.drawdown_pct, 2)}` : ''}
        points={[{ v: at?.drawdown_pct ?? null, cls: 'dot-dd' }]} />
    </Chart>
  )
}

export function ExposureChart({ s: series, sel, onSel, day }: { s: PerformanceSeries; sel: number | null; onSel: Pick; day: Day }) {
  const rows = series.rows
  const ex = rows.map((r) => r.exposure_pct)
  const box = useWidth<SVGSVGElement>(PC.width)
  const sc = scale(rows, ex, [0, 1], box[1])
  const n = rows.length
  const w = Math.max(1, Math.min(14, ((box[1] - PC.left - PC.right) / Math.max(n, 1)) * 0.7))
  const at = sel == null ? null : rows[sel]
  return (
    <Chart title="노출 — 보유 평가액 / 평가액" box={box} rows={rows} sel={sel} onSel={onSel}
      aria={`노출 ${day(series.start)} ~ ${day(rows[n - 1].date)}: 현재 ${sharePct(ex[n - 1], 0)}, 최고 ${sharePct(Math.max(...ex), 0)}`}>
      <Axes s={sc} w={box[1]} share />
      {ex.map((v, i) => v > 0 && (
        <rect key={rows[i].date} className={i === sel ? 'bar-ex on' : 'bar-ex'} x={+(sc.x(i) - w / 2).toFixed(1)} y={sc.y(v)}
          width={+w.toFixed(1)} height={+(sc.y(0) - sc.y(v)).toFixed(1)} />
      ))}
      <Marker s={sc} w={box[1]} i={sel} label={at ? `${at.date.slice(5)} 노출 ${sharePct(at.exposure_pct, 1)} · 현금 ${sharePct(at.cash_pct, 1)}` : ''} points={[]} />
    </Chart>
  )
}

/** The chosen day (or the last) in words — what a screen reader hears as the focus moves. */
export function Readout({ s, sel, market, day }: { s: PerformanceSeries; sel: number | null; market: Market; day: Day }) {
  const r = s.rows[sel ?? s.rows.length - 1]
  return (
    <p className="chart-readout" aria-live="polite">
      <b className="num">{day(r.date)}</b>{sel == null ? ' (마지막 날)' : ''} · 모의 <span className={`num ${tone(r.nav_pct)}`}>{signPct(r.nav_pct, 2)}</span>
      {' '}· 백테스트 <span className={`num ${tone(r.baseline_pct)}`}>{signPct(r.baseline_pct, 2)}</span>
      {' '}· 드로다운 <span className="num">{signPct(r.drawdown_pct, 2)}</span> · 노출 <span className="num">{sharePct(r.exposure_pct, 1)}</span>
      {' '}· 현금 <span className="num">{sharePct(r.cash_pct, 1)}</span> · 평가액 <span className="num">{money(r.equity, market)}</span>
    </p>
  )
}

/** The three charts on one selected day. Empty book: one line saying so (no axes drawn over nothing). */
export function SeriesCharts({ s, market, day, initial = null }: { s: PerformanceSeries; market: Market; day: Day; initial?: number | null }) {
  const [sel, setSel] = useState<number | null>(initial)
  const [kbd, setKbd] = useState<number | null>(null)
  const pick: Pick = (i, byKey) => { setSel(i); if (byKey) setKbd(i) }
  if (!s.rows.length) {
    return <p className="perf-empty">{s.start == null ? '모의 장부 기록이 없습니다 — 첫 모의 주문(또는 자본 설정)부터 일별 추이를 그립니다'
      : '고른 범위에 거래일이 없습니다'}</p>
  }
  return (
    <div className="perf-charts" onPointerLeave={(e) => setSel(afterLeave(kbd, e.currentTarget.contains(document.activeElement)))}>
      <NavChart s={s} sel={sel} onSel={pick} day={day} />
      <div className="perf-2">
        <DrawdownChart s={s} sel={sel} onSel={pick} day={day} />
        <ExposureChart s={s} sel={sel} onSel={pick} day={day} />
      </div>
      <Readout s={s} sel={sel} market={market} day={day} />
      <p className="muted legend"><i className="sw nav" /> 모의 장부 <i className="sw base" /> 백테스트 {s.reference.measurement_ko} · {s.reference.variant_ko}
        {s.reference.error ? ` — ${s.reference.error}` : s.reference.covered_until ? ` (${day(s.reference.covered_until)}까지)` : ''}. 차트를 눌러(Tab) ← → 로 날짜를 옮깁니다.</p>
    </div>
  )
}

/** 6-B: the rating bars from the scoring path (every rating, count 0 shown as such). */
export function RatingBars({ bars }: { bars: RatingBar[] }) {
  if (!bars.some((b) => b.count > 0)) return <p className="perf-empty">정산된 결정이 없습니다</p>
  const top = Math.max(...bars.map((b) => Math.abs(b.mean_alpha ?? 0)))
  const span = Math.max(1, Math.ceil(top * 100))
  return (
    <div className="matrix"><table>
      <thead><tr><th>등급</th><th className="r">n</th><th className="r">5일 평균 수익</th><th className="r">평균 알파</th><th className="r">방향 적중률</th>
        <th style={{ width: '34%' }}>평균 알파 (±{span}% 스케일)</th></tr></thead>
      <tbody>{bars.map((b) => (
        <tr key={b.rating}>
          <td><RatingBadge rating={b.rating} label={b.rating_ko} /></td>
          <td className="r num">{b.count}</td>
          <td className={`r num ${tone(b.mean_raw)}`}>{signPct(b.mean_raw, 2)}</td>
          <td className={`r num ${tone(b.mean_alpha)}`}>{signPct(b.mean_alpha, 2)}</td>
          <td className="r num">{b.count === 0 ? '–' : b.hit_rate == null ? '방향 없음' : `${(b.hit_rate * 100).toFixed(0)}%`}</td>
          <td><AlphaBar value={b.mean_alpha} scale={span} /></td>
        </tr>
      ))}</tbody>
    </table></div>
  )
}

export function SettlementTable({ st }: { st: Settlement }) {
  return (
    <div className="matrix"><table>
      <thead><tr><th>정산 실패 사유</th><th className="r">건수</th><th>종목</th></tr></thead>
      <tbody>{st.by_reason.map((r) => (
        <tr key={r.id}><td>{r.label_ko}</td><td className={`r num ${r.count > 0 ? 'alert-t' : ''}`}>{r.count}</td>
          <td className="wrap num muted">{r.tickers.length ? r.tickers.join(' · ') : '–'}</td></tr>
      ))}</tbody>
    </table></div>
  )
}
