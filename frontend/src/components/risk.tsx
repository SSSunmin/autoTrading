import { useState } from 'react'
import {
  AgentAlert, AgentButton, AgentCheckbox, AgentModal, AgentRadio, AgentRadioGroup, AgentSpinner, AgentTextField,
} from '@bv-ds/ui'

import { announceStateChange, api, money, pct, utc } from '../api/client'
import type { BookEvents, Market, MarketSummary, Order, ProtectionState, ResumePreview, ResumeResult, TradingState } from '../api/types'
import { useApi } from '../hooks/useApi'
import { useLabels } from '../hooks/useLabels'
import { ExitReasonBadge } from './parts'
import { exitSegments, fillTimeLabel, haltWho, previewFresh, resumeOffer, resumeReady } from './riskLogic'

const MARKET_KO: Record<Market, string> = { KRX: '국내', US: '미국' }

/** Home band while the market is halted (mockup MainHalted 정지 상태): who and why, scope, exits, and either when it
 *  lifts itself (daily loss) or the resume. */
export function HaltBand({ state, market }: { state: TradingState; market: Market }) {
  if (state.running) return null
  const who = haltWho(state)
  return (
    <section className="halt-band section" aria-label="정지 상태">
      <div className="hb-head"><b>● 자동 실행 정지 · {who.text}</b><span className="muted num">{utc(state.halted_at)}</span></div>
      <dl className="hb-dl">
        <div><dt>사유</dt><dd>{state.halt_reason ?? '–'}</dd></div>
        <div><dt>범위</dt><dd>{state.halt_scope_ko ?? '–'}</dd></div>
        <div><dt>청산</dt><dd>{state.keep_exits ? 'keep_exits 켬 · 손절·트레일링 계속' : 'keep_exits 끔 · 손절·트레일링도 멈춤'}</dd></div>
      </dl>
      <div className="hb-act">
        <ResumeButton state={state} market={market}
          label={state.auto_resume_label_ko ? `${state.auto_resume_label_ko} 자동 해제 · 재가동 불필요` : undefined} />
      </div>
    </section>
  )
}

/** "재가동 (관리자) →" and its modal; a fresh form every time it opens. A daily-loss halt's button reads when it lifts
 *  (``label``) and opens the notice-only variant. */
export function ResumeButton({ state, market, label }: { state: TradingState; market: Market; label?: string }) {
  const [open, setOpen] = useState(false)
  const [opened, setOpened] = useState(0)
  return (
    <>
      <AgentButton size="sm" variant={label ? 'ghost' : 'secondary'} onClick={() => { setOpened((n) => n + 1); setOpen(true) }}>{label ?? '재가동 (관리자) →'}</AgentButton>
      <ResumeModal key={opened} state={state} market={market} open={open} onClose={() => setOpen(false)} />
    </>
  )
}

type Form = { scope: 'all' | 'market'; resetPeak: boolean; operator: string; reason: string; token: string }

/** Resume confirm modal (mockup Resume): the variant comes from the server's preview — a person's halt, a daily-loss
 *  halt (notice only, no resume), the −20% stop (peak reset on by default, three lines from the server). */
