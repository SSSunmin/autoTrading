import { Link } from 'react-router-dom'
import { AgentChip } from '@bv-ds/ui'

import { money, utc } from '../api/client'
import type { Decision } from '../api/types'
import { useMarketPath } from '../hooks/useMarket'
import { ExecStatus, Pct, RatingBadge, TickerCell } from './common'
import { FillTime } from './risk'

/** One decision as the plan's card (docs/02 v0.3 화면 3): rating, action, confidence, the thesis as the manager wrote it,
 *  evidence with source and as-of, the execution strip (filled / blocked / exception) joined from the orders log,
 *  the settled outcome, and any hard-rule overrides. */
export function DecisionCard({ d }: { d: Decision }) {
  const to = useMarketPath()
  const amt = (v: number | null | undefined) => money(v, d.market)    // the record's own market, not the toggle
  return (
    <article className="card">
      <div className="hd">
        <TickerCell ticker={d.ticker} name={d.name} />
        <div style={{ display: 'flex', gap: '0.6rem', alignItems: 'center', flexWrap: 'wrap' }}>
          {d.source === 'rule' && <AgentChip variant="hot">{d.source_ko}</AgentChip>}
          <RatingBadge rating={d.rating} label={d.rating_ko} />
          {d.action && <AgentChip variant="neutral">{d.action === 'Buy' ? '매수' : d.action === 'Sell' ? '매도' : '관망'}</AgentChip>}
          <span className="muted">확신도 <b className="num">{d.confidence?.toFixed(2) ?? '–'}</b></span>
          <span className="muted num">{d.trade_date}</span>
        </div>
      </div>

      {d.rating === 'REVIEW' && (
        <div className="exec exception">등급을 읽을 수 없어 거래 대상에서 빠졌습니다. 에이전트 원문 전체는 아래에 있습니다.</div>
      )}
      {d.orders.length > 0 ? d.orders.map((o) => (
        <div key={o.order_id} className={`exec ${o.status}`}>
          <ExecStatus status={o.status} label={o.status_ko} />
          <span>{o.side_ko} {o.filled_quantity || o.quantity}주{o.filled_price != null ? ` · ${amt(o.filled_price)} · ${amt(o.amount)}` : ''}</span>
          {o.reason && <span className="muted">{o.reason}</span>}
          {o.rules_applied.map((r) => <AgentChip key={r} variant="hot">{r}</AgentChip>)}
          <span className="muted num" style={{ marginLeft: 'auto' }} title={`주문 기록 ${utc(o.placed_at)}`}><FillTime o={o} market={o.market} /></span>
        </div>
      )) : d.tradeable && (
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
        {d.stop_loss != null && <span>손절가 <b className="num">{amt(d.stop_loss)}</b></span>}
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
        <Link to={to(`/tickers/${encodeURIComponent(d.ticker)}`)} style={{ marginLeft: 'auto' }}>종목 상세 →</Link>
      </div>
    </article>
  )
}
