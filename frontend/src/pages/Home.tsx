import { Link, useNavigate } from 'react-router-dom'
import { AgentAlert, AgentKpiCard, AgentSectionPanel, Grid } from '@bv-ds/ui'

import { api, money, pct, stockName, utc } from '../api/client'
import type { Market, MarketSummary } from '../api/types'
import { useApi } from '../hooks/useApi'
import { useLabels } from '../hooks/useLabels'
import { useMarket, useMarketPath } from '../hooks/useMarket'
import { useStateTick } from '../hooks/useTradingState'
import { ExecStatus, Loaded, ModeBadge, Pct, RatingBadge, TickerCell } from '../components/common'

/** 1 (v0.3): portfolio state, what ran automatically today, and the exceptions a human must look at.
 *  The two market cards (/api/markets) are the one place both books show side by side, each in its own currency
 *  and never added up (docs/08 §2 화면 1). Everything below is the market chosen in the status bar.
 *  Portfolio KPIs, risk limits and Protections come from the paper book (/api/positions), replayed from the orders log. */
export default function Home() {
  const navigate = useNavigate()
  const [market] = useMarket()
  const to = useMarketPath()
  const tick = useStateTick()
  const info = useLabels().data?.markets[market]
  const amt = (v: number | null | undefined) => money(v, market)
  const markets = useApi(() => api.markets(), [tick])
  const dates = useApi(() => api.decisionDates(market), [market])
  // the dates list of the market just left must not drive this market's queries while the new one loads
  const latestDate = dates.loading ? undefined : dates.data?.[0]
  const today = useApi(() => (latestDate ? api.decisions(market, { date: latestDate }) : Promise.resolve([])), [market, latestDate])
  const orders = useApi(() => (latestDate ? api.orders(market, { date: latestDate }) : Promise.resolve([])), [market, latestDate])
  const exceptions = useApi(() => api.exceptions(market), [market])
  const screens = useApi(() => api.screens(market), [market])
  const perf = useApi(() => api.performance(market), [market])
  const book = useApi(() => api.positions(market), [market, tick])
  const latest = screens.data?.[0]
  const first = exceptions.data?.[0]
  const nOrders = (s: string) => orders.data?.filter((o) => o.status === s).length ?? 0
  const b = book.data
  const orderAmount = orders.data?.reduce((sum, o) => sum + (o.amount ?? 0), 0) ?? null
  const kpis = [
    { label: '평가 금액', value: b ? amt(b.equity) : '–', delta: b ? `현금 + 보유 평가액 · ${b.as_of} 종가` : '현금 + 보유 평가액' },
    { label: '오늘 실현 손익', value: b ? amt(b.realized_today) : '–', delta: b ? `누적 ${amt(b.realized_pnl)} · 체결가 기준` : '당일 체결 기준' },
    { label: '미실현 손익', value: b ? amt(b.unrealized_pnl) : '–', delta: '보유 평단 대비' },
    { label: '드로다운', value: b ? pct(b.drawdown) : '–', delta: b ? `피크 대비 · 한도 ${pct(b.limits.dd_halve, 0)} 축소 / ${pct(b.limits.dd_stop, 0)} 정지` : '피크 대비' },
    { label: '그로스 노출 · 현금', value: b ? `${pct(b.gross_exposure).replace('+', '')} · ${amt(b.cash)}` : '–', delta: '포트폴리오 대비' },
    { label: '오늘 주문 금액', value: amt(orderAmount), delta: b ? `결정 ${latestDate ?? '–'} 체결분 · 일일 상한 ${amt(b.limits.auto_daily_order_cap)}` : '자동 실행 일일 상한 대비' },
  ]

  return (
    <>
      <h1 className="page-title">오늘 요약</h1>
      <p className="page-sub">지금 포트폴리오가 어떤 상태이고, 시스템이 오늘 무엇을 자동으로 실행했고, 사람이 봐야 할 예외가 있는지. 숫자는 전부 기록에서 읽고 화면에서 재계산하지 않습니다.</p>

      <div className="cards">
        <Loaded state={markets} empty="시장 요약이 없습니다">
          {(rows) => <>{rows.map((m) => <MarketCard key={m.market} m={m} current={m.market === market} />)}</>}
        </Loaded>
      </div>

      <p className="page-sub section"><b>{info?.label_ko ?? market} 장부</b> · 아래는 상단바에서 고른 시장 기준입니다(금액 {info?.currency ?? ""}).</p>
      <Grid columns={6} gap="1.2rem">
        {kpis.map((k) => <AgentKpiCard key={k.label} label={k.label} value={k.value} delta={book.error ?? k.delta} />)}
      </Grid>

      {exceptions.data && exceptions.data.length > 0 && first && (
        <div className="section">
          <AgentAlert tone="error" title={`예외 ${exceptions.data.length}건 확인 필요`}>
            <b>{stockName(first.ticker, first.name, market)}</b> · {first.summary} ({first.kind_ko}).{' '}
            <Link to={to('/decisions?tab=exceptions')}>예외 큐 열기 →</Link>
          </AgentAlert>
        </div>
      )}

      <div className="section">
        <AgentSectionPanel title="오늘 자동 실행"
          description={latestDate ? `결정 ${latestDate} → 다음 거래일 시가 주문${info ? ` (${info.session.split('-')[0]} 현지)` : ''} · 선별 ${latest ? `${latest.universe_size} → ${latest.selected_count}` : '–'} · 결정 ${today.data?.length ?? '–'} · 체결 ${nOrders('filled')} · 차단 ${nOrders('blocked')} · 예외 ${exceptions.data?.length ?? '–'}` : undefined}>
          <Loaded state={orders} empty="오늘 주문 기록이 없습니다"
            emptyDescription="모의 실행(scripts/paper_execute.py)이 이 결정일을 처리하면 결정마다 방향·수량·체결가·상태·적용 룰이 여기에 기록됩니다.">
            {(rows) => (
              <div className="matrix"><table>
                <thead><tr><th>체결일</th><th>종목</th><th>방향</th><th className="r">수량</th><th className="r">체결가</th><th className="r">금액</th><th>상태</th><th>사유 · 적용 룰</th></tr></thead>
                <tbody>{rows.map((o) => (
                  <tr key={o.order_id} className="click" onClick={() => navigate(to(`/tickers/${encodeURIComponent(o.ticker)}`))}>
                    <td className="num" title={`주문 기록 ${utc(o.placed_at)}`}>{o.fill_date ?? '–'}</td><td><TickerCell ticker={o.ticker} name={o.name} /></td>
                    <td className={o.side === 'Buy' ? 'up' : 'down'}>{o.side_ko}</td>
                    <td className="r num">{o.filled_quantity || o.quantity}</td><td className="r num">{amt(o.filled_price)}</td><td className="r num">{amt(o.amount)}</td>
                    <td><ExecStatus status={o.status} label={o.status_ko} /></td>
                    <td className="wrap muted">{o.reason} {o.rules_applied.join(', ')}</td>
                  </tr>
                ))}</tbody>
              </table></div>
            )}
          </Loaded>
        </AgentSectionPanel>
      </div>

      <div className="cards section">
        <AgentSectionPanel title="리스크 한도" description="드로다운 · 일일 손실 · 오늘 주문 금액 · 섹터 노출을 한도선과 함께" action={<Link to={to('/positions')}>포지션·리스크 →</Link>}>
          <p className="muted">한도를 넘으면 자동 매매가 신규 주문을 멈추고 예외 큐와 알림으로 알립니다.</p>
          <Loaded state={book}>
            {(p) => (
              <div className="matrix"><table><tbody>
                <tr><td>드로다운</td><td className={`r num ${p.drawdown <= p.limits.dd_halve ? 'down' : ''}`}>{pct(p.drawdown)}</td><td className="muted">한도 {pct(p.limits.dd_halve, 0)} 축소 · {pct(p.limits.dd_stop, 0)} 정지</td></tr>
                <tr><td>그로스 노출</td><td className="r num">{pct(p.gross_exposure).replace('+', '')}</td><td className="muted">한도 {pct(p.limits.max_gross_pct, 0).replace('+', '')}</td></tr>
                <tr><td>오늘 주문 금액</td><td className="r num">{amt(orderAmount)}</td><td className="muted">일일 상한 {amt(p.limits.auto_daily_order_cap)}</td></tr>
                <tr><td>종목당 최대 비중</td><td className="r num">{pct(Math.max(0, ...p.positions.map((r) => r.weight ?? 0))).replace('+', '')}</td><td className="muted">한도 {pct(p.limits.max_position_pct, 0).replace('+', '')}</td></tr>
                <tr><td>국면 ({info?.benchmark_name_ko ?? '지수'} vs 200일선)</td><td className="r">{p.regime_on == null ? '–' : p.regime_on ? '매수 가능' : '신규 매수 없음'}</td><td className="muted">{p.as_of} 기준</td></tr>
              </tbody></table></div>
            )}
          </Loaded>
        </AgentSectionPanel>
        <AgentSectionPanel title="Protections · 손절 근접" description="4종 상태와 손절선 2% 이내 종목 수">
          <Loaded state={book}>
            {(p) => (
              <div className="matrix"><table><tbody>
                {p.protections.map((g) => (
                  <tr key={g.id}><td className="num">{g.id}</td><td className={g.active ? 'down' : 'muted'}>{g.active ? '발동' : '정상'}</td><td className="wrap muted">{g.detail ?? ''}</td></tr>
                ))}
                <tr><td>손절선 2% 이내</td><td className="num">{p.positions.filter((r) => r.last_price != null && r.last_price <= r.stop_price * 1.02).length}</td><td className="muted">보유 {p.positions.length}종목</td></tr>
              </tbody></table></div>
            )}
          </Loaded>
        </AgentSectionPanel>
      </div>

      <div className="section">
        <AgentSectionPanel title="최근 정산" description={perf.data ? `실측 · ${perf.data.holding} · ${info?.benchmark_name_ko ?? '지수'} 대비` : undefined} action={<Link to={to('/performance')}>등급별 성과·알파 추이 →</Link>}>
          <Loaded state={perf}>
            {(p) => (
              <div className="matrix"><table>
                <thead><tr><th>종목</th><th>결정일</th><th>등급</th><th className="r">실현</th><th className="r">알파</th><th>정산일</th></tr></thead>
                <tbody>{p.reflections.slice(0, 5).map((r) => (
                  <tr key={r.ticker + r.date} className="click" onClick={() => navigate(to(`/tickers/${encodeURIComponent(r.ticker)}`))}>
                    <td><TickerCell ticker={r.ticker} name={r.name} /></td><td className="num">{r.date}</td>
                    <td><RatingBadge rating={r.rating} label={r.rating_ko} /></td>
                    <td className="r"><Pct value={r.raw_return} /></td><td className="r"><Pct value={r.alpha} /></td><td className="num">{r.resolved ?? ''}</td>
                  </tr>
                ))}</tbody>
              </table></div>
            )}
          </Loaded>
        </AgentSectionPanel>
      </div>
    </>
  )
}

