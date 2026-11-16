import { useState } from 'react'
import { AgentAlert, AgentButton, AgentChip, AgentModal, AgentTextField } from '@bv-ds/ui'

import { announceStateChange, api, money } from '../api/client'
import type { Decision, ExceptionAction, ExceptionItem, Intervention, Market, OrderPlan } from '../api/types'
import {
  MODAL_TITLE, SOURCE_VARIANT, actionPreview, auditLine, modalActions, modalVariant, planRows, splitInterventions,
  type ModalVariant,
} from './decisionLogic'
import { resumeReady } from './riskLogic'

/** The decision's source: 에이전트 결정 / 규칙 청산 / 규칙 베이스라인 / 사람 처리 (server label). */
export function SourceBadge({ d }: { d: Pick<Decision, 'source_kind' | 'source_kind_ko'> }) {
  return <AgentChip variant={SOURCE_VARIANT[d.source_kind]}>{d.source_kind_ko}</AgentChip>
}

/** 원안 (에이전트) vs 최종 (룰 적용 후), the cells that differ highlighted (mockup Decisions §2열 비교). */
export function PlanGrid({ plan, market }: { plan: OrderPlan; market: Market }) {
  return (
    <table className="plan-grid" aria-label="원안 대 최종 비교">
      <thead><tr><th scope="col" /><th scope="col">원안 (에이전트)</th><th scope="col">최종 (룰 적용 후)</th></tr></thead>
      <tbody>
        {planRows(plan, (v) => money(v, market)).map((r) => (
          <tr key={r.label}>
            <th scope="row">{r.label}</th>
            <td className="num">{r.a}</td>
            <td className={`num${r.diff ? ' diff' : ''}`}>{r.b}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** Cuts as chips ("max_position_pct 12.5% → 10.0%"), locks as their own text line. */
export function Interventions({ ivs }: { ivs: Intervention[] }) {
  const { cuts, locks } = splitInterventions(ivs)
  if (!ivs.length) return null
  return (
    <div className="ivs">
      {cuts.length > 0 && (
        <div className="ivs-cuts"><span className="muted">개입</span>
          {cuts.map((i) => <AgentChip key={i.note} variant="hot" title={i.note}>{i.rule} {i.before} → {i.after}</AgentChip>)}
        </div>
      )}
      {locks.map((i) => <div key={i.note} className="ivs-lock"><span className="muted">잠금</span> {i.note}</div>)}
    </div>
  )
}

/** The exception's buttons (REVIEW: 재실행 · 보유로 처리 · 무시; an order: 처리 → 다음 세션 재시도 · 무시), each opening
 *  the confirm modal. Nothing to act on until the executor has written the item's order. */
export function ExceptionButtons({ item, tokenRequired }: { item: ExceptionItem; tokenRequired: boolean }) {
  const [open, setOpen] = useState<ModalVariant | null>(null)
  const [opened, setOpened] = useState(0)
  const show = (v: ModalVariant) => { setOpened((n) => n + 1); setOpen(v) }
  const off = item.order_id == null
  const title = off ? '실행기가 이 결정을 아직 처리하지 않았습니다' : undefined
  return (
    <>
      {item.pending_action_ko && <AgentChip variant="new">{item.pending_action_ko}</AgentChip>}
      {item.kind === 'review' ? item.actions.length > 0 ? item.actions.map((a) => (
        <AgentButton key={a.action} variant={a.action === 'dismiss' ? 'ghost' : 'secondary'} disabled={!a.available}
          title={a.available ? undefined : `상한 ${a.limit}회 도달`} onClick={() => show(modalVariant(item, a.action))}>
          {a.label_ko}{a.used != null && a.used > 0 ? ` ${a.used}/${a.limit}` : ''}
        </AgentButton>
      )) : <AgentButton variant="secondary" disabled title={title}>재실행 · 보유로 처리 · 무시</AgentButton>
        : <AgentButton variant="secondary" disabled={off} title={title} onClick={() => show('order')}>처리 (다음 세션 재시도 · 무시)</AgentButton>}
      {open && <ExceptionModal key={opened} item={item} variant={open} tokenRequired={tokenRequired} onClose={() => setOpen(null)} />}
    </>
  )
}

type Form = { operator: string; reason: string; token: string }

/** The confirm modal (mockup ExceptionAction): what happens, the audit line, a required reason. On success it shows
 *  the server's result and reloads the pages on close. */
export function ExceptionModal({ item, variant, tokenRequired, onClose }: {
  item: ExceptionItem; variant: ModalVariant; tokenRequired: boolean; onClose: () => void
}) {
  const [form, setForm] = useState<Form>({ operator: '', reason: '', token: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const buttons = modalActions(item, variant)
  const ready = resumeReady({ ...form, tokenRequired }) && !busy && item.order_id != null
  const close = () => { if (done) announceStateChange(); onClose() }
  const submit = async (action: ExceptionAction) => {
    if (!item.order_id) return
    setBusy(true); setError(null)
    try {
      const r = await api.exceptionAction(item.market, item.order_id, { action, operator: form.operator.trim(), reason: form.reason.trim() },
        form.token || undefined)
      setDone(`${r.action_ko}: ${r.result_ko}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <AgentModal open onClose={close} title={MODAL_TITLE[variant]} description="사유 필수 · 감사 로그에 남습니다 · 권한: 관리자 토큰(운영자 역할 분리는 후속)"
      actions={done ? <AgentButton variant="primary" onClick={close}>닫기</AgentButton> : (
        <>
          <AgentButton variant="ghost" onClick={close} disabled={busy}>취소</AgentButton>
          {buttons.map((b) => (
            <AgentButton key={b.action} variant={b.action === 'dismiss' || b.action === 'dismiss_order' ? 'secondary' : 'primary'}
              disabled={!ready || !b.available} onClick={() => submit(b.action)}>{b.label_ko}</AgentButton>
          ))}
        </>
      )}>
      {done ? <AgentAlert tone="success" title="처리됨">{done}</AgentAlert>
        : <ExceptionBody item={item} variant={variant} form={form} set={(patch) => setForm((f) => ({ ...f, ...patch }))}
          tokenRequired={tokenRequired} error={error} />}
    </AgentModal>
  )
}

/** The modal's content, apart so the tests render each variant before and after the reason. */
export function ExceptionBody({ item, variant, form, set, tokenRequired, error }: {
  item: ExceptionItem; variant: ModalVariant; form: Form; set: (patch: Partial<Form>) => void; tokenRequired: boolean; error: string | null
}) {
  const action: ExceptionAction = variant === 'order' ? 'retry_next' : variant
  return (
    <div className="xa">
      <div className="xa-head"><b>{item.name ?? item.ticker}</b> <span className="muted num">{item.ticker}</span>
        <span className="xa-kind">⚠ {item.kind_ko}</span></div>
      <div className="muted">결정일 {item.trade_date} · {item.summary}</div>
      {item.detail && <pre className="raw">{item.detail}</pre>}
      <div className="xa-sec"><b>처리 결과</b>
        <ul>{actionPreview(item, variant).map((t) => <li key={t}>{t}</li>)}</ul>
      </div>
      <div className="xa-sec"><b>감사 로그에 남는 문구</b><code className="xa-log">{auditLine(item, action, form.operator, form.reason)}</code></div>
      <AgentTextField label="운영자" size="sm" placeholder="이름 또는 ID" value={form.operator} onChange={(e) => set({ operator: e.target.value })} />
      <AgentTextField label="처리 사유 (필수)" size="sm" placeholder="감사 로그에 남습니다" value={form.reason} onChange={(e) => set({ reason: e.target.value })} />
      {tokenRequired && <AgentTextField label="관리자 토큰" size="sm" type="password" value={form.token} onChange={(e) => set({ token: e.target.value })} />}
      {!form.reason.trim() && <div className="muted xa-need">사유를 적어야 처리 버튼이 켜집니다</div>}
      {error && <AgentAlert tone="error" title="처리 실패">{error}</AgentAlert>}
    </div>
  )
}
