import { Link } from 'react-router-dom'
import { AgentChip } from '@bv-ds/ui'

import { money, utc } from '../api/client'
import type { Decision, ExceptionItem } from '../api/types'
import { useLabels } from '../hooks/useLabels'
import { useMarketPath } from '../hooks/useMarket'
import { ExecStatus, Pct, RatingBadge, TickerCell } from './common'
import { orderWords } from './decisionLogic'
import { ExceptionButtons, Interventions, PlanGrid, SourceBadge } from './decisionParts'
import { FillTime } from './risk'

/** One decision as the plan's card (docs/02 v0.3 화면 3): its source, rating, action, confidence, the thesis as the
 *  manager wrote it, evidence with source and as-of, the execution strip (filled / blocked / exception) joined from the
 *  orders log, the 원안 vs 최종 grid with its intervention chips where a hard rule cut or blocked the order, the settled
 *  outcome, and — an exception still on the queue — its actions. */
export function DecisionCard({ d, exception, tokenRequired = false }: { d: Decision; exception?: ExceptionItem; tokenRequired?: boolean }) {
  const to = useMarketPath()
  const actionKo = useLabels().data?.exception_action_ko ?? {}
  const amt = (v: number | null | undefined) => money(v, d.market)    // the record's own market, not the toggle
  return (
    <article className="card">
      <div className="hd">
        <TickerCell ticker={d.ticker} name={d.name} />
        <div style={{ display: 'flex', gap: '0.6rem', alignItems: 'center', flexWrap: 'wrap' }}>
          <SourceBadge d={d} />
          <RatingBadge rating={d.rating} label={d.rating_ko} />
          {d.action && <AgentChip variant="neutral">{d.action === 'Buy' ? '매수' : d.action === 'Sell' ? '매도' : '관망'}</AgentChip>}
          <span className="muted">확신도 <b className="num">{d.confidence?.toFixed(2) ?? '–'}</b></span>
          <span className="muted num">{d.trade_date}</span>
        </div>
      </div>

      {d.rating === 'REVIEW' && (
        <div className="exec exception">등급을 읽을 수 없어 거래 대상에서 빠졌습니다. 에이전트 원문 전체는 아래에 있습니다.</div>
      )}
      {d.source === 'human' && (
        <div className="exec none muted">사람 처리: 보유(Hold) · 주문 없음 · 채점 제외</div>
      )}
      {d.orders.length > 0 ? d.orders.map((o) => (
        <div key={o.order_id}>
          <div className={`exec ${o.status}`}>
            <ExecStatus status={o.status} label={o.status_ko} />
            <span>{orderWords(o)}{o.filled_price != null ? ` · ${amt(o.filled_price)} · ${amt(o.amount)}` : ''}</span>
            {o.reason && <span className="muted">{o.reason}</span>}
            {o.exception_resolved && o.exception_action && <AgentChip variant="secondary">처리됨 · {actionKo[o.exception_action] ?? o.exception_action}</AgentChip>}
            {o.rerun_answer && (
              <Link to={to(`/decisions?date=${o.rerun_answer.trade_date}`)} title={o.rerun_answer.same_day ? '그날 결정이 이 종목을 이미 판단해 그 결정이 답입니다' : undefined}>
                <AgentChip variant="new">재실행 {o.rerun_answer.attempt}회째 답 · {o.rerun_answer.trade_date} {o.rerun_answer.rating_ko} →</AgentChip>
              </Link>
            )}
            <span className="muted num" style={{ marginLeft: 'auto' }} title={`주문 기록 ${utc(o.placed_at)}`}><FillTime o={o} market={o.market} /></span>
          </div>
          {o.plan && o.plan.interventions.length > 0 && (
            <div className="plan">
              <PlanGrid plan={o.plan} market={o.market} />
              <Interventions ivs={o.plan.interventions} />
            </div>
          )}
        </div>
      )) : d.tradeable && d.source !== 'human' && (
        <div className="exec none muted">주문 기록 없음 · 아직 모의 실행 전 (결정은 다음 거래일 시가에 주문됩니다)</div>
      )}

      <div className="memo">{d.thesis.replace(/\*\*/g, '')}</div>

      {d.evidence.length > 0 && (
        <div className="evidence">
          {d.evidence.map((e, i) => (
            <div key={i}><span className="src">{e.source}</span><span>{e.summary}</span><span className="muted num">{e.as_of ?? ''}</span></div>
          ))}
        </div>
      )}

      <div className="meta">
        {d.entry_price != null && <span>진입가 <b className="num">{amt(d.entry_price)}</b></span>}
        {d.stop_loss != null && <span title="에이전트 원안(결정가 기준). 실제 손절선은 체결 평단 기준(포지션 화면)">제안 손절가 <b className="num">{amt(d.stop_loss)}</b></span>}
        {d.position_sizing && <span>비중 <b>{d.position_sizing}</b></span>}
        <span>
          결과{' '}
          {d.outcome == null ? '기록 없음' : d.outcome.pending ? <RatingBadge rating="pending" label="정산 대기" /> : (
            <>실현 <Pct value={d.outcome.raw_return} /> · 알파 <Pct value={d.outcome.alpha} /></>
          )}
        </span>
        {d.hard_rule_overrides.length > 0 && (
          <span>개입 {d.hard_rule_overrides.map((o) => <AgentChip key={o} variant="hot">{o}</AgentChip>)}</span>
        )}
        {exception && <span className="xa-btns"><ExceptionButtons item={exception} tokenRequired={tokenRequired} /></span>}
        <Link to={to(`/tickers/${encodeURIComponent(d.ticker)}`)} style={{ marginLeft: 'auto' }}>종목 상세 →</Link>
      </div>
    </article>
  )
}
