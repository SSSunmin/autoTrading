import { AgentEmptyState, AgentSectionPanel } from '@bv-ds/ui'

import { money } from '../api/client'
import type { Market, SellReviewInfo, ThesisCheck, TickerDetail, TickerHolding } from '../api/types'
import { reasonChips } from './cardLogic'
import { Pct, RatingBadge } from './common'
import { earningsLine, latestReview, metLine, verdictBadge } from './tickerLogic'

/** Header facts: sector and exchange from files, the holding in one line, the earnings date as a reference only. */
export function TickerFacts({ d }: { d: TickerDetail }) {
  const e = earningsLine(d)
  const h = d.holding
  return (
    <div className="meta ticker-facts">
      <span>업종 <b>{d.sector ?? '미분류'}</b></span>
      <span>거래소 <b>{d.exchange ?? '미확인'}</b></span>
      <span>보유 <b>{h ? `${h.quantity}주 · ${h.entry_date} 진입` : '없음'}</b></span>
      <span title={e.sub}>실적 발표 <span className="tag">참고</span> {e.failed && !e.date ? <span className="tag fail-tag">조회 실패</span>
        : <b>{e.date ?? '–'}</b>}{e.failed && e.date && <span className="tag fail-tag">갱신 실패 · 이전 값</span>} <span className="muted">{e.sub}</span></span>
    </div>
  )
}

/** A sell review's badges: the verdict (the 논지 점검 mapping) and the reasons the agent gave as the v0.8 chips (the
 *  mockup's order and words, the main one filled, 하드 룰 amber — ``max`` shown and "+n": a table cell 1). ``source`` adds
 *  the "에이전트 매도 검토" tag so a row never reads it as a rule exit's reason (ExitReasonBadge). */
export function ReviewBadges({ r, source = true, max }: {
  r: Pick<SellReviewInfo, 'action' | 'verdict' | 'verdict_ko' | 'reasons' | 'reasons_ko' | 'trim_to_pct'>; source?: boolean; max?: number
}) {
  const b = verdictBadge(r)
  return (
    <span className="review-badges">
      {source && <span className="tag review-src">에이전트 매도 검토</span>}
      <RatingBadge rating={b.rating} label={b.label} />
      {r.reasons.length > 0 && reasonChips(r.reasons, max).map((c) => <span key={c.text} className={`rc ${c.cls}`}>{c.text}</span>)}
      {r.action === 'Trim' && r.trim_to_pct != null && <span className="muted">목표 비중 {r.trim_to_pct}%</span>}
    </span>
  )
}

function Price({ v, market }: { v: number | null | undefined; market: Market }) {
  return <span className="num">{money(v, market)}</span>
}

function CheckRow({ c, market }: { c: ThesisCheck; market: Market }) {
  const b = verdictBadge(c)
  return (
    <li className="thesis-row">
      <div className="thesis-hd">
        <span className="num">{c.trade_date}</span>
        {c.kind === 'review' && c.action && c.verdict !== 'memo'
          ? <ReviewBadges source={false} r={{ action: c.action, verdict: c.verdict, verdict_ko: c.verdict_ko, reasons: c.reasons ?? [],
            reasons_ko: c.reasons_ko ?? [], trim_to_pct: c.trim_to_pct ?? null }} />
          : <RatingBadge rating={b.rating} label={b.label} />}
        <span className="muted">{metLine(c)}</span>
      </div>
      {c.kind === 'memo' ? (
        <div className="thesis-body">
          <p>{c.thesis}</p>
          <div className="meta">
            <span>현재가 <Price v={c.price} market={market} /></span><span>목표가 <Price v={c.target} market={market} /></span>
            <span>하락가(손절 제안) <Price v={c.downside} market={market} /></span>{c.horizon && <span>기간 {c.horizon}</span>}
          </div>
          {c.exit_plan && <p className="muted">매도 계획 · {c.exit_plan}</p>}
        </div>
      ) : c.reasoning ? <p className="thesis-body">{c.reasoning}</p> : null}
      <ul className="thesis-checks">{c.checks.map((k) => (
        <li key={k.condition} className={k.met ? 'met' : undefined}>
          <span className="tag">{k.met == null ? '깨짐 조건' : k.met ? '충족' : '미충족'}</span> {k.condition}
          {k.evidence && <span className="muted"> — {k.evidence}</span>}
        </li>
      ))}</ul>
    </li>
  )
}

