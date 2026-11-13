import { useState } from 'react'
import { AgentAlert, AgentButton, AgentCheckbox, AgentModal, AgentRadio, AgentRadioGroup, AgentTextarea, AgentTextField } from '@bv-ds/ui'

import { announceStateChange, api } from '../api/client'
import type { Market, TradingState } from '../api/types'

const MARKET_KO: Record<Market, string> = { KRX: '국내', US: '미국' }

/** Emergency stop (docs/02 v0.3 화면 3, docs/08 §4 Q2): button in the status bar → confirm modal → POST /api/trading/halt.
 *  Scope: every market by default, or only the one on screen. Effects are spelled out before confirming; the reason
 *  goes to each market's operations log. Resume is admin-only, in Settings, with the same scope choice. */
export function KillSwitch({ state, market }: { state: TradingState | null; market: Market }) {
  const [open, setOpen] = useState(false)
  const [operator, setOperator] = useState('')
  const [reason, setReason] = useState('')
  const [keepExits, setKeepExits] = useState(true)
  const [scope, setScope] = useState<'all' | 'market'>('all')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const close = () => { setOpen(false); setError(null) }
  const openModal = () => { setScope('all'); setOpen(true) }
  const submit = async () => {
    setBusy(true); setError(null)
    try {
      const r = await api.halt({ operator: operator.trim(), reason: reason.trim(), keep_exits: keepExits, scope: scope === 'all' ? 'all' : market })
      // per market: what was cancelled and whether its exits still run (a skipped market keeps its own setting)
      const lines = r.results.map(({ market: m, effects: e, error }) => {
        const cancelled = e.open_orders_cancelled == null ? '미체결 취소 못 함(재가동 때 정지 전 미체결을 취소합니다)' : `미체결 취소 ${e.open_orders_cancelled}건`
        return `${MARKET_KO[m]} 정지 · ${cancelled} · 손절 청산 ${e.exits_active ? '계속 작동' : '중단'}${error ? ` · ${error}` : ''}`
      })
      const skipped = r.skipped.map((s) => `${MARKET_KO[s.market]} 건너뜀(${s.reason}) · 손절 청산 ${s.keep_exits ? '계속 작동' : '중단'}`)
      setDone(`${r.scope_ko}: ${[...lines, ...skipped].join(' / ')} · 보유 포지션 유지`)
      setOpen(false)
      announceStateChange()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <AgentButton variant="primary" size="sm" disabled={!state} onClick={openModal}
        title={state && !state.running ? `${MARKET_KO[market]}은 이미 정지된 상태입니다(${state.halt_target_ko ?? state.halted_by_ko ?? ''}). 전체 정지는 아직 실행 중인 시장만 멈춥니다.` : undefined}>
        긴급 정지
      </AgentButton>
      {done && <span className="muted">{done}</span>}
      <AgentModal open={open} onClose={close} title="자동 매매를 긴급 정지할까요?"
        description="새 주문을 즉시 멈춥니다. 다음 결정부터는 기록만 하고 주문하지 않습니다."
        actions={
          <>
            <AgentButton variant="ghost" onClick={close} disabled={busy}>취소</AgentButton>
            <AgentButton variant="primary" onClick={submit} disabled={busy || !operator.trim() || !reason.trim()}>
              {scope === 'all' ? '전체 시장 긴급 정지' : `${MARKET_KO[market]}만 긴급 정지`}
            </AgentButton>
          </>
        }>
        <div className="stack-gap" style={{ gap: '1rem' }}>
          <AgentRadioGroup label="정지 범위" value={scope} onChange={(e) => setScope(e.target.value as 'all' | 'market')}>
            <AgentRadio value="all" label="전체 시장 (국내 · 미국)" />
            <AgentRadio value="market" label={`이 시장만 (${MARKET_KO[market]}) — 다른 시장은 계속 돕니다`} />
          </AgentRadioGroup>
          <ul className="effects">
            <li>미체결 주문을 취소합니다. 건수는 실행 후 시장마다 기록에 남습니다.</li>
            <li>보유 포지션은 그대로 둡니다.</li>
            <li>다시 켜기는 관리자만 할 수 있습니다. 설정·감사 로그에서 정지 시각과 사유를 확인할 수 있습니다.</li>
          </ul>
          <AgentCheckbox label="손절·트레일링 청산은 계속 작동" description="끄면 보유 종목이 손절선을 깨도 자동으로 팔지 않습니다"
            checked={keepExits} onChange={(e) => setKeepExits(e.target.checked)} />
          <AgentTextField label="운용자" size="sm" placeholder="이름 또는 ID" value={operator} onChange={(e) => setOperator(e.target.value)} />
          <AgentTextarea label="정지 사유" size="sm" rows={3} placeholder="감사 로그에 남습니다" value={reason} onChange={(e) => setReason(e.target.value)} />
          {error && <AgentAlert tone="error" title="정지 실패">{error}</AgentAlert>}
        </div>
      </AgentModal>
    </>
  )
}
