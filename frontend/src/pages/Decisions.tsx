import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { AgentAlert, AgentEmptyState, AgentSelect, AgentTabs } from '@bv-ds/ui'

import { api } from '../api/client'
import type { Decision, ExceptionItem } from '../api/types'
import { useApi } from '../hooks/useApi'
import { useMarket } from '../hooks/useMarket'
import { useStateTick, useTradingState } from '../hooks/useTradingState'
import { Loaded, TickerCell } from '../components/common'
import { DecisionCard } from '../components/DecisionCard'
import { Tip } from '../components/Tip'
import { ExceptionButtons } from '../components/decisionParts'
import { ReviewBadges } from '../components/ticker'

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
  // ?date= (a link from another card, e.g. a rerun's answer) when that day is on this market's list
  const linked = params.get('date')
  const day = (picked?.market === market ? picked.date : null) ?? (linked && dates.data?.includes(linked) ? linked : null)
    ?? dates.data?.[0] ?? null
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
  const reviews = rows.filter((d) => d.review)
  const nExc = exceptions.data?.length ?? 0
  const items = exceptions.data ?? []
  const token = mode.data?.admin_token_required ?? false
  // the first card of a tab carries the terms' ⓘ (glossary-tips: once a screen)
  const card = (d: Decision, i: number, showPlan = false) => (
    <DecisionCard key={d.ticker + d.trade_date + d.source} d={d} exception={exceptionOf(d, items)} tokenRequired={token} showPlan={showPlan} tips={i === 0} />
  )
  // the queue item's own card: the decision it belongs to (its REVIEW, or the decision whose order it is)
  const decisionOf = (x: ExceptionItem) => rows.find((d) => exceptionOf(d, [x]) === x)
  const count = (n: number, alert = false) => <span className={alert && n > 0 ? 'nav-badge' : 'nav-count'}>{n}</span>

  return (
    <>
      <h1 className="page-title">결정·실행</h1>
      <p className="page-sub">
        에이전트가 무엇을 결정했고, 하드 룰<Tip k="hard_rule" />이 무엇을 바꾸거나 막았고, 실제로 무엇이 체결됐는지.
        {mode.data?.mode === 'manual' ? ' 수동 승인 모드라 주문 전에 승인 단계가 있습니다. 사람이 봐야 할 예외는 예외 큐' : ' 자동 모드라 승인 단계는 없고, 사람이 봐야 할 것은 예외 큐'}
        <Tip k="exception_queue" />{mode.data?.mode === 'manual' ? '에 올라옵니다.' : '에만 올라옵니다.'}
      </p>
      <div className="toolbar">
        <AgentSelect label={`결정일 (${market === 'US' ? '미국 거래일, 뉴욕 날짜' : '국내 거래일'})`} value={day ?? ''} onChange={(e) => setPicked({ market, date: e.target.value })}
          options={(dates.data ?? []).map((d) => ({ value: d, label: d }))} />
        {mode.data && <span className="muted">{mode.data.label_ko}</span>}
      </div>
      {/* v0.8 §2-1: the sell review is a kind of decision, beside 결정·실행; the tab a person works (the queue) after it,
          its count the only red badge */}
      <AgentTabs value={tab} onChange={setTab} label="결정 분류" items={[
        { id: 'all', label: <>결정·실행 {count(rows.length)}</> },
        { id: 'reviews', label: <>매도 검토 {count(reviews.length)}</> },
        { id: 'exceptions', label: <>예외 큐 {count(nExc, true)}</> },
        { id: 'override', label: <>하드룰 개입 {count(overrides.length)}</> },
      ]} />

      <div className="section">
        {tab === 'all' && (
          <Loaded state={all} empty="이 날짜의 결정이 없습니다">
            {() => <div className="dc-grid">{rows.map((d, i) => card(d, i))}</div>}
          </Loaded>
        )}

        {tab === 'exceptions' && (
          <Loaded state={exceptions} empty="예외가 없습니다"
            emptyDescription="모든 결정의 등급을 읽을 수 있었고 거부된 주문도 없습니다(처리한 예외는 빠집니다). REVIEW·자동 실행 한도 초과·벤더 장애가 생기면 여기에 올라옵니다.">
            {(items) => (
              <div className="dc-grid">
                {items.map((x, i) => {
                  const d = decisionOf(x)
                  // this item's own card and buttons: a decision with two exceptions is two cards, each its own (review 1R #4)
                  if (d) return <DecisionCard key={x.kind + x.ticker + (x.ref ?? x.order_id ?? '')} d={d} exception={x} tokenRequired={token} tips={i === 0} />
                  return (
                    <article key={x.kind + x.ticker + (x.ref ?? '')} className="card">
                      <div className="hd">
                        <TickerCell ticker={x.ticker} name={x.name} />
                        <span className="rating REVIEW">{x.kind_ko}</span>
                      </div>
                      <div className="exec exception">{x.summary} · {x.pending_action_ko ?? '자동 주문하지 않았습니다.'}</div>
                      {x.review && <div className="review-line"><ReviewBadges r={x.review} /></div>}
                      {x.detail && <div className="memo">{x.detail}</div>}
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

        {tab === 'reviews' && (
          <Loaded state={all} empty="이 날짜의 결정이 없습니다">
            {() => reviews.length === 0 ? (
              <AgentEmptyState title="매도 검토가 없습니다" description="이 결정일에는 매도 검토가 없습니다. 보유 종목이 있어야 검토가 열리고, 결정일마다 보유 종목 하나에 카드 한 장이 쌓입니다." />
            ) : (
              <div className="dc-grid">{reviews.map((d, i) => card(d, i))}</div>
            )}
          </Loaded>
        )}

        {tab === 'override' && (
          <Loaded state={all} empty="이 날짜의 결정이 없습니다">
            {() => overrides.length === 0 ? (
              <AgentEmptyState title="하드룰 개입이 없습니다" description="이 날짜에 하드 룰이 비중·수량을 줄이거나 매수를 막은 주문이 없습니다. 개입이 있으면 원안(에이전트)과 최종(룰 적용 후)이 나란히 표시됩니다." />
            ) : (
              <div className="dc-grid">{overrides.map((d, i) => card(d, i, true))}</div>
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
