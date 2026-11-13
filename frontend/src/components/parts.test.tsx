import type { ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import type { Positions } from '../api/types'
import { TickerCell } from './common'
import { AsOfStamp, ExitReasonBadge, LimitGauge, PageHeader, ProtectionsChip, RegimeBadge, RiskGauges, SourceBadge } from './parts'
import { gaugeState, tickerCellParts } from './partsLogic'

const html = (el: ReactElement, url = '/') => renderToStaticMarkup(<MemoryRouter initialEntries={[url]}>{el}</MemoryRouter>)
const EXIT_KO = { stop_loss: '손절', trailing: '트레일링', expired: '기간 만료', exit_signal: '등급 하향', force: '정지 청산' }

describe('gaugeState', () => {
  it('a drawdown toward a negative limit: ok, near from 80% of it, over at and past it', () => {
    expect(gaugeState(-0.034, -0.10)).toBe('ok')
    expect(gaugeState(-0.079, -0.10)).toBe('ok')
    expect(gaugeState(-0.08, -0.10)).toBe('near')
    expect(gaugeState(-0.10, -0.10)).toBe('over')
    expect(gaugeState(-0.25, -0.10)).toBe('over')
  })
  it('a gain never moves toward a loss limit; a weight moves toward a positive one', () => {
    expect(gaugeState(0.012, -0.02)).toBe('ok')
    expect(gaugeState(0.098, 0.10)).toBe('near')
    expect(gaugeState(0.125, 0.10)).toBe('over')
    expect(gaugeState(null, 0.10)).toBe('ok')
  })
})

describe('LimitGauge', () => {
  it('places the fill, the black limit line and the red stop line on a 1.25× scale, and names them for a reader', () => {
    // scale 0.25 (1.25 × the −20% stop): −3.4% → 13.6%, −10% → 40%, −20% → 80%
    const out = html(<LimitGauge label="드로다운" value={-0.034} limit={-0.10} stop={-0.20} />)
    expect(out).toContain('class="gauge ok"')
    expect(out).toContain('role="img" aria-label="드로다운 -3.4%, 한도 -10.0%, 정지 -20.0%"')
    expect(out).toMatch(/class="fill" style="width:13\.6\d*%"/)
    expect(out).toContain('class="line" style="left:40%"')
    expect(out).toContain('class="line stop" style="left:80%"')
    expect(out).toContain('-3.4% <span class="lim">/ -10.0% · -20.0%</span>')
  })
  it('past the limit it turns over and says so; without a stop there is no red line', () => {
    const out = html(<LimitGauge label="일일 손실" value={-0.025} limit={-0.02} />)
    expect(out).toContain('class="gauge over"')
    expect(out).toContain('aria-label="일일 손실 -2.5%, 한도 -2.0%, 한도 초과"')
    expect(out).not.toContain('line stop')
    expect(out).toContain('style="width:100%"')                       // 2.5% on a 2.5% scale
  })
  it('near the limit it says so; a missing value shows a dash and an empty bar', () => {
    expect(html(<LimitGauge label="종목 최대" value={0.09} limit={0.10} />)).toContain('aria-label="종목 최대 +9.0%, 한도 +10.0%, 한도 근접"')
    const none = html(<LimitGauge label="일일 손실" value={null} limit={-0.02} />)
    expect(none).toContain('style="width:0%"')
    expect(none).toContain('– <span class="lim">')
  })
})

describe('ExitReasonBadge', () => {
  it('maps the five rule exits to their own class and the server label', () => {
    for (const [reason, ko] of Object.entries(EXIT_KO)) {
      expect(html(<ExitReasonBadge reason={reason} labels={EXIT_KO} />)).toBe(`<span class="xr ${reason}">${ko}</span>`)
    }
  })
  it('an unknown reason keeps its id in the neutral look; before the labels load the id shows', () => {
    expect(html(<ExitReasonBadge reason="halt_sweep" labels={EXIT_KO} />)).toBe('<span class="xr expired">halt_sweep</span>')
    expect(html(<ExitReasonBadge reason="trailing" labels={undefined} />)).toBe('<span class="xr trailing">trailing</span>')
  })
})

describe('RegimeBadge', () => {
  it('on: buying allowed, with the index and its distance from the average', () => {
    expect(html(<RegimeBadge on gap={0.181} index="KOSPI" days={200} />))
      .toBe('<span class="regime on">● 국면 · 위험 자산 매수 가능 <span class="tnum">· KOSPI 200일선 대비 +18.1%</span></span>')
  })
  it('off: no new buys, in dollars-market words too; without enough bars it says so', () => {
    expect(html(<RegimeBadge on={false} gap={-0.032} index="S&P 500" days={200} />))
      .toBe('<span class="regime off">● 국면 · 신규 매수 없음 <span class="tnum">· S&amp;P 500 200일선 대비 -3.2%</span></span>')
    expect(html(<RegimeBadge on={null} gap={null} index="KOSPI" days={200} />)).toContain('class="regime none">○ 국면 필터 꺼짐')
    expect(html(<RegimeBadge on gap={null} index="KOSPI" days={200} />)).toContain('KOSPI 200일선 데이터 부족')
  })
})

describe('AsOfStamp', () => {
  it('plain on the reference date, amber and labelled on another', () => {
    expect(html(<AsOfStamp asOf="2026-07-24" expected="2026-07-24" />)).toBe('<span class="asof tnum ">as-of 2026-07-24</span>')
    const stale = html(<AsOfStamp asOf="2026-07-17" expected="2026-07-24" />)
    expect(stale).toContain('class="asof tnum stale"')
    expect(stale).toContain('as-of 2026-07-17 · 기준일과 다름')
  })
  it('without a reference date it is never amber', () => {
    expect(html(<AsOfStamp asOf="2026-07-17" />)).not.toContain('stale')
    expect(html(<AsOfStamp asOf={null} expected="2026-07-24" />)).toContain('as-of –')
  })
})

describe('SourceBadge and PageHeader', () => {
  it('three sources in Korean', () => {
    expect(html(<SourceBadge source="example" />)).toBe('<span class="ex">예시</span>')
    expect(html(<SourceBadge source="measured" />)).toBe('<span class="ex">실측</span>')
    expect(html(<SourceBadge source="proposed" />)).toBe('<span class="ex">제안</span>')
  })
  it('the page head carries the title, the line and, at the right, when it was read and the source', () => {
    const out = html(<PageHeader title="오늘 요약" description="무엇을 보는지" updated="2026-07-24 종가 기준" />)
    expect(out).toContain('<h1>오늘 요약</h1><p>무엇을 보는지</p>')
    expect(out).toContain('<span class="tnum">2026-07-24 종가 기준</span><span class="ex">실측</span>')
  })
})

describe('instrument cell', () => {
  it('name over the ticker tag, the ticker as each market writes it', () => {
    expect(tickerCellParts('000660.KS', 'SK하이닉스', 'KRX')).toEqual({ primary: 'SK하이닉스', tag: '000660.KS' })
    expect(tickerCellParts('AAPL', '애플', 'US')).toEqual({ primary: '애플', tag: 'AAPL' })
  })
  it('without a name: a US ticker stands alone, a KRX code says the name is missing', () => {
    expect(tickerCellParts('AAPL', null, 'US')).toEqual({ primary: 'AAPL', tag: null })
    expect(tickerCellParts('000660.KS', null, 'KRX')).toEqual({ primary: '회사명 미확인', tag: '000660.KS' })
  })
  it('the cell reads the market from the URL when not given', () => {
    expect(html(<TickerCell ticker="AAPL" />, '/positions?market=US')).toBe('<span class="tk "><span class="tk-n">AAPL</span></span>')
    expect(html(<TickerCell ticker="000660.KS" />, '/positions')).toContain('<span class="tag mono">000660.KS</span>')
  })
})

describe('ProtectionsChip', () => {
  it('active guards: red, counted, a link to the positions screen of that market', () => {
    expect(html(<ProtectionsChip active={2} to="/positions?market=US" />))
      .toBe('<a class="prot-chip on" href="/positions?market=US" data-discover="true">Protections 2건 발동</a>')
  })
  it('none active, loading, and a failed read are three different words', () => {
    expect(html(<ProtectionsChip active={0} to="/positions" />)).toContain('class="prot-chip ok" href="/positions" data-discover="true">Protections 정상')
    expect(html(<ProtectionsChip active={null} to="/positions" />)).toContain('Protections 확인 중')
    expect(html(<ProtectionsChip active={null} failed to="/positions" />)).toContain('class="prot-chip unknown" href="/positions" data-discover="true">Protections 읽기 실패')
  })
})

describe('RiskGauges', () => {
  const book = {
    drawdown: -0.12, day_return: -0.025, max_position_weight: 0.05, max_position_ticker: 'A.KS', gross_exposure: 0.3,
    today_buy_amount: 52_000_000, today_session: '2026-07-27', positions: [{ ticker: 'A.KS', name: '에이' }],
    limits: { dd_halve: -0.10, dd_stop: -0.20, daily_loss_limit: -0.02, max_position_pct: 0.10, max_gross_pct: 1.0, auto_daily_order_cap: 100_000_000 },
  } as unknown as Positions
  it('five rows from the book, each in its own state, money in the book\'s format', () => {
    const out = renderToStaticMarkup(<RiskGauges p={book} amt={(v) => `₩${v}`} />)
    expect(out.match(/class="gauge /g)).toHaveLength(5)
    expect(out).toContain('aria-label="드로다운 -12.0%, 한도 -10.0%, 정지 -20.0%, 한도 초과"')
    expect(out).toContain('aria-label="일일 손실 -2.5%, 한도 -2.0%, 한도 초과"')
    expect(out).toContain('aria-label="종목 최대 5.0%, 한도 10.0%"')
    expect(out).toContain('aria-label="오늘 매수 금액 ₩52000000, 한도 ₩100000000"')
    expect(out).toContain('title="가장 큰 보유: 에이"')
  })
})
