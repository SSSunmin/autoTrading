import { Link, useNavigate } from 'react-router-dom'
import { AgentAlert, AgentKpiCard, AgentSectionPanel, Grid } from '@bv-ds/ui'

import { api, money, pct, stockName, utc } from '../api/client'
import type { Market, MarketFlow, MarketSummary, Order, TradingState } from '../api/types'
import { useApi } from '../hooks/useApi'
import { useLabels } from '../hooks/useLabels'
import { useMarket, useMarketPath } from '../hooks/useMarket'
import { useStateTick } from '../hooks/useTradingState'
import { ExecStatus, Loaded, ModeBadge, Pct, RatingBadge, TickerCell } from '../components/common'
import { AsOfStamp, PageHeader, RegimeBadge, RiskGauges } from '../components/parts'
import { FillTime, HaltBand, HaltedChip } from '../components/risk'

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
  const flow = markets.data?.find((m) => m.market === market)?.flow ?? null
  const halted = b && !b.mode.running ? b.mode : null
  const nearStop = b?.positions.filter((r) => r.near_stop) ?? []
  const kpis = [
    { label: '평가 금액', value: b ? amt(b.equity) : '–', delta: b ? `현금 + 보유 평가액 · ${b.as_of} 종가` : '현금 + 보유 평가액' },
    { label: '오늘 실현 손익', value: b ? amt(b.realized_today) : '–', delta: b ? `누적 ${amt(b.realized_pnl)} · 체결가 기준` : '당일 체결 기준' },
    { label: '미실현 손익', value: b ? amt(b.unrealized_pnl) : '–', delta: '보유 평단 대비' },
    { label: '드로다운', value: b ? pct(b.drawdown) : '–', delta: b ? `피크 대비 · 한도 ${pct(b.limits.dd_halve, 0)} 축소 / ${pct(b.limits.dd_stop, 0)} 정지` : '피크 대비' },
    { label: '그로스 노출 · 현금', value: b ? `${pct(b.gross_exposure).replace('+', '')} · ${amt(b.cash)}` : '–', delta: '포트폴리오 대비' },
    { label: '오늘 주문 금액', value: b ? `매수 ${amt(b.today_buy_amount)} · 매도 ${amt(b.today_sell_amount)}` : '–',
      delta: b ? `일일 상한 ${amt(b.limits.auto_daily_order_cap)}의 ${pct(b.today_buy_cap_used).replace('+', '')}(매수만) · ${b.today_session ?? '다음 거래일'} 시가` : '일일 상한은 매수만' },
  ]

  const orderTable = (rows: Order[]) => (
    <div className="matrix"><table>
      <thead><tr><th>체결 시각</th><th>종목</th><th>방향</th><th className="r">수량</th><th className="r">체결가</th><th className="r">금액</th><th>상태</th><th>사유 · 적용 룰</th></tr></thead>
      <tbody>{halted && b && <HaltRows state={halted} held={b.positions.length} signals={b.positions.filter((r) => r.exit_signal).length} />}{rows.map((o) => (
        <tr key={o.order_id} className="click" onClick={() => navigate(to(`/tickers/${encodeURIComponent(o.ticker)}`))}>
          <td className="num" title={`주문 기록 ${utc(o.placed_at)}`}><FillTime o={o} market={market} /></td><td><TickerCell ticker={o.ticker} name={o.name} /></td>
          <td className={o.side === 'Buy' ? 'up' : 'down'}>{o.side_ko}</td>
          <td className="r num">{o.filled_quantity || o.quantity}</td><td className="r num">{amt(o.filled_price)}</td><td className="r num">{amt(o.amount)}</td>
          <td><ExecStatus status={o.status} label={o.status_ko} /></td>
          <td className="wrap muted">{o.reason} {o.rules_applied.join(', ')}</td>
        </tr>
      ))}</tbody>
    </table></div>
  )

  return (
    <>
      <PageHeader title="오늘 요약" description="지금 포트폴리오가 어떤 상태이고, 시스템이 오늘 무엇을 자동으로 실행했고, 사람이 봐야 할 예외가 있는지. 숫자는 전부 기록에서 읽고 화면에서 재계산하지 않습니다."
        updated={b ? `${b.as_of} 종가 기준` : undefined} />

      <div className="cards">
        <Loaded state={markets} empty="시장 요약이 없습니다">
          {(rows) => <>{rows.map((m) => <MarketCard key={m.market} m={m} current={m.market === market} />)}</>}
        </Loaded>
      </div>

      {halted && <HaltBand state={halted} market={market} />}

      <p className="page-sub section"><b>{info?.label_ko ?? market} 장부</b> · 아래는 상단바에서 고른 시장 기준입니다(금액 {info?.currency ?? ""}).</p>
      <Grid columns={6} gap="1.2rem">
        {kpis.map((k) => <AgentKpiCard key={k.label} label={k.label} value={k.value} delta={book.error ?? k.delta} />)}
      </Grid>

      {flow && <FlowCards flow={flow} asOf={b?.as_of} to={to} />}

      {exceptions.data && exceptions.data.length > 0 && first && (
        <div className="section">
          <AgentAlert tone="error" title={`예외 ${exceptions.data.length}건 확인 필요`}>
            <b>{stockName(first.ticker, first.name, market)}</b> · {first.summary} ({first.kind_ko}).{' '}
            <Link to={to('/decisions?tab=exceptions')}>예외 큐 열기 →</Link>
          </AgentAlert>
        </div>
      )}

      <div className="section">
        <AgentSectionPanel title="오늘 자동 실행" action={halted ? <HaltedChip state={halted} /> : undefined}
          description={latestDate ? `결정 ${latestDate} → 다음 거래일 시가 주문${info ? ` (${info.session.split('-')[0]} 현지)` : ''} · 선별 ${latest ? `${latest.universe_size} → ${latest.selected_count}` : '–'} · 결정 ${today.data?.length ?? '–'} · 체결 ${nOrders('filled')} · 차단 ${nOrders('blocked')} · 예외 ${exceptions.data?.length ?? '–'}` : undefined}>
          {/* halted: the halt rows stand even on a day with no orders (the empty state would hide them) */}
          {halted && orders.data?.length === 0 ? orderTable([]) : (
            <Loaded state={orders} empty="오늘 주문 기록이 없습니다"
              emptyDescription="모의 실행(scripts/paper_execute.py)이 이 결정일을 처리하면 결정마다 방향·수량·체결가·상태·적용 룰이 여기에 기록됩니다.">
              {orderTable}
            </Loaded>
          )}
        </AgentSectionPanel>
      </div>

      <div className="cards section">
        <AgentSectionPanel title="리스크 한도" description="검은 선은 한도, 빨간 선은 드로다운 정지. 한도를 넘으면 신규 주문을 멈추고 예외 큐로 알립니다"
          action={<span className="toolbar" style={{ marginBottom: 0 }}><AsOfStamp asOf={b?.as_of} /><Link to={to('/positions')}>포지션·리스크 →</Link></span>}>
          <Loaded state={book}>
            {(p) => <RiskGauges p={p} amt={amt} />}
          </Loaded>
        </AgentSectionPanel>
        <AgentSectionPanel title="Protections · 손절 근접" description="4종 상태와 손절선 근접 종목 수"
          action={b ? <RegimeBadge on={b.regime_on} gap={b.regime_gap} index={info?.benchmark_name_ko ?? '지수'} days={b.limits.regime_ma_days} /> : undefined}>
          <Loaded state={book}>
            {(p) => (
              <div className="matrix"><table><tbody>
                {p.protections.map((g) => (
                  <tr key={g.id}><td className="num">{g.id}</td><td className={g.active ? 'alert-t' : 'muted'}>{g.active ? '● 발동' : '○ 정상'}</td>
                    <td className="wrap muted">{g.detail ? `${g.detail} · ` : ''}{g.until ? `해제 ${g.until} · ` : ''}lookback {g.lookback_days}거래일 내 트리거 {g.lookback_hits}건</td></tr>
                ))}
                <tr><td>손절선 {pct(p.limits.near_stop_pct, 0).replace('+', '')} 이내</td><td className="num">{p.near_stop_count}</td>
                  <td className="wrap muted">{nearStop.length ? nearStop.map((r) => `${stockName(r.ticker, r.name, market)} 손절까지 ${pct(r.stop_gap).replace('+', '')}`).join(' · ') : `보유 ${p.positions.length}종목 중 없음`}</td></tr>
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

/** While halted, the two rows on top of today's orders (mockup MainHalted): the halt itself, and the exits still
 *  watched (keep_exits) — the held names and how many carry a rule-exit verdict now (the server's per-row verdict). */
function HaltRows({ state, held, signals }: { state: TradingState; held: number; signals: number }) {
  return (
    <>
      <tr className="halt-row"><td className="num">{utc(state.halted_at)}</td><td colSpan={5}>자동 실행 정지 · {state.halt_target_ko ?? state.halted_by_ko}</td>
        <td><span className="st halt">정지</span></td><td className="wrap muted">{state.halt_reason}</td></tr>
      {state.keep_exits && (
        <tr className="halt-row"><td>감시 중</td><td className="muted">보유 {held}종목</td><td className="down">청산 매도</td><td colSpan={3} />
          <td><span className="st wait">대기</span></td><td className="wrap muted">손절·트레일링 조건 충족 시에만 매도 주문 · 지금 해당 {signals}종목</td></tr>
      )}
    </>
  )
}

/** Four cards from screening to holdings (mockup v0.6 홈 흐름 카드), each a link to its screen; counts from /api/markets. */
function FlowCards({ flow, asOf, to }: { flow: MarketFlow; asOf: string | undefined; to: (path: string) => string }) {
  const join = (m: Record<string, number>) => Object.entries(m).map(([k, n]) => `${k} ${n}`).join(' · ')
  const { screen, decisions, exceptions, holdings } = flow
  return (
    <nav className="flow section" aria-label="오늘 흐름">
      <Link className="flow-card" to={to('/screens')}>
        <span className="hd"><span className="k">선별 후보</span><span className="v tnum">{screen ? screen.selected_count : '–'}</span></span>
        <span className="s">{screen ? <>유니버스 {screen.universe_size}종목 중 · <AsOfStamp asOf={screen.as_of} expected={asOf} /></> : '선별 실행 없음'}</span>
      </Link>
      <Link className="flow-card" to={to('/decisions')}>
        <span className="hd"><span className="k">오늘 결정{decisions.date ? ` · ${decisions.date}` : ''}</span><span className="v tnum">{decisions.total}</span></span>
        <span className="s">{[join(decisions.by_rating_ko), decisions.rule ? `규칙 청산 ${decisions.rule}` : ''].filter(Boolean).join(' · ') || '결정 없음'}</span>
      </Link>
      <Link className="flow-card" to={to('/decisions?tab=exceptions')}>
        <span className="hd"><span className="k">예외</span><span className={`v tnum ${exceptions.total > 0 ? 'alert' : ''}`}>{exceptions.total}</span></span>
        <span className="s">{join(exceptions.by_kind_ko) || '확인할 예외 없음'}</span>
      </Link>
      <Link className="flow-card" to={to('/positions')}>
        <span className="hd"><span className="k">보유 종목</span><span className="v tnum">{holdings.count}</span></span>
        <span className="s">손절 근접 {holdings.near_stop} · Protections {holdings.protections_active}건 발동</span>
      </Link>
    </nav>
  )
}

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
        <tr><td>다음 실행</td><td className="r num">{m.next_run_label_ko}</td>
          <td className="muted">{m.state && !m.state.running ? (m.state.keep_exits ? '결정만 기록 · 청산 매도만' : '결정만 기록 · 주문 없음') : `정규장 ${m.session_kst} KST · 마감 5분 뒤`}</td></tr>
      </tbody></table></div>}
    </AgentSectionPanel>
  )
}
