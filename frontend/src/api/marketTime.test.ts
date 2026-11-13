import { describe, expect, it } from 'vitest'

import { kst, marketClock, sessionOn, type SessionInfo } from './marketTime'

const US: SessionInfo = { timezone: 'America/New_York', session: '09:30-16:00' }
const KRX: SessionInfo = { timezone: 'Asia/Seoul', session: '09:00-15:30' }

// Expected values worked out by hand from the exchanges' local hours: New York is UTC-4 in summer (from 2026-03-08)
// and UTC-5 in winter, Seoul UTC+9 all year.
describe('sessionOn: the local session in KST', () => {
  const session = (info: SessionInfo, y: number, m: number, d: number) => {
    const s = sessionOn(info, y, m, d)
    return `${kst(s.open)}~${kst(s.close)}`
  }
  it.each([
    ['US summer (DST)', US, [2026, 7, 1], '07-01 22:30~07-02 05:00'],
    ['US winter', US, [2026, 12, 1], '12-01 23:30~12-02 06:00'],
    ['US the day after the 03-08 switch', US, [2026, 3, 9], '03-09 22:30~03-10 05:00'],
    ['US before the switch', US, [2026, 3, 6], '03-06 23:30~03-07 06:00'],
    ['KRX', KRX, [2026, 7, 1], '07-01 09:00~07-01 15:30'],
  ] as const)('%s', (_, info, [y, m, d], want) => {
    expect(session(info, y, m, d)).toBe(want)
  })
})

describe('marketClock: open or closed, and the next run (close + 5 min, weekdays) in KST', () => {
  it.each([
    ['KRX Friday 16:00 KST: closed, next Monday', KRX, '2026-11-13T07:00:00Z', false, '11-16 15:35', '09:00-15:30'],
    ['US Friday 16:00 KST: closed, Friday New York close', US, '2026-11-13T07:00:00Z', false, '11-14 06:05', '23:30-06:00'],
    ['US Thursday 23:45 KST: open (winter)', US, '2026-11-12T14:45:00Z', true, '11-13 06:05', '23:30-06:00'],
    ['US summer 09:00 KST: closed', US, '2026-07-01T00:00:00Z', false, '07-02 05:05', '22:30-05:00'],
    ['KRX Wednesday 10:00 KST: open', KRX, '2026-11-11T01:00:00Z', true, '11-11 15:35', '09:00-15:30'],
  ] as const)('%s', (_, info, now, open, next, sessionKst) => {
    const c = marketClock(info, new Date(now))
    expect([c.open, kst(c.nextRun), c.sessionKst]).toEqual([open, next, sessionKst])
  })
})
