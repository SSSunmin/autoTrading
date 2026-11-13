import type { Market, Order, ResumePreview, TradingState } from '../api/types'
import { EXIT_REASONS } from './partsLogic'

/** The branching behind the halted home, the resume modal and the risk panels (risk.tsx, common.tsx), apart from the
 *  components so the tests read the rules directly. Every value comes from the server; nothing here recomputes one. */

export type BadgeVariant = 'hot' | 'live' | 'new' | 'secondary'

/** Status-bar mode badge. Halted: a system daily-loss halt says when it lifts (the server's market-open label), every
 *  other halt that a person must resume it — by the system (the −20% stop) or by an operator. */
export function modeBadge(state: TradingState): { variant: BadgeVariant; text: string } {
  if (state.running) {
    const variant = state.mode === 'auto' ? 'live' : state.mode === 'paper' ? 'new' : 'secondary'
    return { variant, text: `${state.mode === 'auto' ? '● ' : ''}${state.label_ko}` }
  }
  if (state.auto_resume_label_ko) return { variant: 'hot', text: `● 정지 · 시스템(일일 손실) · ${state.auto_resume_label_ko} 자동 해제` }
  const who = state.halted_by === 'system' ? `시스템(${state.dd_stop ? '−20% 드로다운' : '자동'})` : '운영자'
  return { variant: 'hot', text: `● 정지 · ${who} · 재가동 필요` }
}

/** Who halted, for the halt band and the modal: the system names what stopped it, a person is named. */
export function haltWho(state: TradingState): { kind: 'system' | 'person'; text: string } {
  return state.halted_by === 'system'
    ? { kind: 'system', text: state.halt_target_ko ?? state.halted_by_ko ?? '시스템' }
    : { kind: 'person', text: `운영자 ${state.halted_by_ko ?? state.halted_by ?? ''}`.trim() }
}

/** One segment of the exit-reason bar: the server's count and its share of all closes (the bar's width). The rule's
 *  reasons first in their fixed order, any other reason after them. */
export function exitSegments(exits: Record<string, number>): { reason: string; n: number; share: number }[] {
  const total = Object.values(exits).reduce((a, n) => a + n, 0)
  if (total <= 0) return []
  const rank = (r: string) => { const i = (EXIT_REASONS as readonly string[]).indexOf(r); return i < 0 ? EXIT_REASONS.length : i }
  return Object.entries(exits).filter(([, n]) => n > 0).sort(([a], [b]) => rank(a) - rank(b))
    .map(([reason, n]) => ({ reason, n, share: n / total }))
}

const ZONE_ABBR: Record<string, string> = { 'America/New_York': 'ET' }

function parts(iso: string, timeZone: string): { day: string; time: string } {
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
    timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(iso)).map((x) => [x.type, x.value]))
  return { day: `${p.year}-${p.month}-${p.day}`, time: `${p.hour}:${p.minute}` }
}

/** "체결 2026-07-01 22:30 KST (09:30 ET)" / "체결 예정 …" from the server's ISO times, formatted by Intl in each zone
 *  (no offset worked out here). KRX shows KST only. null for an order that does not fill. */
export function fillTimeLabel(o: Pick<Order, 'fill_at_kst' | 'fill_at_local' | 'fill_planned'>, market: Market, timezone: string | undefined): string | null {
  if (!o.fill_at_kst) return null
  const k = parts(o.fill_at_kst, 'Asia/Seoul')
  const local = market !== 'KRX' && o.fill_at_local && timezone
    ? ` (${parts(o.fill_at_local, timezone).time} ${ZONE_ABBR[timezone] ?? timezone})` : ''
  return `${o.fill_planned ? '체결 예정' : '체결'} ${k.day} ${k.time} KST${local}`
}

/** What the resume modal offers, from the server's preview: the resume button only for a person's halt or the −20% stop
 *  (a daily-loss halt lifts itself at the next open); the peak reset where a −20% stop is in the resume's scope — this
 *  market, or another stopped market the all-markets resume reaches. ``reset_peak`` is false whenever the reset is not
 *  shown, so the server's default (true) never resets a peak the admin did not see. */
export function resumeOffer(preview: ResumePreview, scopeAll: boolean, othersStopped: number, resetPeak: boolean) {
  const canResume = preview.can_resume
  const showReset = canResume && (preview.variant === 'dd_stop' || (scopeAll && othersStopped > 0))
  return { canResume, showReset, showTable: canResume && preview.variant === 'dd_stop' && preview.lines.length > 0,
    resetPeak: showReset && resetPeak }
}

/** The preview on screen answers the checkbox as it is now: after a toggle the last preview stays until the new one
 *  arrives (or fails), and its numbers must not sit under the other choice's labels, nor be resumed on. */
export function previewFresh(preview: ResumePreview, resetPeak: boolean): boolean {
  return preview.variant !== 'dd_stop' || preview.reset_peak === resetPeak
}

/** Every required field of the resume form filled in (the token only when the server has one). */
export function resumeReady(f: { operator: string; reason: string; token: string; tokenRequired: boolean }): boolean {
  return Boolean(f.operator.trim() && f.reason.trim() && (!f.tokenRequired || f.token.trim()))
}
