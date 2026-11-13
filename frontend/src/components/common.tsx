import type { ReactNode } from 'react'
import { AgentAlert, AgentChip, AgentEmptyState, AgentSpinner } from '@bv-ds/ui'

import { pct } from '../api/client'
import type { ApiState } from '../hooks/useApi'
import type { FilterResult, OrderStatus, Rating, TradingState } from '../api/types'

/** Rating chip: values stay English in data, the label is the server's *_ko. KRX colors in app.css. */
export function RatingBadge({ rating, label }: { rating: Rating | 'pending'; label?: string }) {
  return <span className={`rating ${rating}`}>{label ?? rating}</span>
}

/** Instrument as the plan (v0.3) writes it everywhere: code tag on the first line, Korean name on the second. */
export function TickerCell({ ticker, name, size }: { ticker: string; name?: string | null; size?: 'lg' }) {
  return (
    <span className={`tkr ${size ?? ''}`}>
      <span className="code num">{ticker}</span>
      <span className="name">{name ?? '회사명 미확인'}</span>
    </span>
  )
}

export function Pct({ value, digits = 1 }: { value: number | null | undefined; digits?: number }) {
  const cls = value == null ? '' : value > 0 ? 'up' : value < 0 ? 'down' : ''
  return <span className={`num ${cls}`}>{pct(value, digits)}</span>
}

/** Order status as a strip: filled/partial neutral, open live, cancelled (by a halt) secondary, blocked and rejected hot. */
export function ExecStatus({ status, label }: { status: OrderStatus; label: string }) {
  const variant = status === 'filled' || status === 'partial' ? 'neutral' : status === 'open' ? 'live'
    : status === 'cancelled' ? 'secondary' : 'hot'
  return <AgentChip variant={variant}>{label}</AgentChip>
}

/** Operating mode badge for the status bar: running auto = live, halted = hot, manual/paper = labelled. */
export function ModeBadge({ state }: { state: TradingState | null }) {
  if (!state) return <AgentChip variant="neutral">운용 상태 확인 중</AgentChip>
  const variant = !state.running ? 'hot' : state.mode === 'auto' ? 'live' : state.mode === 'paper' ? 'new' : 'secondary'
  return <AgentChip variant={variant} title={state.halt_reason ?? undefined}>{state.running && state.mode === 'auto' ? '● ' : ''}{state.label_ko}</AgentChip>
}

function fmtValue(v: FilterResult['value']): string {
  if (v == null) return '–'
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(3).replace(/\.?0+$/, '')
  return String(v)
}

/** One rule verdict as "value (threshold)", green pass / red fail / grey not evaluated. */
export function RuleCell({ f, dataNote }: { f?: FilterResult; dataNote?: string }) {
  if (!f) return <span className="cell none" title={dataNote ?? '이전 규칙에서 탈락'}>{dataNote ? '데이터 없음' : '–'}</span>
  return (
    <span className={`cell ${f.passed ? 'pass' : 'fail'}`} title={f.note || undefined}>
      {fmtValue(f.value)} <span className="thr">({fmtValue(f.threshold)})</span>
    </span>
  )
}

/** Loading / error / empty wrapper so every page handles the three states the same way (docs/02 §5). */
export function Loaded<T>({ state, empty, emptyDescription, children }: {
  state: ApiState<T>; empty?: string; emptyDescription?: string; children: (data: T) => ReactNode
}) {
  if (state.loading) return <div style={{ padding: '2.4rem' }}><AgentSpinner /></div>
  if (state.error) {
    return (
      <AgentAlert tone={state.status === 501 ? 'info' : 'error'} title={state.status === 404 ? '기록 없음' : state.status === 501 ? '아직 구현되지 않은 레이어' : 'API 오류'}>
        {state.error}
      </AgentAlert>
    )
  }
  if (state.data == null || (Array.isArray(state.data) && state.data.length === 0)) {
    return <AgentEmptyState title={empty ?? '데이터가 없습니다'} description={emptyDescription ?? 'scripts/baseline_krx.py 를 실행하면 선별·결정·정산 기록이 생성됩니다.'} />
  }
  return <>{children(state.data)}</>
}

export function AlphaBar({ value, scale = 10 }: { value: number | null; scale?: number }) {
  if (value == null) return <div className="bar"><i className="axis" /></div>
  const w = Math.min(50, (Math.abs(value) * 100 / scale) * 50)
  const side = value >= 0 ? { left: '50%' } : { right: '50%' }
  return (
    <div className="bar" aria-label={pct(value, 2)}>
      <i style={{ ...side, width: `${w}%`, background: value >= 0 ? 'var(--app-up)' : 'var(--app-down)' }} />
      <i className="axis" />
    </div>
  )
}
