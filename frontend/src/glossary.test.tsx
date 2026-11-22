/// <reference types="node" />
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { Tooltip } from '@bv-ds/ui'
import { describe, expect, it, vi } from 'vitest'

import type { RulesSettings } from './api/types'
import { Tip } from './components/Tip'
import { firstTerms, GLOSSARY, RULE_TERM, rulesOf, type Term } from './glossary'

/** glossary-tips (2026-11-19): every term the screens explain, in words a beginner reads, with the rule values from the
 *  server; an entry no screen uses, an English-only title and a rule value written into the text turn this red. */

const SRC = fileURLToPath(new URL('.', import.meta.url))
const sources = (readdirSync(SRC, { recursive: true }) as string[]).map((f) => relative(SRC, join(SRC, f)).replaceAll('\\', '/'))
  .filter((f) => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f) && f !== 'glossary.ts').map((f) => readFileSync(join(SRC, f), 'utf8'))
const keys = Object.keys(GLOSSARY) as Term[]
const hangul = (s: string) => (s.match(/[가-힣]/g) ?? []).length

/** a RulesSettings with values no default has, so a number in the text can only have come from here */
const settings = (): RulesSettings => ({
  risk_rules: [['dd_halve', -0.13], ['dd_stop', -0.27], ['max_gross_pct', 0.93], ['stop_loss_pct', -0.07], ['trailing_activate_pct', 0.11],
    ['trailing_pct', 0.06], ['max_holding_days', 17], ['max_sector_pct', 0.23], ['max_position_pct', 0.09], ['daily_loss_limit', -0.03],
    ['regime_ma_days', 150], ['cooldown_days', 4], ['stoploss_guard_n', 2], ['stoploss_guard_lookback_days', 9], ['stoploss_guard_lock_days', 6]]
    .map(([id, value]) => ({ id: id as string, value, label_ko: '', layer: 'screen' as const, layer_ko: '' })),
  auto_by_market: { KRX: { auto_daily_loss_limit: -0.04, underweight_sell_fraction: 0.4 }, US: { auto_daily_loss_limit: -0.05 } },
  paper_criteria: [{ id: 'max_drawdown', label_ko: '', value: -0.16 }, { id: 'min_weeks', label_ko: '', value: 7 }],
  regime_index: { KRX: { benchmark: '^KS200', name_ko: '코스피200' }, US: { benchmark: '^GSPC', name_ko: 'S&P 500' } },
} as unknown as RulesSettings)

