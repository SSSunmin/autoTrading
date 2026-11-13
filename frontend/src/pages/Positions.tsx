import { useNavigate } from 'react-router-dom'
import { AgentAlert, AgentChip, AgentSectionPanel } from '@bv-ds/ui'

import { api, money, pct } from '../api/client'
import { useApi } from '../hooks/useApi'
import { useLabels } from '../hooks/useLabels'
import { useMarket, useMarketPath } from '../hooks/useMarket'
import { Loaded, Pct, TickerCell } from '../components/common'

/** 5: the paper book replayed from the orders log — holdings with their stop line, trailing high and expiry,
 *  the risk limits, the four Protections, and how positions were closed. Rule verdicts are shown, not acted on. */
export default function Positions() {
  const navigate = useNavigate()
  const [market] = useMarket()
  const to = useMarketPath()
  const info = useLabels().data?.markets[market]
  const amt = (v: number | null | undefined) => money(v, market)
  const book = useApi(() => api.positions(market), [market])
  return (
    <>
      <h1 className="page-title">포지션·리스크</h1>
      <p className="page-sub">{info?.label_ko ?? market} 모의투자 장부: 그 시장의 체결 기록(orders.jsonl)을 캐시 종가로 재생한 값입니다. 따로 저장한 상태는 없고, 다른 시장과 섞지 않습니다.</p>
      <Loaded state={book}>
        {(b) => (
          <>
            <AgentAlert tone="info">{b.as_of} 종가 기준 · {b.note}</AgentAlert>
            <div className="section">
              <AgentSectionPanel title="보유 표" description={`${b.positions.length}종목 · 평가 ${amt(b.equity)} · 현금 ${amt(b.cash)} · 미실현 ${amt(b.unrealized_pnl)}`}>
                {b.positions.length === 0 ? <p className="muted">보유 종목이 없습니다.</p> : (
                  <div className="matrix"><table>
                    <thead><tr><th>종목</th><th className="r">수량</th><th className="r">평단</th><th className="r">현재가</th><th className="r">미실현</th><th className="r">비중</th>
                      <th className="r">손절선</th><th className="r">트레일링 고점</th><th className="r">보유일</th><th>만료일</th><th>룰 판정</th></tr></thead>
                    <tbody>{b.positions.map((r) => (
                      <tr key={r.ticker} className="click" onClick={() => navigate(to(`/tickers/${encodeURIComponent(r.ticker)}`))}>
                        <td><TickerCell ticker={r.ticker} name={r.name} /></td>
                        <td className="r num">{r.quantity}</td><td className="r num">{amt(r.avg_price)}</td><td className="r num">{amt(r.last_price)}</td>
                        <td className="r"><Pct value={r.unrealized_pct} /></td><td className="r num">{pct(r.weight).replace('+', '')}</td>
                        <td className="r num">{amt(r.stop_price)}</td>
                        <td className="r num">{amt(r.trailing_high)}{r.trailing_active ? ` → ${amt(r.trailing_stop_price)}` : ''}</td>
                        <td className="r num">{r.holding_days}</td><td className="num">{r.expiry_date ?? '–'}</td>
                        <td className="wrap">{r.exit_signal ? <AgentChip variant="hot" title={r.exit_signal.note}>{r.exit_signal.note}</AgentChip> : <span className="muted">없음</span>}</td>
                      </tr>
                    ))}</tbody>
                  </table></div>
                )}
              </AgentSectionPanel>
            </div>
            <div className="cards section">
              <AgentSectionPanel title="리스크 게이지" description="피크 대비 드로다운 · 그로스 노출 · 실현 손익">
                <div className="matrix"><table><tbody>
                  <tr><td>드로다운</td><td className={`r num ${b.drawdown <= b.limits.dd_halve ? 'down' : ''}`}>{pct(b.drawdown)}</td><td className="muted">피크 {amt(b.peak)} · 한도 {pct(b.limits.dd_halve, 0)} 축소 / {pct(b.limits.dd_stop, 0)} 정지</td></tr>
                  <tr><td>그로스 노출</td><td className="r num">{pct(b.gross_exposure).replace('+', '')}</td><td className="muted">한도 {pct(b.limits.max_gross_pct, 0).replace('+', '')}</td></tr>
                  <tr><td>실현 손익</td><td className="r num">{amt(b.realized_pnl)}</td><td className="muted">시작 자본 {amt(b.initial_cash)}</td></tr>
                  <tr><td>국면 ({info?.benchmark_name_ko ?? '지수'} vs 200일선)</td><td className="r">{b.regime_on == null ? '–' : b.regime_on ? '매수 가능' : '신규 매수 없음'}</td><td className="muted">섹터 노출: 섹터 정보 미연결</td></tr>
                </tbody></table></div>
              </AgentSectionPanel>
              <AgentSectionPanel title="Protections" description="StoplossGuard · MaxDrawdown · Cooldown · LowProfit 상태와 해제일">
                <div className="matrix"><table><tbody>
                  {b.protections.map((g) => (
                    <tr key={g.id}>
                      <td className="num">{g.id}</td><td className={g.active ? 'down' : 'muted'}>{g.active ? '발동' : '정상'}</td>
                      <td className="wrap muted">{g.detail ?? ''}{g.tickers.length > 0 ? ` · ${g.tickers.map((t) => `${t.ticker}(${t.until ?? '–'}까지)`).join(', ')}` : ''}{g.until ? ` · ${g.until}까지` : ''}</td>
                    </tr>
                  ))}
                </tbody></table></div>
              </AgentSectionPanel>
            </div>
            <div className="section">
              <AgentSectionPanel title="청산 사유 분포 · 청산 기록" description={Object.entries(b.exits_by_reason_ko).map(([k, n]) => `${k} ${n}건`).join(' · ') || '청산 없음'}>
                {b.closed.length > 0 && (
                  <div className="matrix"><table>
                    <thead><tr><th>종목</th><th>진입</th><th>청산</th><th className="r">수량</th><th className="r">진입가</th><th className="r">청산가</th><th className="r">손익</th><th className="r">수익률</th></tr></thead>
                    <tbody>{b.closed.map((t) => (
                      <tr key={t.ticker + t.exit_date}>
                        <td><TickerCell ticker={t.ticker} name={t.name} /></td><td className="num">{t.entry_date}</td><td className="num">{t.exit_date}</td>
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
