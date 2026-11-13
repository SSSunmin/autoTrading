import { useNavigate } from 'react-router-dom'
import { AgentAlert, AgentSectionPanel } from '@bv-ds/ui'

import { api, money, pct } from '../api/client'
import { useApi } from '../hooks/useApi'
import { useLabels } from '../hooks/useLabels'
import { useMarket, useMarketPath } from '../hooks/useMarket'
import { Loaded, Pct, TickerCell } from '../components/common'
import { AsOfStamp, ExitReasonBadge, PageHeader, RegimeBadge, RiskGauges } from '../components/parts'

/** 5: the paper book replayed from the orders log — holdings with their stop line, trailing high and expiry,
 *  the risk limits, the four Protections, and how positions were closed. Rule verdicts are shown, not acted on. */
export default function Positions() {
  const navigate = useNavigate()
  const [market] = useMarket()
  const to = useMarketPath()
  const labels = useLabels().data
  const info = labels?.markets[market]
  const exitKo = labels?.exit_reason_ko
  const amt = (v: number | null | undefined) => money(v, market)
  const book = useApi(() => api.positions(market), [market])
  return (
    <>
      <PageHeader title="포지션·리스크" description={`${info?.label_ko ?? market} 모의투자 장부: 그 시장의 체결 기록(orders.jsonl)을 캐시 종가로 재생한 값입니다. 따로 저장한 상태는 없고, 다른 시장과 섞지 않습니다.`}
        updated={book.data ? `${book.data.as_of} 종가 기준` : undefined} />
      <Loaded state={book}>
        {(b) => (
          <>
            <AgentAlert tone="info">{b.as_of} 종가 기준 · {b.note}</AgentAlert>
            <div className="section">
              <AgentSectionPanel title="보유 표" description={`${b.positions.length}종목 · 평가 ${amt(b.equity)} · 현금 ${amt(b.cash)} · 미실현 ${amt(b.unrealized_pnl)} · 손절선은 체결 평단 기준`}
                action={<AsOfStamp asOf={b.as_of} />}>
                {b.positions.length === 0 ? <p className="muted">보유 종목이 없습니다.</p> : (
                  <div className="matrix"><table>
                    <thead><tr><th>종목</th><th className="r">수량</th><th className="r">평단</th><th className="r">현재가</th><th className="r">미실현</th><th className="r">비중</th>
                      <th className="r">손절선</th><th className="r">손절까지</th><th className="r">트레일링 고점</th><th className="r">보유일</th><th>만료일</th><th>룰 판정</th></tr></thead>
                    <tbody>{b.positions.map((r) => (
                      <tr key={r.ticker} className="click" onClick={() => navigate(to(`/tickers/${encodeURIComponent(r.ticker)}`))}>
                        <td><TickerCell ticker={r.ticker} name={r.name} /></td>
                        <td className="r num">{r.quantity}</td><td className="r num">{amt(r.avg_price)}</td><td className="r num">{amt(r.last_price)}</td>
                        <td className="r"><Pct value={r.unrealized_pct} /></td><td className="r num">{pct(r.weight).replace('+', '')}</td>
                        <td className="r num">{amt(r.stop_price)}</td>
                        <td className={`r num ${r.near_stop ? 'alert-t' : ''}`}>{pct(r.stop_gap).replace('+', '')}{r.near_stop ? ' · 근접' : ''}</td>
                        <td className="r num">{amt(r.trailing_high)}{r.trailing_active ? ` → ${amt(r.trailing_stop_price)}` : ''}</td>
                        <td className="r num">{r.holding_days}</td><td className="num">{r.expiry_date ?? '–'}</td>
                        <td className="wrap">{r.exit_signal ? <><ExitReasonBadge reason={r.exit_signal.reason} labels={exitKo} /> <span className="muted">{r.exit_signal.note}</span></> : <span className="muted">없음</span>}</td>
                      </tr>
                    ))}</tbody>
                  </table></div>
                )}
              </AgentSectionPanel>
            </div>
            <div className="cards section">
              <AgentSectionPanel title="리스크 게이지" description={`피크 ${amt(b.peak)} · 실현 손익 ${amt(b.realized_pnl)}(시작 자본 ${amt(b.initial_cash)}) · 섹터 노출은 섹터 정보 연결 뒤`}
                action={<AsOfStamp asOf={b.as_of} />}>
                <RiskGauges p={b} amt={amt} />
              </AgentSectionPanel>
              <AgentSectionPanel title="Protections" description="StoplossGuard · MaxDrawdown · Cooldown · LowProfit 상태와 해제일"
                action={<RegimeBadge on={b.regime_on} gap={b.regime_gap} index={info?.benchmark_name_ko ?? '지수'} days={b.limits.regime_ma_days} />}>
                <div className="matrix"><table><tbody>
                  {b.protections.map((g) => (
                    <tr key={g.id}>
                      <td className="num">{g.id}</td><td className={g.active ? 'alert-t' : 'muted'}>{g.active ? '● 발동' : '○ 정상'}</td>
                      <td className="wrap muted">{g.detail ?? ''}{g.tickers.length > 0 ? ` · ${g.tickers.map((t) => `${t.ticker}(${t.until ?? '–'}까지)`).join(', ')}` : ''}{g.until ? ` · ${g.until}까지` : ''}</td>
                    </tr>
                  ))}
                </tbody></table></div>
              </AgentSectionPanel>
            </div>
            <div className="section">
              <AgentSectionPanel title="청산 사유 분포 · 청산 기록" description="청산 사유는 룰의 판정입니다(결정 카드의 에이전트 매도 사유와 다름)">
                <p className="toolbar">{Object.keys(b.exits_by_reason).length === 0 ? <span className="muted">청산 없음</span>
                  : Object.entries(b.exits_by_reason).map(([k, n]) => <span key={k}><ExitReasonBadge reason={k} labels={exitKo} /> <b className="tnum">{n}</b>건</span>)}</p>
                {b.closed.length > 0 && (
                  <div className="matrix"><table>
                    <thead><tr><th>종목</th><th>사유</th><th>진입</th><th>청산</th><th className="r">수량</th><th className="r">진입가</th><th className="r">청산가</th><th className="r">손익</th><th className="r">수익률</th></tr></thead>
                    <tbody>{b.closed.map((t) => (
                      <tr key={t.ticker + t.exit_date}>
                        <td><TickerCell ticker={t.ticker} name={t.name} /></td><td><ExitReasonBadge reason={t.reason} labels={exitKo} /></td><td className="num">{t.entry_date}</td><td className="num">{t.exit_date}</td>
                        <td className="r num">{t.quantity}</td><td className="r num">{amt(t.entry_price)}</td><td className="r num">{amt(t.exit_price)}</td>
                        <td className="r num">{amt(t.pnl)}</td><td className="r"><Pct value={t.pnl_pct} /></td>
                      </tr>
                    ))}</tbody>
                  </table></div>
                )}
              </AgentSectionPanel>
            </div>
          </>
        )}
      </Loaded>
    </>
  )
}