describe('glossary — each entry used, readable, its numbers from the server', () => {
  it('finds the screens it checks', () => {
    expect(sources.length).toBeGreaterThan(30)
    expect(keys.length).toBeGreaterThan(30)
  })

  it('has no entry that no screen places (a Tip k, a label map, or a rule id the settings and screen tables show)', () => {
    // only the shapes that place a ⓘ: <Tip k="…" />, a KPI's `tip: '…'`, a gauge's `tip('…')`, a `const TERM` label map
    const maps = sources.flatMap((s) => [...s.matchAll(/const TERM[^=]*=\s*\{([^}]*)\}/g)].map((m) => m[1]))
    const placed = new Set([
      ...sources.flatMap((s) => [...s.matchAll(/<Tip k="(\w+)"|\btip: '(\w+)'|\btip\('(\w+)'\)/g)].map((m) => m[1] ?? m[2] ?? m[3])),
      ...maps.flatMap((m) => [...m.matchAll(/:\s*'(\w+)'/g)].map((x) => x[1])), ...Object.values(RULE_TERM),
    ])
    expect(placed.size).toBeGreaterThan(30)
    expect(keys.filter((k) => !placed.has(k))).toEqual([])
  })

  it('gives every English word a Korean one beside it in the title, and explains in Korean', () => {
    for (const k of keys) {
      const g = GLOSSARY[k]
      expect(hangul(g.title), `${k} title`).toBeGreaterThan(0)
      expect(hangul(g.text(null)), `${k} text`).toBeGreaterThan(30)
    }
  })

  it('writes no number of its own: without the rules every text is the general meaning, digit-free', () => {
    expect(keys.filter((k) => /\d/.test(GLOSSARY[k].title + GLOSSARY[k].text(null)))).toEqual([])
  })

  it('takes each rule value from /api/settings/rules, for the market on screen', () => {
    const r = rulesOf(settings(), 'KRX')
    const t = (k: Term) => GLOSSARY[k].text(r)
    expect(t('drawdown')).toContain('이 앱은 -13%가 되면 새로 사는 비중을 절반으로 줄이고, -27%가 되면 전부 팔고 매매를 멈춥니다.')
    expect(t('max_drawdown')).toContain('-16%이거나 그보다 나쁘면')      // the server: dd > -16% passes, so -16% fails
    expect(t('gross')).toContain('93%')
    expect(t('stop_loss')).toContain('산 가격 대비 -7%에서')
    expect(t('trailing')).toContain('11% 이상 오르면 켜지고, 고점에서 6% 떨어지면')
    expect(t('expiry')).toContain('산 날부터 17거래일이 되면 팝니다(다음 날 시가에 체결).')   // `>=` that day, filled next open
    expect(t('sector')).toContain('23%')
    expect(t('weight')).toContain('9%')
    expect(t('daily_loss')).toContain('하루 -3% 이상 잃으면 다음 거래일 새 매수를 멈추고, -4% 이상 잃으면 그 시장의 자동 실행을 정지합니다(다음 거래일에 풀림). 가진 종목의 손절 · 트레일링 매도는 계속합니다.')
    expect(t('regime')).toContain('지수(코스피200)')
    expect(t('regime')).toContain('최근 150거래일')
    expect(t('cooldown')).toContain('4거래일')
    expect(t('protections')).toContain('최근 9거래일에 손절이 2번 이상이면 6거래일')
    expect(t('protections')).toMatch(/StoplossGuard.*MaxDrawdown.*Cooldown.*LowProfit/)
    expect(t('rating')).toContain('보유 수량의 40%')
    expect(t('paper')).toContain('최소 7주')
    expect(t('alpha')).toContain('벤치마크 코스피200')
    const us = rulesOf(settings(), 'US')
    expect(GLOSSARY.daily_loss.text(us)).toContain('-5% 이상 잃으면 그 시장의 자동 실행')
    // the same value twice (the defaults: -2% / -2%) is one step, not two (review 1R #1)
    const same = settings()
    same.auto_by_market.KRX.auto_daily_loss_limit = -0.03
    const one = GLOSSARY.daily_loss.text(rulesOf(same, 'KRX'))
    expect(one).toContain('이 앱은 하루 -3% 이상 잃으면 그 시장의 새 매수를 멈추고 다음 거래일에 풉니다. 가진 종목의 손절 · 트레일링 매도는 계속합니다.')
    expect(one.match(/-3%/g)).toHaveLength(1)
    // the boundary is in: the server blocks at `≤ limit` (risk.daily_loss_blocks, execution._auto_halt) — tip-daily-loss
    expect(keys.filter((k) => /보다 많이 잃|보다 나빠지/.test(GLOSSARY[k].text(r) + GLOSSARY[k].text(null)))).toEqual([])
    expect(GLOSSARY.regime.text(us)).toContain('지수(S&P 500)')
    expect(GLOSSARY.rating.text(us)).not.toContain('비중축소는 보유')     // a value the market does not have: its sentence left out
  })

  it('maps only rule ids the server has (a renamed id would drop its ⓘ silently — review 1R #4)', () => {
    const core = ['screening.py', 'risk.py', 'config.py'].map((f) => readFileSync(join(SRC, '..', '..', 'agent_trading_core', f), 'utf8')).join('\n')
    // a quoted id (the screen rules, AUTO_EXECUTION keys) or a dataclass field (RiskConfig)
    const has = (id: string) => new RegExp(`["']${id}["']|^\\s+${id}\\s*:`, 'm').test(core)
    expect(Object.keys(RULE_TERM).filter((id) => !has(id))).toEqual([])
    expect(has('momentum_topk')).toBe(false)
  })

  it('puts a rule-table ⓘ on the first row of each term only (once a screen)', () => {
    expect(firstTerms(['price_min', 'trailing_activate_pct', 'trailing_pct', 'dd_halve', 'dd_stop', 'momentum_top_k']))
      .toEqual({ trailing_activate_pct: 'trailing', dd_halve: 'drawdown', momentum_top_k: 'momentum' })
  })
})

describe('Tip — the ⓘ button', () => {
  const html = (el: ReactElement) => renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>)
  const tooltip = (k: Term) => Tip({ k }) as ReactElement<{ variant: string; children: ReactElement<{ onClick: (e: unknown) => void }> }>

  it('is a button named "‹term› 설명", the icon hidden from screen readers, nothing open until hover or focus', () => {
    const out = html(<Tip k="drawdown" />)
    expect(out).toMatch(/^<span class="_root[^"]*"><button type="button" class="tip" aria-label="드로다운 설명"><svg [^>]*aria-hidden="true"/)
    expect(out).not.toContain('role="tooltip"')
    expect(html(<Tip k="cagr" />)).toContain('aria-label="CAGR · 연평균 수익률 설명"')
  })

  it('keeps the last word of ``term`` and the ⓘ on one line (an ⓘ fell to a line of its own in a wrapped title)', () => {
    expect(html(<Tip k="regime" term="● 국면 · 신규 매수 없음" />)).toMatch(/^● 국면 · 신규 매수 <span class="tip-term">없음<span class="_root[^"]*"><button/)
    expect(html(<Tip k="stop_loss" term="손절선" />)).toMatch(/^<span class="tip-term">손절선<span class="_root/)
  })

  it("is the DS white tooltip's trigger (it opens on focus, Esc closes it — the DS's own handlers)", () => {
    const el = tooltip('gross')
    expect(el.type).toBe(Tooltip)
    expect(el.props.variant).toBe('white')
    expect(el.props.children.type).toBe('button')
  })

  it('re-focuses on a click or Enter (blur, then focus: it opens again after Esc, and a tap opens it), the row click kept out', () => {
    const button = tooltip('gross').props.children
    const calls: string[] = []
    const e = { stopPropagation: vi.fn(() => calls.push('stop')),
      currentTarget: { blur: () => calls.push('blur'), focus: () => calls.push('focus') } }
    button.props.onClick(e)
    expect(calls).toEqual(['stop', 'blur', 'focus'])
  })
})
