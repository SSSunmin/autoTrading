import type { DataStatus, Market, SeriesRow } from '../api/types'

/** Performance 6-A charts (docs/10 §3 stage 6): the server's daily rows on a 1000-wide viewBox, as the screen-8
 *  drawdown chart draws. Every value comes from /api/performance/series; these only place them. */
export const PC = { width: 1000, height: 210, left: 56, right: 10, top: 10, bottom: 180 }

/** Round steps (1 · 2 · 2.5 · 5 × 10^k) covering [lo, hi], about `count` of them, always through 0 when it is in range. */
export function niceTicks(lo: number, hi: number, count = 4): number[] {
  if (!(hi > lo)) return [lo]
  const raw = (hi - lo) / count
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag
  const out: number[] = []
  for (let v = Math.floor(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toFixed(10) || 0)        // no −0
  return out
}

export interface Scale {
  x: (i: number) => number
  y: (v: number) => number
  ticks: number[]
  /** a date label every 5 trading days (every 10, 15, … past 50 days — about 10 at most), the last day too */
  dates: { i: number; x: number; label: string }[]
}

/** The y range covers every value given (nulls skipped) and `include` (0 for a return), widened to round ticks. */
export function scale(rows: { date: string }[], values: (number | null)[], include: number[] = [0]): Scale {
  const vs = [...values.filter((v): v is number => v != null && Number.isFinite(v)), ...include]
  let lo = Math.min(...vs), hi = Math.max(...vs)
  if (hi - lo < 1e-6) { lo -= 0.01; hi += 0.01 }
  const ticks = niceTicks(lo, hi)
  const [tlo, thi] = [Math.min(ticks[0], lo), Math.max(ticks[ticks.length - 1], hi)]
  const { width, left, right, top, bottom } = PC
  const n = rows.length
  const x = (i: number) => +(n > 1 ? left + (i / (n - 1)) * (width - left - right) : left + (width - left - right) / 2).toFixed(1)
  const y = (v: number) => +(top + ((thi - v) / (thi - tlo)) * (bottom - top)).toFixed(1)
  // the last day too, unless it would crowd the label 1–2 days before it
  const every = 5 * Math.max(1, Math.ceil(n / 50))
  const dates = rows.flatMap((r, i) => (i % every === 0 || (i === n - 1 && i % every >= Math.ceil(every * 0.6))
    ? [{ i, x: x(i), label: r.date.slice(5) }] : []))
  return { x, y, ticks, dates }
}

/** An SVG path through the values, broken where one is missing (a day the reference does not cover). */
export function linePath(values: (number | null)[], s: Pick<Scale, 'x' | 'y'>): string {
  let out = '', pen = false
  values.forEach((v, i) => {
    if (v == null) { pen = false; return }
    out += `${pen ? 'L' : 'M'}${s.x(i)},${s.y(v)}`
    pen = true
  })
  return out
}

/** The area between the values and 0 (the drawdown band hangs from the 0% line). */
export function areaPath(values: number[], s: Pick<Scale, 'x' | 'y'>): string {
  if (!values.length) return ''
  const zero = s.y(0)
  return `M${s.x(0)},${zero}${values.map((v, i) => `L${s.x(i)},${s.y(v)}`).join('')}L${s.x(values.length - 1)},${zero}Z`
}

/** The day under a pointer at viewBox x `px` (the nearest of n evenly spread days). */
export function nearestIndex(px: number, n: number): number {
  if (n <= 1) return 0
  const { width, left, right } = PC
  const i = Math.round(((px - left) / (width - left - right)) * (n - 1))
  return Math.max(0, Math.min(n - 1, i))
}

/** Keyboard reading of a chart: ← → one day, Home · End the ends; null for any other key (the page keeps it). */
export function stepIndex(i: number | null, key: string, n: number): number | null {
  if (n === 0) return null
  const at = i ?? n - 1
  if (key === 'ArrowLeft') return Math.max(0, at - 1)
  if (key === 'ArrowRight') return Math.min(n - 1, at + 1)
  if (key === 'Home') return 0
  if (key === 'End') return n - 1
  return null
}

/** The pointer left the charts: back to the day the keyboard picked while a chart has focus, else none (the last day). */
export const afterLeave = (keyboardPick: number | null, focused: boolean): number | null => (focused ? keyboardPick : null)

export const signPct = (v: number | null | undefined, digits = 1): string =>
  v == null || Number.isNaN(v) ? '–' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v * 100).toFixed(digits)}%`

/** A share of equity (exposure, cash): no sign. */
export const sharePct = (v: number | null | undefined, digits = 1): string =>
  v == null || Number.isNaN(v) ? '–' : `${(v * 100).toFixed(digits)}%`

/** A tick label: −10% · 0% · +5% (a share: 0% · 50%), no decimals unless the step needs them. */
export function tickLabel(v: number, ticks: number[], share = false): string {
  const step = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : 1
  return (share ? sharePct : signPct)(v, step * 100 < 1 ? 1 : 0)
}

/** Rising red, falling blue (Korean convention, both markets): the class of a signed value. */
export const tone = (v: number | null | undefined): 'up' | 'down' | 'flat' => (v == null || v === 0 ? 'flat' : v > 0 ? 'up' : 'down')

/** The one fallback warning line the settings data panel shows for a market's last loop run (core fix/loop-prices
 *  `RunRecord.notes`); null — nothing shown — when that run had no fallback line. */
export function fallbackLine(status: Pick<DataStatus, 'price_fallback'>, market: Market): string | null {
  const f = status.price_fallback?.[market]
  if (!f || !f.notes.length) return null
  const more = f.notes.length > 1 ? ` 외 ${f.notes.length - 1}건` : ''
  return `마지막 루프 실행(${f.trade_date} ${f.stage}): ${f.notes[0]}${more}`
}

export type { SeriesRow }
