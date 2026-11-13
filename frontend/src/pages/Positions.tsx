import { AgentAlert, AgentSectionPanel } from '@bv-ds/ui'

import { api } from '../api/client'
import { useApi } from '../hooks/useApi'

/** 5: served by the execution layer later. The API answers 501 until then; the panels name what will appear. */
export default function Positions() {
  const state = useApi(() => api.health().then(() => fetch('/api/positions').then((r) => r.json())), [])
  return (
    <>
      <h1 className="page-title">포지션·리스크</h1>
      <p className="page-sub">실행 레이어와 하드 룰 레이어가 연결되면 채워집니다. 기획서 2차 범위.</p>
      {state.data && <AgentAlert tone="info">{(state.data as { message?: string }).message}</AgentAlert>}
      <div className="cards section">
        <AgentSectionPanel title="보유 표" description="수량·평단·현재가·미실현·손절선·트레일링 고점·보유일·만료일" />
        <AgentSectionPanel title="리스크 게이지" description="포트폴리오 드로다운(피크 대비), 그로스 노출, 섹터별 노출 vs 20% 한도" />
        <AgentSectionPanel title="Protections" description="StoplossGuard · MaxDrawdown · Cooldown · LowProfit 상태와 해제 시각" />
        <AgentSectionPanel title="청산 사유 분포" description="roi / stop_loss / trailing / exit_signal / force / expired" />
      </div>
    </>
  )
}
