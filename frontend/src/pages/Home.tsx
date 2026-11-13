import { Link, useNavigate } from 'react-router-dom'
import { AgentAlert, AgentKpiCard, AgentSectionPanel, Grid } from '@bv-ds/ui'

import { api, utc, won } from '../api/client'
import { useApi } from '../hooks/useApi'
import { ExecStatus, Loaded, Pct, RatingBadge, TickerCell } from '../components/common'

const KPIS = [
  { label: '평가 금액', hint: '현금 + 보유 평가액' },
  { label: '오늘 실현 손익', hint: '당일 체결 기준' },
  { label: '미실현 손익', hint: '보유 평단 대비' },
  { label: '드로다운', hint: '피크 대비 · 한도 −10% 축소 / −20% 정지' },
  { label: '그로스 노출 · 현금', hint: '포트폴리오 대비' },
  { label: '오늘 주문 금액', hint: '자동 실행 일일 상한 대비' },
]

const PROTECTIONS = ['StoplossGuard', 'MaxDrawdown', 'Cooldown', 'LowProfit']

/** 1 (v0.3): portfolio state, what ran automatically today, and the exceptions a human must look at.
 *  Portfolio KPIs, risk gauges and Protections come from the positions layer, which answers 501 until it exists;
 *  those panels say so instead of showing zeros. */
export default function Home() {
  const navigate = useNavigate()
  const dates = useApi(() => api.decisionDates(), [])
  const latestDate = dates.data?.[0]
  const today = useApi(() => (latestDate ? api.decisions({ date: latestDate }) : Promise.resolve([])), [latestDate])
  const orders = useApi(() => (latestDate ? api.orders({ date: latestDate }) : Promise.resolve([])), [latestDate])
  const exceptions = useApi(() => api.exceptions(), [])
  const screens = useApi(() => api.screens(), [])
  const perf = useApi(() => api.performance(), [])
  const positions = useApi(() => fetch('/api/positions').then((r) => r.json() as Promise<{ message: string }>), [])
  const latest = screens.data?.[0]
  const first = exceptions.data?.[0]
  const nOrders = (s: string) => orders.data?.filter((o) => o.status === s).length ?? 0

  return (
    <>
      <h1 className="page-title">오늘 요약</h1>
      <p className="page-sub">지금 포트폴리오가 어떤 상태이고, 시스템이 오늘 무엇을 자동으로 실행했고, 사람이 봐야 할 예외가 있는지. 숫자는 전부 기록에서 읽고 화면에서 재계산하지 않습니다.</p>

      <Grid columns={6} gap="1.2rem">
        {KPIS.map((k) => <AgentKpiCard key={k.label} label={k.label} value="–" delta={positions.data ? '포지션 레이어 미연결' : k.hint} />)}
      </Grid>

      {exceptions.data && exceptions.data.length > 0 && first && (
        <div className="section">
          <AgentAlert tone="error" title={`예외 ${exceptions.data.length}건 확인 필요`}>
            <b className="num">{first.ticker}</b> {first.name} · {first.summary} ({first.kind_ko}).{' '}
            <Link to="/decisions?tab=exceptions">예외 큐 열기 →</Link>
          </AgentAlert>
        </div>
      )}

      <div className="section">
        <AgentSectionPanel title="오늘 자동 실행"
          description={latestDate ? `결정 ${latestDate} → 다음 거래일 09:00 시가 주문 · 선별 ${latest ? `${latest.universe_size} → ${latest.selected_count}` : '–'} · 결정 ${today.data?.length ?? '–'} · 체결 ${nOrders('filled')} · 차단 ${nOrders('blocked')} · 예외 ${exceptions.data?.length ?? '–'}` : undefined}>
          <Loaded state={orders} empty="오늘 주문 기록이 없습니다"
            emptyDescription="실행 레이어가 연결되면 결정마다 시각·방향·수량·체결가·상태·적용 룰이 여기에 기록됩니다. 지금은 결정만 있고 주문은 없습니다.">
            {(rows) => (
              <div className="matrix"><table>
                <thead><tr><th>시각</th><th>종목</th><th>방향</th><th className="r">수량</th><th className="r">체결가</th><th className="r">금액</th><th>상태</th><th>사유 · 적용 룰</th></tr></thead>
                <tbody>{rows.map((o) => (
                  <tr key={o.order_id} className="click" onClick={() => navigate(`/tickers/${encodeURIComponent(o.ticker)}`)}>
                    <td className="num">{utc(o.placed_at)}</td><td><TickerCell ticker={o.ticker} name={o.name} /></td>
                    <td className={o.side === 'Buy' ? 'up' : 'down'}>{o.side_ko}</td>
                    <td className="r num">{o.filled_quantity || o.quantity}</td><td className="r num">{won(o.filled_price)}</td><td className="r num">{won(o.amount)}</td>
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
        <AgentSectionPanel title="리스크 한도" description="드로다운 · 일일 손실 · 오늘 주문 금액 · 섹터 노출을 한도선과 함께" action={<Link to="/positions">포지션·리스크 →</Link>}>
          <p className="muted">한도를 넘으면 자동 매매가 신규 주문을 멈추고 예외 큐와 알림으로 알립니다.</p>
          <AgentAlert tone="info">{positions.data?.message ?? '포지션·리스크 레이어 연결 전입니다. 게이지는 그 레이어가 값을 주면 채워집니다.'}</AgentAlert>
        </AgentSectionPanel>
        <AgentSectionPanel title="Protections · 손절 근접" description="4종 상태와 손절선 2% 이내 종목 수">
          <div className="matrix"><table><tbody>
            {PROTECTIONS.map((p) => <tr key={p}><td className="num">{p}</td><td className="muted">미연결</td></tr>)}
            <tr><td>손절선 2% 이내</td><td className="muted">–</td></tr>
          </tbody></table></div>
        </AgentSectionPanel>
      </div>

      <div className="section">
        <AgentSectionPanel title="최근 정산" description={perf.data ? `실측 · ${perf.data.holding} · KOSPI 대비` : undefined} action={<Link to="/performance">등급별 성과·알파 추이 →</Link>}>
          <Loaded state={perf}>
            {(p) => (
              <div className="matrix"><table>
                <thead><tr><th>종목</th><th>결정일</th><th>등급</th><th className="r">실현</th><th className="r">알파</th><th>정산일</th></tr></thead>
                <tbody>{p.reflections.slice(0, 5).map((r) => (
                  <tr key={r.ticker + r.date} className="click" onClick={() => navigate(`/tickers/${encodeURIComponent(r.ticker)}`)}>
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
