import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { AgentAlert, AgentEmptyState, AgentSectionPanel, AgentSelect, AgentTabs } from '@bv-ds/ui'

import { api } from '../api/client'
import type { Decision, ExceptionItem } from '../api/types'
import { useApi } from '../hooks/useApi'
import { useMarket } from '../hooks/useMarket'
import { useStateTick, useTradingState } from '../hooks/useTradingState'
import { Loaded, TickerCell } from '../components/common'
import { DecisionCard } from '../components/DecisionCard'
import { ExceptionButtons } from '../components/decisionParts'

/** The queue item a card acts on: its REVIEW, or one of its orders' exceptions. */
function exceptionOf(d: Decision, items: ExceptionItem[]): ExceptionItem | undefined {
  if (d.source !== 'agent') return undefined
  return items.find((x) => x.trade_date === d.trade_date && x.ticker === d.ticker
    && (x.kind === 'review' ? d.rating === 'REVIEW' : d.orders.some((o) => o.order_id === x.order_id)))
}

/** 3 (v0.3) 결정·실행: the day's decisions with their execution strip, the exception queue
 *  (REVIEW + cap exceeded + vendor failure) with its write actions (docs/10 D7), and the decisions a hard rule cut or
 *  blocked (원안 vs 최종). No approval step in auto mode. */
export default function Decisions() {
  const [params, setParams] = useSearchParams()
  const [market] = useMarket()
  const dates = useApi(() => api.decisionDates(market), [market])
  // a date picked on one market's list is that market's trading day; it does not carry over to the other market
  const [picked, setPicked] = useState<{ market: string; date: string } | null>(null)
  const day = (picked?.market === market ? picked.date : null) ?? dates.data?.[0] ?? null
  const tick = useStateTick()                              // an exception action announces itself: reload both
  const all = useApi(() => (day ? api.decisions(market, { date: day }) : Promise.resolve([])), [market, day, tick])
  const exceptions = useApi(() => (day ? api.exceptions(market, { date: day }) : Promise.resolve([])), [market, day, tick])
  const mode = useTradingState(market)
  const tab = params.get('tab') ?? 'all'
  const setTab = (t: string) => setParams((p) => {
    const q = new URLSearchParams(p)                       // keep ?market=
    if (t === 'all') q.delete('tab')
    else q.set('tab', t)
    return q
  })

  const rows = all.data ?? []
  const overrides = rows.filter((d) => d.has_intervention)
  const nExc = exceptions.data?.length ?? 0
  const items = exceptions.data ?? []
  const token = mode.data?.admin_token_required ?? false
  const card = (d: Decision) => <DecisionCard key={d.ticker + d.trade_date + d.source} d={d} exception={exceptionOf(d, items)} tokenRequired={token} />

  return (
    <>
      <h1 className="page-title">결정·실행</h1>
      <p className="page-sub">
        에이전트가 무엇을 결정했고, 룰이 무엇을 바꾸거나 막았고, 실제로 무엇이 체결됐는지.
        {mode.data?.mode === 'manual' ? ' 수동 승인 모드라 주문 전에 승인 단계가 있습니다.' : ' 자동 모드라 승인 단계는 없고, 사람이 봐야 할 것은 예외 큐에만 올라옵니다.'}
      </p>
      <div className="toolbar">
        <AgentSelect label={`결정일 (${market === 'US' ? '미국 거래일, 뉴욕 날짜' : '국내 거래일'})`} size="sm" value={day ?? ''} onChange={(e) => setPicked({ market, date: e.target.value })}
          options={(dates.data ?? []).map((d) => ({ value: d, label: d }))} />
        {mode.data && <span className="muted">{mode.data.label_ko}</span>}
      </div>
      <AgentTabs value={tab} onChange={setTab} label="결정 분류" items={[
        { id: 'all', label: `결정·실행 ${rows.length}` },
        { id: 'exceptions', label: `예외 큐 ${nExc}` },
        { id: 'override', label: `하드룰 개입 ${overrides.length}` },
      ]} />

      <div className="section">
        {tab === 'all' && (
          <Loaded state={all} empty="이 날짜의 결정이 없습니다">
            {() => <div className="cards">{rows.map(card)}</div>}
          </Loaded>
        )}

        {tab === 'exceptions' && (
          <Loaded state={exceptions} empty="예외가 없습니다"
            emptyDescription="모든 결정의 등급을 읽을 수 있었고 거부된 주문도 없습니다(처리한 예외는 빠집니다). REVIEW·자동 실행 한도 초과·벤더 장애가 생기면 여기에 올라옵니다.">
            {(items) => (
              <div className="cards">
                {items.map((x) => {
                  const d = rows.find((r) => r.ticker === x.ticker)
                  return (
                    <article key={x.kind + x.ticker + (x.ref ?? '')} className="card">
                      <div className="hd">
                        <TickerCell ticker={x.ticker} name={x.name} />
                        <span className="rating REVIEW">{x.kind_ko}</span>
                      </div>
                      <div className="exec exception">{x.summary} · {x.pending_action_ko ?? '자동 주문하지 않았습니다.'}</div>
                      {x.kind === 'review' && d && (
                        <AgentSectionPanel title="에이전트 원문 전체" description="등급 파서가 읽지 못한 텍스트. Hold로 뭉개지 않고 사람이 봅니다">
                          <pre className="raw">{d.thesis}</pre>
                        </AgentSectionPanel>
                      )}
                      {x.kind !== 'review' && x.detail && <div className="memo">{x.detail}</div>}
                      <div className="meta">
                        <ExceptionButtons item={x} tokenRequired={token} />
                        <span className="muted num" style={{ marginLeft: 'auto' }}>{x.trade_date}</span>
                      </div>
                    </article>
                  )
                })}
              </div>
            )}
          </Loaded>
        )}

        {tab === 'override' && (
          <Loaded state={all} empty="이 날짜의 결정이 없습니다">
            {() => overrides.length === 0 ? (
              <AgentEmptyState title="하드룰 개입이 없습니다" description="이 날짜에 하드 룰이 비중·수량을 줄이거나 매수를 막은 주문이 없습니다. 개입이 있으면 원안(에이전트)과 최종(룰 적용 후)이 나란히 표시됩니다." />
            ) : (
              <div className="cards">{overrides.map(card)}</div>
            )}
          </Loaded>
        )}
      </div>

      {mode.data?.mode === 'manual' && tab === 'all' && (
        <div className="section">
          <AgentAlert tone="info" title="수동 승인 흐름">
            [승인] → 주문 요약(종목·방향·수량·예상 금액·적용 룰) → 확인 → 실행 레이어. 실행 레이어가 없어 승인 API는 501을 돌려주고, 이 화면에서는 버튼을 아직 숨겼습니다.
          </AgentAlert>
        </div>
      )}
    </>
  )
}
