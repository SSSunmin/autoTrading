/** Market hours in KST from the market's own time zone (docs/08 §1 장 상태 · 다음 실행 칩).
 *  The browser's zone database (Intl.DateTimeFormat with timeZone) applies daylight saving; nothing here adds
 *  or subtracts an hour by hand. Mirrors backend/main.py session_window / next_run (weekdays only, no holidays). */

export interface SessionInfo {
  timezone: string          // e.g. America/New_York
  session: string           // local "HH:MM-HH:MM", regular session only
}

const KST = 'Asia/Seoul'
const RUN_AFTER_CLOSE_MIN = 5   // the executor runs after the close (backend RUN_AFTER_CLOSE)

interface Wall { y: number; m: number; d: number; hh: number; mm: number; weekday: number }

const fmtCache = new Map<string, Intl.DateTimeFormat>()
function formatter(timeZone: string): Intl.DateTimeFormat {
  let f = fmtCache.get(timeZone)
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit',
      minute: '2-digit', second: '2-digit', hourCycle: 'h23', weekday: 'short' })
    fmtCache.set(timeZone, f)
  }
  return f
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

/** The wall clock in `timeZone` at instant `t`. */
export function wallClock(t: Date, timeZone: string): Wall {
  const parts = Object.fromEntries(formatter(timeZone).formatToParts(t).map((p) => [p.type, p.value]))
  return { y: +parts.year, m: +parts.month, d: +parts.day, hh: +parts.hour, mm: +parts.minute, weekday: WEEKDAYS.indexOf(parts.weekday) }
}

/** Offset of `timeZone` from UTC at instant `t`, in ms (positive east of Greenwich). */
function offsetMs(t: Date, timeZone: string): number {
  const w = wallClock(t, timeZone)
  const sec = +(Object.fromEntries(formatter(timeZone).formatToParts(t).map((p) => [p.type, p.value])).second)
  return Date.UTC(w.y, w.m - 1, w.d, w.hh, w.mm, sec) - Math.floor(t.getTime() / 1000) * 1000
}

/** The instant a wall-clock time in `timeZone` names (two passes settle the offset across a DST change). */
export function zonedInstant(y: number, m: number, d: number, hh: number, mm: number, timeZone: string): Date {
  const guess = Date.UTC(y, m - 1, d, hh, mm)
  let t = new Date(guess - offsetMs(new Date(guess), timeZone))
  t = new Date(guess - offsetMs(t, timeZone))
  return t
}

function hm(s: string): [number, number] {
  const [h, m] = s.split(':').map(Number)
  return [h, m]
}

/** One local day's regular session as instants. */
export function sessionOn(info: SessionInfo, y: number, m: number, d: number): { open: Date; close: Date } {
  const [start, end] = info.session.split('-').map(hm)
  return { open: zonedInstant(y, m, d, start[0], start[1], info.timezone), close: zonedInstant(y, m, d, end[0], end[1], info.timezone) }
}

/** Is the market in its regular session at `now`, and when does the executor next run (close + 5 min)? */
export function marketClock(info: SessionInfo, now: Date): { open: boolean; nextRun: Date; sessionKst: string } {
  const w = wallClock(now, info.timezone)
  const today = sessionOn(info, w.y, w.m, w.d)
  const weekday = w.weekday >= 1 && w.weekday <= 5
  const open = weekday && now >= today.open && now < today.close
  let nextRun: Date | null = null
  for (let i = 0; i < 8 && nextRun == null; i++) {
    // step whole local days from today's wall date (noon avoids landing on a DST gap)
    const day = wallClock(new Date(zonedInstant(w.y, w.m, w.d, 12, 0, info.timezone).getTime() + i * 86_400_000), info.timezone)
    if (day.weekday < 1 || day.weekday > 5) continue
    const run = new Date(sessionOn(info, day.y, day.m, day.d).close.getTime() + RUN_AFTER_CLOSE_MIN * 60_000)
    if (run > now) nextRun = run
  }
  return { open, nextRun: nextRun ?? now, sessionKst: `${kst(today.open, 'time')}-${kst(today.close, 'time')}` }
}

/** An instant in KST: 'time' → "23:30", 'datetime' → "11-14 06:05". */
export function kst(t: Date, style: 'time' | 'datetime' = 'datetime'): string {
  const w = wallClock(t, KST)
  const pad = (n: number) => String(n).padStart(2, '0')
  const time = `${pad(w.hh)}:${pad(w.mm)}`
  return style === 'time' ? time : `${pad(w.m)}-${pad(w.d)} ${time}`
}
