import type { PaperExpectedRow, PaperProgress, PaperStatus, TradingState } from '../api/types'

/** Screen 8 placement only: the verdicts, counts and values are the server's (/api/paper/progress). These map them
 *  to classes and chart coordinates and decide what the screen lets a person press. */

export const PILL_CLASS: Record<PaperStatus, string> = { met: 'pill met', unmet: 'pill unmet', in_progress: 'pill wait' }

/** Banner tone: switchable, something unmet, or only waiting for the period. */
export function verdictTone(p: Pick<PaperProgress, 'eligible' | 'counts'>): 'ok' | 'no' | 'wait' {
  if (p.eligible) return 'ok'
  return p.counts.unmet > 0 ? 'no' : 'wait'
}

/** The live-switch button: on only when the server says eligible and the market is still on paper. `why` is shown
 *  next to a disabled button. */
export function switchState(p: Pick<PaperProgress, 'started' | 'eligible'> | null, state: Pick<TradingState, 'account_mode'> | null):
  { enabled: boolean; why: string } {
  if (!p || !state) return { enabled: false, why: '판정을 읽는 중' }
  if (state.account_mode === 'live') return { enabled: false, why: '이미 실거래 모드입니다' }
  if (!p.started) return { enabled: false, why: '모의 운용 기록이 없습니다' }
  if (!p.eligible) return { enabled: false, why: '통과 기준 6개를 전부 충족해야 켜집니다' }
  return { enabled: true, why: '' }
}

/** The modal's submit: the acknowledgement, a reason and an operator, and the admin token when the server asks for one. */
export function modalReady(f: { ack: boolean; reason: string; operator: string; token: string; tokenRequired: boolean }): boolean {
  return f.ack && f.reason.trim() !== '' && f.operator.trim() !== '' && (!f.tokenRequired || f.token.trim() !== '')
}

export const CHART = { width: 1000, height: 200, top: 8, bottom: 162.5 }

export interface DrawdownChart {
  points: [number, number][]
  /** horizontal rules: 0%, every 5% step, the pass line (= the halving line here) and the stop line */
  rules: { value: number; y: number; kind: 'grid' | 'limit' | 'stop'; label: string }[]
  low: { x: number; y: number; date: string; drawdown: number } | null
  ticks: { x: number; label: string }[]
}

/** Drawdown series (≤ 0) → the mockup's 1000×200 viewBox: 0% at the top, the deeper of the stop line and the low at
 *  `CHART.bottom`. x spreads the trading days evenly; a tick about every week (5 trading days). */
export function drawdownChart(series: { date: string; drawdown: number }[], limits: { max_drawdown: number; dd_halve: number; dd_stop: number }): DrawdownChart {
  const { width, top, bottom } = CHART
  const lows = series.map((r) => r.drawdown)
  const floor = Math.min(limits.dd_stop, ...lows)
  const y = (v: number) => +(top + (v / floor) * (bottom - top)).toFixed(1)
  const x = (i: number) => +(series.length > 1 ? (i / (series.length - 1)) * width : 0).toFixed(1)
  const points = series.map((r, i) => [x(i), y(r.drawdown)] as [number, number])
  const pctLabel = (v: number) => `${v < 0 ? '−' : ''}${Math.round(Math.abs(v) * 100)}%`
  const rules: DrawdownChart['rules'] = []
  for (let v = 0; v >= floor - 1e-9; v -= 0.05) {
    const value = +v.toFixed(2)
    const limit = Math.abs(value - limits.max_drawdown) < 1e-9
    const stop = Math.abs(value - limits.dd_stop) < 1e-9
    const halve = Math.abs(value - limits.dd_halve) < 1e-9
    const kind = stop ? 'stop' : limit || halve ? 'limit' : 'grid'
    const label = stop ? `${pctLabel(value)} 정지` : limit && halve ? `${pctLabel(value)} 기준 · 반감` : limit ? `${pctLabel(value)} 기준`
      : halve ? `${pctLabel(value)} 반감` : pctLabel(value)
    rules.push({ value, y: y(value), kind, label })
  }
  let low: DrawdownChart['low'] = null
  series.forEach((r, i) => { if (r.drawdown < 0 && (!low || r.drawdown < low.drawdown)) low = { x: x(i), y: y(r.drawdown), date: r.date, drawdown: r.drawdown } })
  const ticks = series.flatMap((r, i) => (i % 5 === 0 ? [{ x: x(i), label: r.date.slice(5) }] : []))
  return { points, rules, low, ticks }
}

/** "0,8 29.4,11.1 …" for a polyline; the area closes along the 0% line. */
export function polyline(points: [number, number][]): string {
  return points.map(([px, py]) => `${px},${py}`).join(' ')
}
export function areaPolygon(points: [number, number][]): string {
  if (points.length === 0) return ''
  return `${points[0][0]},${CHART.top} ${polyline(points)} ${points[points.length - 1][0]},${CHART.top}`
}

/** A reference-table difference as the screen writes it: percentage points for a rate, a signed count otherwise. */
export function diffText(row: Pick<PaperExpectedRow, 'unit' | 'diff'>): string {
  if (row.diff == null) return '–'
  if (row.unit === 'pct') return `${row.diff > 0 ? '+' : row.diff < 0 ? '−' : ''}${Math.abs(row.diff * 100).toFixed(1)}%p`
  return row.diff === 0 ? '같음' : `${row.diff > 0 ? '+' : '−'}${Math.abs(row.diff)}`
}
