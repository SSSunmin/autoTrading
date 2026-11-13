import { describe, expect, it } from 'vitest'

import { screenEmpty, weeksBefore } from './screenFilter'

describe('weeksBefore', () => {
  it('counts back whole weeks from the latest run, across a month and a year', () => {
    expect(weeksBefore('2026-07-24', 4)).toBe('2026-06-26')
    expect(weeksBefore('2026-07-24', 8)).toBe('2026-05-29')
    expect(weeksBefore('2026-01-05', 1)).toBe('2025-12-29')
  })
})

describe('screenEmpty', () => {
  it('no run yet: today\'s screening has not run', () => {
    expect(screenEmpty(0, undefined).title).toBe('오늘 선별이 아직 실행되지 않았습니다')
    expect(screenEmpty(0, undefined).description).toContain('마지막 실행 없음')
  })
  it('runs exist but none in the filter: says so and names the last run', () => {
    expect(screenEmpty(8, '2026-07-24')).toEqual({ title: '선택한 기간·유니버스에 선별 실행이 없습니다',
      description: '마지막 실행 2026-07-24 · 기간이나 유니버스를 넓혀 보세요.' })
  })
})
