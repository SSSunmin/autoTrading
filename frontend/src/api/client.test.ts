import { describe, expect, it } from 'vitest'

import { money, stockName } from './client'

describe('money: each market in its own currency, never converted', () => {
  it.each([
    [1234567.4, 'KRX', '₩1,234,567'],
    [99052.88368, 'US', '$99,052.88'],
    [20000, 'US', '$20,000.00'],
    [-944.386, 'US', '-$944.39'],
    [0, 'KRX', '₩0'],
  ] as const)('%s %s → %s', (v, market, want) => {
    expect(money(v, market)).toBe(want)
  })
  it('a missing amount is a dash, not zero', () => {
    expect([money(null, 'US'), money(undefined, 'KRX'), money(Number.NaN, 'US')]).toEqual(['–', '–', '–'])
  })
})

describe('stockName: US "한글명 TICKER", KRX the Korean name only (docs/08 Q4)', () => {
  it('names each market its own way', () => {
    expect(stockName('AAPL', '애플', 'US')).toBe('애플 AAPL')
    expect(stockName('005930.KS', '삼성전자', 'KRX')).toBe('삼성전자')
  })
  it('falls back to the ticker without a name', () => {
    expect([stockName('XYZ', null, 'US'), stockName('000000.KS', undefined, 'KRX')]).toEqual(['XYZ', '000000.KS'])
  })
})