export function ResumeModal({ state, market, open, onClose }: { state: TradingState; market: Market; open: boolean; onClose: () => void }) {
  const [form, setForm] = useState<Form>({ scope: state.halt_scope === 'all' ? 'all' : 'market', resetPeak: true, operator: '', reason: '', token: '' })
  const preview = useApi(() => (open ? api.resumePreview(market, form.resetPeak) : Promise.resolve(null)), [open, market, form.resetPeak])
  // the last preview stays on screen while the checkbox reloads it, so the modal does not jump (derived state, set in render)
  const [held, setHeld] = useState<ResumePreview | null>(null)
  if (preview.data && preview.data !== held) setHeld(preview.data)
  const markets = useApi(() => (open ? api.markets() : Promise.resolve([])), [open])
  const others = (markets.data ?? []).filter((m) => m.market !== market && m.state && !m.state.running)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const p = held
  const offer = p ? resumeOffer(p, form.scope === 'all', others.filter((m) => m.state?.dd_stop).length, form.resetPeak) : null
  const ready = !!p && !!offer?.canResume && previewFresh(p, form.resetPeak) && resumeReady({ ...form, tokenRequired: state.admin_token_required }) && !busy
  // the state reload unmounts this modal (the halt band goes away), so it waits until the result has been read
  const close = () => { if (done) announceStateChange(); onClose() }
  const submit = async () => {
    if (!offer) return
    setBusy(true); setError(null)
    try {
      const r = await api.resume({ operator: form.operator.trim(), reason: form.reason.trim(), scope: form.scope === 'all' ? 'all' : market,
        reset_peak: offer.resetPeak }, form.token || undefined)
      setDone(resumeSummary(r))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <AgentModal open={open} onClose={close} title="자동 실행을 다시 켤까요?"
      description="재가동은 관리자만 합니다. 정지 기록과 재가동 사유는 감사 로그에 남습니다."
      actions={offer?.canResume && !done ? (
        <>
          <AgentButton variant="ghost" onClick={close} disabled={busy}>취소</AgentButton>
          <AgentButton variant="primary" onClick={submit} disabled={!ready}>{form.scope === 'all' ? '전체 시장 재가동' : `${MARKET_KO[market]}만 재가동`}</AgentButton>
        </>
      ) : <AgentButton variant="primary" onClick={close}>닫기</AgentButton>}>
      {/* nothing while closed: the modal keeps its children in the page, and a closed one has no preview to wait for */}
      {!open ? null : done ? <AgentAlert tone="success" title="재가동됨">{done}</AgentAlert>
        : p ? <ResumeBody state={state} market={market} preview={p} others={others} form={form}
          set={(patch) => setForm((f) => ({ ...f, ...patch }))} error={error} previewError={preview.error} />
          : <div style={{ padding: '2.4rem' }}>{preview.error ? <AgentAlert tone="error" title="미리보기 실패">{preview.error}</AgentAlert> : <AgentSpinner />}</div>}
    </AgentModal>
  )
}

/** "미국 재가동 / 미국 피크 재설정 $… (… 종가) / 국내 건너뜀(이미 실행 중)" from the server's answer. */
function resumeSummary(r: ResumeResult): string {
  const resets = Object.entries(r.peak_resets).map(([m, x]) => `${MARKET_KO[m as Market]} 피크 재설정 ${money(x.to, m as Market)}(${x.at} 종가)`)
  return `${r.scope_ko}: ${[...r.resumed.map((m) => `${MARKET_KO[m]} 재가동`), ...resets, ...r.skipped.map((x) => `${MARKET_KO[x.market]} 건너뜀(${x.reason})`)].join(' / ')}`
}

/** The modal's content, apart so the tests render each variant. */
export function ResumeBody({ state, market, preview: p, others, form, set, error, previewError = null }: {
  state: TradingState; market: Market; preview: ResumePreview; others: MarketSummary[]; form: Form
  set: (patch: Partial<Form>) => void; error: string | null; previewError?: string | null
}) {
  const who = haltWho(state)
  const offer = resumeOffer(p, form.scope === 'all', others.filter((m) => m.state?.dd_stop).length, form.resetPeak)
  const amt = (v: number | null | undefined) => money(v, market)
  const reset = offer.resetPeak
  const old = p.lines[0]?.vs_equity
  const fresh = previewFresh(p, form.resetPeak)
  return (
    <div className="stack-gap" style={{ gap: '1rem' }}>
      <dl className="rs-dl">
        <div><dt>정지 시각</dt><dd className="num">{utc(state.halted_at)}</dd></div>
        <div><dt>주체</dt><dd><span className={`who ${who.kind}`}>{who.kind === 'system' ? '시스템' : '사람'}</span> {who.text}</dd></div>
        <div><dt>범위</dt><dd>{state.halt_scope_ko ?? '–'}</dd></div>
        <div><dt>청산</dt><dd>{state.keep_exits ? '켬 · 손절·트레일링 계속' : '끔 · 손절·트레일링도 멈춤'}</dd></div>
        <div><dt>사유</dt><dd>{state.halt_reason ?? '–'}</dd></div>
      </dl>
      {p.variant === 'daily_loss' && (
        <AgentAlert tone="info" title={`${state.auto_resume_label_ko ?? '다음 거래일 개장'}에 자동 해제됩니다`}>
          일일 손실 자동 정지는 다음 거래일 개장 때 스스로 풀리므로 여기서 다시 켜지 않습니다 — 사람 정지와 −20% 정지만 재가동합니다.
        </AgentAlert>
      )}
      {p.variant === 'running' && <AgentAlert tone="info" title="이미 실행 중입니다">정지가 풀렸습니다. 다시 켤 것이 없습니다.</AgentAlert>}
      {offer.canResume && (
        <>
          <AgentRadioGroup label="재가동 범위" value={form.scope} onChange={(e) => set({ scope: e.target.value as Form['scope'] })}>
            <AgentRadio value="all" label="전체 시장 (국내 · 미국 — 정지된 시장만 다시 켭니다)" />
            <AgentRadio value="market" label={`이 시장만 (${MARKET_KO[market]}) — 다른 시장은 그대로 둡니다`} />
          </AgentRadioGroup>
          {form.scope === 'all' && others.length > 0 && (
            <ul className="effects">
              {others.map((m) => (
                <li key={m.market}>{m.label_ko}: {m.state?.dd_stop ? '−20% 정지 — 아래 피크 재설정 선택이 같이 적용됩니다'
                  : m.state?.auto_resume_label_ko ? `일일 손실 정지 — ${m.state.auto_resume_label_ko} 자동 해제 전에 지금 함께 풀립니다`
                    : '운영자 정지 — 함께 다시 켭니다'}</li>
              ))}
            </ul>
          )}
          {offer.showReset && (
            <AgentCheckbox label="드로다운 피크를 지금 평가액으로 재설정" checked={form.resetPeak} onChange={(e) => set({ resetPeak: e.target.checked })}
              description={p.variant !== 'dd_stop' ? '−20% 정지가 선 다른 시장에 적용됩니다' : !fresh ? '미리보기를 다시 읽는 중'
                : reset ? `새 피크 ${amt(p.base)} · 반감·정지 기준이 지금 평가액에서 다시 시작합니다`
                  : `끄면 옛 피크 ${amt(p.peak_before)} 기준으로 지금 이미 ${pct(old)}${p.restops ? '라 재가동 직후 다시 정지됩니다' : '입니다'}`} />
          )}
          {offer.showTable && !fresh && (previewError
            ? <AgentAlert tone="error" title="미리보기 실패">{previewError}</AgentAlert> : <AgentSpinner />)}
          {offer.showTable && fresh && (
            <div className="matrix rs-preview"><table>
              <caption className="muted">{p.as_of} 종가 평가액 {amt(p.equity)} 기준</caption>
              <thead><tr><th>기준</th><th className="r">{reset ? '재설정 후 기준 금액' : '옛 피크 기준 금액'}</th><th className="r">지금 평가액 대비</th></tr></thead>
              <tbody>{p.lines.map((l) => (
                <tr key={l.id} className={l.id === 'dd_stop' && p.restops ? 'alert-t' : undefined}>
                  <td>{l.label_ko}</td><td className="r num">{amt(l.amount)}</td><td className="r num">{pct(l.vs_equity)}</td>
                </tr>
              ))}</tbody>
            </table></div>
          )}
          <AgentTextField label="관리자" size="sm" placeholder="이름 또는 ID" value={form.operator} onChange={(e) => set({ operator: e.target.value })} />
          <AgentTextField label="재가동 사유" size="sm" placeholder="감사 로그에 남습니다" value={form.reason} onChange={(e) => set({ reason: e.target.value })} />
          {state.admin_token_required && <AgentTextField label="관리자 토큰" size="sm" type="password" value={form.token} onChange={(e) => set({ token: e.target.value })} />}
        </>
      )}
      {error && <AgentAlert tone="error" title="재가동 실패">{error}</AgentAlert>}
    </div>
  )
}

/** Exit reasons as one stacked horizontal bar with a legend (mockup Positions 청산 사유 분포): widths are the server's
 *  counts over their sum (drawing only); the legend shows the counts, no share worked out on screen. */
export function ExitBar({ exits, labels }: { exits: Record<string, number>; labels: Record<string, string> | undefined }) {
  const segs = exitSegments(exits)
  if (segs.length === 0) return <p className="muted">청산 없음</p>
  const name = (r: string) => labels?.[r] ?? r
  return (
    <div className="xbar-wrap">
      <div className="xbar" role="img" aria-label={`청산 사유 분포: ${segs.map((s) => `${name(s.reason)} ${s.n}건`).join(', ')}`}>
        {segs.map((s) => <i key={s.reason} className={`xseg ${s.reason}`} style={{ width: `${s.share * 100}%` }} />)}
      </div>
      <ul className="xlegend">
        {segs.map((s) => (
          <li key={s.reason}><i className={`xsw ${s.reason}`} aria-hidden="true" /><ExitReasonBadge reason={s.reason} labels={labels} />
            <span className="mono muted">{s.reason}</span><b className="tnum">{s.n}건</b></li>
        ))}
      </ul>
    </div>
  )
}

/** The four Protections as cards: state, until when, and the triggers inside its own window. */
export function ProtectionCards({ items }: { items: ProtectionState[] }) {
  return (
    <div className="pcards">
      {items.map((g) => (
        <div key={g.id} className={`pcard ${g.active ? 'on' : ''}`}>
          <div className="pc-head"><b className="mono">{g.id}</b><span className={g.active ? 'alert-t' : 'muted'}>{g.active ? '● 발동' : '○ 정상'}</span></div>
          <div className="muted">{g.label_ko}</div>
          <div className="pc-meta num">해제 {g.until ?? (g.tickers.length ? g.tickers.map((t) => `${t.ticker} ${t.until ?? '–'}`).join(', ') : '–')} · lookback {g.lookback_days}거래일 내 트리거 {g.lookback_hits}건</div>
          {g.detail && <div className="pc-detail wrap">{g.detail}</div>}
        </div>
      ))}
    </div>
  )
}

/** Guard / lock history rows (mockup Positions 가드·잠금 이력). */
export function EventList({ data }: { data: BookEvents }) {
  if (data.events.length === 0) return <p className="muted">가드·잠금 이력이 없습니다.</p>
  return (
    <>
      <ul className="evlist">
        {data.events.map((e, i) => (
          <li key={`${e.date}-${i}`}><span className="num muted">{e.date}</span><span className={`evk ${e.kind}`}>{e.kind_ko}</span>
            <span className="mono wrap">{e.name ? `${e.name} · ` : ''}{e.text}</span></li>
        ))}
      </ul>
      {data.total > data.events.length && <p className="muted">최근 {data.events.length}건 · 전체 {data.total}건</p>}
    </>
  )
}

/** Fill time for one order in both zones (KRX: KST only). */
export function FillTime({ o, market }: { o: Order; market: Market }) {
  const tz = useLabels().data?.markets[market]?.timezone
  return <>{fillTimeLabel(o, market, tz) ?? o.fill_date ?? '–'}</>
}

/** Today's-orders panel action while halted (mockup MainHalted). */
export function HaltedChip({ state }: { state: TradingState }) {
  return <span className="st-halt-chip">{state.keep_exits ? '정지 중 · 청산 매도만 실행' : '정지 중 · 주문 없음'}</span>
}
