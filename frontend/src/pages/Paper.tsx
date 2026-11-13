import { useState } from 'react'
import { Link } from 'react-router-dom'
import { AgentButton, AgentSectionPanel } from '@bv-ds/ui'

import { api, money, pct } from '../api/client'
import type { Market, PaperProgress, TradingState } from '../api/types'
import { useApi } from '../hooks/useApi'
import { useLabels } from '../hooks/useLabels'
import { useMarket, useMarketPath } from '../hooks/useMarket'
import { useStateTick, useTradingState } from '../hooks/useTradingState'
import { Loaded } from '../components/common'
import { DrawdownSvg, ExpectedTable, PaperGo, Scorecard, VerdictBanner, ViolationList } from '../components/paper'
import { PageHeader } from '../components/parts'
import { switchState } from './paperLogic'

/** 8: the paper-trading gate (docs/02 v0.7 §11-1). The server judges the six criteria (/api/paper/progress) and the
 *  live switch takes only an eligible market; this screen shows the verdict, it never works one out. Per market. */
export default function Paper() {
  const [market] = useMarket()
  return <PaperScreen key={market} market={market} />
}

function PaperScreen({ market }: { market: Market }) {
  const to = useMarketPath()
  const exitKo = useLabels().data?.exit_reason_ko
  const other: Market = market === 'KRX' ? 'US' : 'KRX'
  const tick = useStateTick()                                          // a mode change reloads the verdict
  const gate = useApi(() => api.paperProgress(market), [market, tick])
  const otherGate = useApi(() => api.paperProgress(other), [other])
  const state = useTradingState(market).data
  const [open, setOpen] = useState(false)
  const [opened, setOpened] = useState(0)                               // a fresh form (unchecked) every time it opens
  // the modal outlives the reload its own success sets off (announceStateChange → both loads back to loading):
  // it keeps the last answers it was given, so "전환됨" stays on screen until the operator closes it
  const [held, setHeld] = useState<{ p: PaperProgress; state: TradingState } | null>(null)
  if (gate.data && state && (held?.p !== gate.data || held.state !== state)) setHeld({ p: gate.data, state })
  const day = (d: string | null | undefined) => (d ? (market === 'US' ? `${d} ET` : d) : '–')
  const sw = switchState(gate.data ?? null, state)
  const o = otherGate.data
  return (
    <>
      <PageHeader title={`모의투자 검증 · ${market === 'US' ? '미국' : '국내'}`}
        description={`실거래 전환 게이트. 같은 자동 루프를 모의로 돌려 통과 기준 6개(config.PAPER_TRADING)를 전 기간 충족해야 이 시장만 실거래로 넘어갑니다. 판정은 서버가 하고, 기대 대비 표는 참고용입니다.`}
        updated={gate.data?.as_of ? `${day(gate.data.as_of)} 장 마감 기준` : undefined} />
      <Loaded state={gate}>
        {(p) => (
          <div className="stack-gap">
            <VerdictBanner p={p} day={day} />
            <AgentSectionPanel title="기준 스코어카드" description={p.eligible ? '전부 충족 — 실거래 전환을 요청할 수 있습니다' : '전부 충족이면 이 자리에 "전환 가능"이 뜹니다'}>
              <Scorecard p={p} />
            </AgentSectionPanel>
            <div className="paper-2">
              <AgentSectionPanel title={`드로다운 (${p.market_ko} 모의 기간)`}
                description={p.drawdown ? `현재 ${pct(p.drawdown.current)} · 최저 ${pct(p.drawdown.min)} · 기준 ${pct(p.limits.max_drawdown, 0)}` : '기록 없음'}>
                <DrawdownSvg p={p} day={day} />
                <p className="muted">검은 점선은 통과 기준이자 하드 룰 반감선, 빨간 점선은 하드 룰 정지선입니다. 장 마감 평가액 기준{p.started ? `(${day(p.start)} → ${day(p.as_of)}, ${p.series.length}거래일)` : ''}.</p>
              </AgentSectionPanel>
              <AgentSectionPanel title="위반 목록" description="일일 손실 한도 위반 · 정지 발동 · REVIEW 결정 · 결정 없는 주문">
                <ViolationList p={p} to={to} day={day} />
              </AgentSectionPanel>
            </div>
            <AgentSectionPanel title="기대 대비"
              action={<span className="cross-act"><span className="ref-badge">참고 · 판정에 안 씀</span><Link to={to('/performance')}>{p.expected?.measurement_ko ?? '실측'} →</Link></span>}>
              <ExpectedTable p={p} exitKo={exitKo} day={day} />
            </AgentSectionPanel>
            <section className="cross-card lead">
              <div><b>{o?.market_ko ?? (other === 'US' ? '미국' : '국내')} 모의투자 · 별도 판정</b>
                <span className="muted">스코어카드 · 드로다운 · 위반 목록 · 전환 버튼은 시장마다 따로입니다.{' '}
                  {o ? (o.started ? `${o.market_ko} 시장은 ${o.start}${other === 'US' ? ' ET' : ''} 시작 · ${o.weeks_badge_ko} · 드로다운 ${pct(o.drawdown?.current)} (최저 ${pct(o.drawdown?.min)}) · ${o.summary_ko} · ${o.verdict_ko}`
                    : `${o.market_ko} 시장은 모의 운용 기록이 없습니다.`) : otherGate.error ? `읽기 실패: ${otherGate.error}` : '읽는 중'}</span></div>
              <Link to={`/paper?market=${other}`}>{other === 'US' ? '미국' : '국내'} 보기 →</Link>
            </section>
            <section className="cross-card">
              <div><b>실거래 자본 · 유니버스</b>
                <span className="muted">실거래 자본과 종목 목록은 설정에서 정합니다(단계 5에서 연결). 지금 주문 한도는 1건 {money(p.go_live.auto_order_max, market)} · 일일 {money(p.go_live.auto_daily_order_cap, market)}.</span></div>
              <Link to={to('/settings')}>설정에서 보기 →</Link>
            </section>
            <section className="cross-card">
              <div><b>실거래 전환{market === 'US' ? ' · 미국' : ''}</b>
                <span className="muted">관리자 · {p.market_ko} 모의 기준 6개를 전부 충족해야 켜집니다. {p.go_live.other_markets_ko.join(' · ')}에는 영향이 없습니다. 누르면 확인 모달 → POST /api/trading/mode {'{'}market: {market}, account_mode: live{'}'}</span></div>
              <span className="cross-act">
                {!sw.enabled && <span className="muted" id="go-why">{sw.why}</span>}
                <AgentButton variant="primary" disabled={!sw.enabled} aria-describedby={sw.enabled ? undefined : 'go-why'} onClick={() => { setOpened((n) => n + 1); setOpen(true) }}>실거래 전환</AgentButton>
              </span>
            </section>
          </div>
        )}
      </Loaded>
      {held && <PaperGo key={opened} p={held.p} state={held.state} market={market} open={open} onClose={() => setOpen(false)} />}
    </>
  )
}