/** 논지 점검: the memo's invalidation conditions and every sell review against them, as recorded — no new model call. */
export function ThesisPanel({ checks, market }: { checks: ThesisCheck[]; market: Market }) {
  const last = latestReview(checks)
  const badge = last && verdictBadge(last)
  return (
    <AgentSectionPanel title="논지 점검" description="투자 메모의 무효화 조건과 보유 중 매도 검토 결과를 기록 그대로 시간순으로 보여 줍니다">
      {last && badge ? (
        <div className="thesis-verdict"><RatingBadge rating={badge.rating} label={badge.label} />
          <span className="muted">{last.trade_date} 검토 · {metLine(last)}</span></div>
      ) : <AgentEmptyState title="이 종목은 아직 매도 검토 기록이 없습니다" compact
        description={checks.length ? '투자 메모만 있습니다 — 보유 중 매도 검토가 기록되면 판정이 붙습니다.' : '투자 메모도 없습니다.'} />}
      {checks.length > 0 && <ol className="thesis-list">{checks.map((c) => <CheckRow key={`${c.trade_date}-${c.recorded_at}`} c={c} market={market} />)}</ol>}
    </AgentSectionPanel>
  )
}

/** 보유: the paper book's row for this ticker, as the 포지션 screen has it, and its partial sells. */
export function HoldingPanel({ h, market }: { h: TickerHolding | null; market: Market }) {
  if (!h) return <AgentEmptyState title="보유하지 않은 종목입니다" description="모의 장부에 이 종목의 보유가 없습니다." />
  return (
    <AgentSectionPanel title="보유" description={`${h.as_of ?? '–'} 종가 기준 · 모의 장부`}>
      <div className="matrix"><table className="holding-table">
        <tbody>
          <tr><th>수량</th><td className="r num">{h.quantity}주{h.partial_sold > 0 && <span className="muted"> · 부분 매도 {h.partial_sold}주</span>}</td></tr>
          <tr><th>평단 / 현재가</th><td className="r num">{money(h.avg_price, market)} / {money(h.last_price, market)}</td></tr>
          <tr><th>평가액</th><td className="r num">{money(h.market_value, market)}</td></tr>
          <tr><th>미실현 손익</th><td className="r"><span className={`num ${h.unrealized == null ? '' : h.unrealized > 0 ? 'up' : h.unrealized < 0 ? 'down' : ''}`}>{money(h.unrealized, market)}</span> <Pct value={h.unrealized_pct} /></td></tr>
          <tr><th>손절선</th><td className="r num">{money(h.stop_price, market)}</td></tr>
          <tr><th>트레일링 고점</th><td className="r num">{money(h.trailing_high, market)}{h.trailing_active ? ` → ${money(h.trailing_stop_price, market)}` : ''}</td></tr>
          <tr><th>보유일 · 만료</th><td className="r num">{h.holding_days}일 · {h.expiry_date ?? '–'}</td></tr>
        </tbody>
      </table></div>
      <h3 className="sub-title">부분 매도 이력</h3>
      {h.partial_sales.length ? (
        <div className="matrix"><table>
          <thead><tr><th>체결일</th><th className="r">수량</th><th className="r">체결가</th></tr></thead>
          <tbody>{h.partial_sales.map((p) => (
            <tr key={p.order_id}><td className="num">{p.date}</td><td className="r num">{p.quantity}주</td><td className="r num">{money(p.price, market)}</td></tr>
          ))}</tbody>
        </table></div>
      ) : <AgentEmptyState title="부분 매도 없음" compact />}
    </AgentSectionPanel>
  )
}
