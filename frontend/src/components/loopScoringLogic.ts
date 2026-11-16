import type { LoopColumn, LoopScoring } from '../api/types'

/** Loop scoring (loop-scoring): the comparison's rows and the 6-A period. Every figure comes from
 *  /api/performance/loop and /api/performance/series; these only choose and word them. */

export type Period = 'week' | 'month' | 'all'
export const PERIODS: { value: Period; label: string }[] = [
  { value: 'week', label: '1주' }, { value: 'month', label: '1개월' }, { value: 'all', label: '전체' },
]

/** The series request's `from` for a period ending at the book's last day: 7 calendar days back, a month back
 *  (the same day of the previous month, clamped to its end), or none (the whole book). */
export function periodFrom(asOf: string | null, period: Period): string | undefined {
  if (!asOf || period === 'all') return undefined
  const [y, m, d] = asOf.split('-').map(Number)
  const at = period === 'week' ? new Date(Date.UTC(y, m - 1, d - 7))
    : new Date(Date.UTC(y, m - 2, Math.min(d, new Date(Date.UTC(y, m - 1, 0)).getUTCDate())))
  return at.toISOString().slice(0, 10)
}

/** The 6-A period's own series request: `{ from }` for a week or a month, none for the whole book (the page then
 *  shows the whole-book answer it already has). */
export function seriesParams(asOf: string | null, period: Period): { from: string } | undefined {
  const from = periodFrom(asOf, period)
  return from ? { from } : undefined
}

/** The 6-A book shown: the period's own answer once a period is picked, the whole book otherwise. */
export function shownSeries<T>(from: string | undefined, whole: T, cut: T): T {
  return from ? cut : whole
}

/** The rule's own records and stand-ins, which the comparison leaves out (it scores the rule on the model's days). */
export function recordedText(r: LoopScoring['rule_recorded'], noScreen: number): string | null {
  const parts = []
  if (r.decisions) parts.push(`규칙이 직접 기록한 결정 ${r.decisions}건(${r.first === r.last ? r.first : `${r.first} ~ ${r.last}`})`)
  if (r.stand_ins) parts.push(`모델 대신 규칙이 정한 결정 ${r.stand_ins}건`)
  if (noScreen) parts.push(`선별 기록이 없어 규칙과 비교하지 못한 모델 결정 ${noScreen}건`)
  return parts.length ? `${parts.join(' · ')}은 이 비교에 넣지 않았습니다.` : null
}

/** "LLM 시험 n/5일" — null when no trial is on; past its days it says so. */
export function trialBadge(trial: LoopScoring['trial']): { text: string; done: boolean } | null {
  if (!trial) return null
  const n = trial.days.length
  if (trial.sessions == null) return { text: `LLM 시험 ${n}일 (기한 없음)`, done: false }
  const done = trial.remaining === 0
  return { text: done ? `LLM 시험 종료 ${n}/${trial.sessions}일 → 규칙` : `LLM 시험 ${n}/${trial.sessions}일`, done }
}

export type Cell = { kind: 'count'; value: number } | { kind: 'pct'; value: number | null; signed: boolean } | { kind: 'text'; value: string }

/** The comparison's rows, top to bottom, each with one cell per column (null: the column is not shown). */
export function loopRows(cols: Partial<Record<'agent' | 'rule', LoopColumn>>): { label: string; note?: string; cells: (Cell | null)[] }[] {
  const order = (['agent', 'rule'] as const).map((k) => cols[k] ?? null)
  const row = (label: string, f: (c: LoopColumn) => Cell, note?: string) => ({ label, note, cells: order.map((c) => (c ? f(c) : null)) })
  const pctCell = (v: number | null, signed = true): Cell => ({ kind: 'pct', value: v, signed })
  return [
    row('기간 (결정일)', (c) => ({ kind: 'text', value: c.first ? (c.first === c.last ? c.first : `${c.first} ~ ${c.last}`) : '–' })),
    row('결정', (c) => ({ kind: 'count', value: c.decisions })),
    row('등급 분포', (c) => ({ kind: 'text', value: ratingsText(c.ratings) })),
    row('정산 완료', (c) => ({ kind: 'count', value: c.settled })),
    row('정산 대기', (c) => ({ kind: 'text', value: pendingText(c) })),
    row('매수 쪽 평균 수익', (c) => pctCell(c.mean_raw), '매수 · 비중확대, 5거래일'),
    row('매수 쪽 평균 알파', (c) => pctCell(c.mean_alpha), '벤치마크 대비'),
    row('방향 수익', (c) => pctCell(c.directional.mean_return), '매도 쪽은 부호 반대'),
    row('방향 적중률', (c) => pctCell(c.directional.hit_rate, false), '방향 × 알파 > 0'),
    row('보유 평균 알파 (참고)', (c) => pctCell(c.hold.mean_alpha), '방향을 주장하지 않음'),
    row('REVIEW (미채점)', (c) => ({ kind: 'count', value: c.review })),
  ]
}

const RATING_ORDER = ['Buy', 'Overweight', 'Hold', 'Underweight', 'Sell', 'REVIEW']
const RATING_SHORT: Record<string, string> = { Buy: '매수', Overweight: '비중확대', Hold: '보유', Underweight: '비중축소', Sell: '매도', REVIEW: 'REVIEW' }

export function ratingsText(r: Record<string, number>): string {
  const parts = RATING_ORDER.filter((k) => r[k]).map((k) => `${RATING_SHORT[k]} ${r[k]}`)
  return parts.length ? parts.join(' · ') : '–'
}

export function pendingText(c: LoopColumn): string {
  const why = c.pending_by_reason.filter((r) => r.count).map((r) => `${r.label_ko} ${r.count}`)
  return why.length ? `${c.pending} (${why.join(' · ')})` : String(c.pending)
}