const MARKET_KO: Record<Market, string> = { KRX: '국내', US: '미국' }

/** One book on the home screen: value in its own currency, today, drawdown, mode and the next run in KST. */
function MarketCard({ m, current }: { m: MarketSummary; current: boolean }) {
  const [, setMarket] = useMarket()
  return (
    <AgentSectionPanel title={`${MARKET_KO[m.market]} · ${m.currency}`} description={m.error ? m.benchmark_name_ko : `${m.benchmark_name_ko} · ${m.as_of} 종가 · 보유 ${m.positions}종목`}
      action={current ? <span className="muted">보는 중</span> : <a href={`?market=${m.market}`} onClick={(e) => { e.preventDefault(); setMarket(m.market) }}>{MARKET_KO[m.market]} 보기 →</a>}>
      {m.error ? <AgentAlert tone="error" title="기록 파손">{m.error}</AgentAlert> : <div className="matrix"><table><tbody>
        <tr><td>평가액</td><td className="r num">{money(m.equity, m.market)}</td><td className="muted">시작 {money(m.initial_cash, m.market)}</td></tr>
        <tr><td>오늘 손익</td><td className="r"><Pct value={m.day_return} digits={2} /></td><td className="muted">전일 종가 평가 대비</td></tr>
        <tr><td>드로다운</td><td className="r"><Pct value={m.drawdown} /></td><td className="muted">피크 대비</td></tr>
        <tr><td>운용</td><td className="r"><ModeBadge state={m.state} /></td><td className="muted">{m.state?.running ? '' : m.state?.halt_reason}</td></tr>
        <tr><td>다음 실행</td><td className="r num">{m.next_run_label_ko}</td><td className="muted">정규장 {m.session_kst} KST · 마감 5분 뒤</td></tr>
      </tbody></table></div>}
    </AgentSectionPanel>
  )
}
