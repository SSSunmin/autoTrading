/** Screening list filter (screen 2-A). Dates are trading-day strings (YYYY-MM-DD), counted in UTC so a time zone
 *  never moves them by a day. */

/** The ``from`` date ``weeks`` weeks before ``date`` (inclusive filter: a run on that day is kept). */
export function weeksBefore(date: string, weeks: number): string {
  const d = new Date(`${date}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() - 7 * weeks)
  return d.toISOString().slice(0, 10)
}

/** The empty list's wording: no run at all yet, or none in the chosen period / universe (with the last run's date). */
export function screenEmpty(runs: number, lastAsOf: string | undefined): { title: string; description: string } {
  if (runs === 0 || !lastAsOf) {
    return { title: '오늘 선별이 아직 실행되지 않았습니다', description: '마지막 실행 없음 · 선별 스크립트가 실행되면 여기에 실행 기록이 쌓입니다.' }
  }
  return { title: '선택한 기간·유니버스에 선별 실행이 없습니다', description: `마지막 실행 ${lastAsOf} · 기간이나 유니버스를 넓혀 보세요.` }
}
