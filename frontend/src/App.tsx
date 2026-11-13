import { NavLink, Route, Routes } from 'react-router-dom'
import { AgentChip } from '@bv-ds/ui'

import { useApi } from './hooks/useApi'
import { useLabels } from './hooks/useLabels'
import { useMarket, useMarketPath } from './hooks/useMarket'
import { useTradingState } from './hooks/useTradingState'
import { api, utc } from './api/client'
import { MarketToggle, ModeBadge, SessionChip } from './components/common'
import { KillSwitch } from './components/KillSwitch'
import { ProtectionsChip } from './components/parts'
import Home from './pages/Home'
import Screens from './pages/Screens'
import ScreenDetail from './pages/ScreenDetail'
import Decisions from './pages/Decisions'
import Ticker from './pages/Ticker'
import Performance from './pages/Performance'
import Positions from './pages/Positions'
import Settings from './pages/Settings'
import Paper from './pages/Paper'

// Screen plan docs/02 v0.3 §1.
const NAV = [
  { to: '/', label: '오늘 요약' },
  { to: '/screens', label: '종목 선별' },
  { to: '/decisions', label: '결정·실행', badge: 'exceptions' },
  { to: '/tickers', label: '종목 상세' },
  { to: '/positions', label: '포지션·리스크' },
  { to: '/performance', label: '성과·백테스트' },
  { to: '/paper', label: '모의투자 검증', badge: 'paper' },
  { to: '/settings', label: '설정·감사 로그' },
]

export default function App() {
  const [market] = useMarket()
  const to = useMarketPath()
  const labels = useLabels()
  const info = labels.data?.markets[market]
  const health = useApi(() => api.health(market), [market])
  const screens = useApi(() => api.screens(market), [market])
  const dates = useApi(() => api.decisionDates(market), [market])
  const exceptions = useApi(() => api.exceptions(market), [market])
  const book = useApi(() => api.positions(market), [market])
  const gate = useApi(() => api.paperProgress(market), [market])
  const mode = useTradingState(market)
  const latest = screens.data?.[0]
  const nException = exceptions.data?.length ?? 0

  return (
    <div className="app">
      <nav className="rail" aria-label="화면">
        <div className="brand">
          <strong>에이전트 운용 데스크</strong>
          <span className="muted">{health.data ? `core ${health.data.core_version} · ${health.data.market_ko} 장부` : '연결 중'}</span>
        </div>
        <ul className="nav">
          {NAV.map((n) => (
            <li key={n.to}>
              <NavLink to={to(n.to)} end={n.to === '/'} className={({ isActive }) => (isActive ? 'active' : '')}>
                <span>{n.label}</span>
                {n.badge === 'exceptions' && nException > 0 && <span className="nav-badge num" aria-label={`예외 ${nException}건`}>예외 {nException}</span>}
                {n.badge === 'paper' && gate.data && <span className="nav-count num" aria-label={`모의 운용 ${gate.data.weeks_badge_ko}`}>{gate.data.weeks_badge_ko}</span>}
              </NavLink>
            </li>
          ))}
        </ul>
        <p className="legend">
          색 규칙 <span className="up">빨강 = 상승·매수</span> · <span className="down">파랑 = 하락·매도</span> (국내 관례, 두 시장 공통)
        </p>
      </nav>
      <main className="main">
        <div className="statusbar">
          <MarketToggle />
          <SessionChip info={info} />
          <span>기준일 <b className="num">{latest?.as_of ?? '–'}</b></span>
          <span>마지막 선별 <b className="num">{utc(latest?.recorded_at)}</b></span>
          <span>마지막 결정 <b className="num">{dates.data?.[0] ?? '–'}</b></span>
          <ProtectionsChip active={book.data?.protections_active ?? null} failed={!!book.error} to={to('/positions')} />
          <span className="statusbar-right">
            <ModeBadge state={mode.data} />
            <KillSwitch state={mode.data} market={market} />
          </span>
          {health.error && <AgentChip variant="hot">API 연결 실패: {health.error}</AgentChip>}
        </div>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/screens" element={<Screens />} />
          <Route path="/screens/:runId" element={<ScreenDetail />} />
          <Route path="/decisions" element={<Decisions />} />
          <Route path="/tickers" element={<Ticker />} />
          <Route path="/tickers/:ticker" element={<Ticker />} />
          <Route path="/performance" element={<Performance />} />
          <Route path="/positions" element={<Positions />} />
          <Route path="/paper" element={<Paper />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  )
}
