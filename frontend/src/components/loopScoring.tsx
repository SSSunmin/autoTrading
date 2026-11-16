import { AgentSegmentedControl } from '@bv-ds/ui'

import type { LoopScoring } from '../api/types'
import { type Cell, loopRows, type Period, PERIODS, recordedText, trialBadge } from './loopScoringLogic'
import { sharePct, signPct, tone } from './perfLogic'

/** 모의 루프 채점 (loop-scoring): the model's and the rule's daily-loop decisions side by side, settled five
 *  sessions on (the trial report's definition). Every figure is the server's; rising red, falling blue. */

export function TrialBadge({ trial }: { trial: LoopScoring['trial'] }) {
  const b = trialBadge(trial)
  if (!b) return null
  return <span className={b.done ? 'trial-badge done' : 'trial-badge'}>{b.text}</span>
}

function CellView({ c }: { c: Cell | null }) {
  if (!c) return null
  if (c.kind === 'count') return <td className="r num">{c.value}</td>
  if (c.kind === 'text') return <td className="r">{c.value}</td>
  return <td className={`r num ${c.signed ? tone(c.value) : ''}`}>{c.signed ? signPct(c.value, 2) : sharePct(c.value, 0)}</td>
}

export function LoopPanel({ l }: { l: LoopScoring }) {
  const cols = (['agent', 'rule'] as const).filter((k) => l.columns[k])
  if (!cols.some((k) => l.columns[k]!.decisions > 0)) {
    return <p className="perf-empty">일일 루프 결정이 없습니다 — 일일 루프 decide가 기록한 결정부터 셉니다</p>
  }
  const a = l.agreement
  const rec = recordedText(l.rule_recorded, a.no_screen)
  return (
    <div className="stack-gap" style={{ gap: '0.8rem' }}>
      <div className="matrix"><table className="loop-t">
        <thead><tr><th>항목</th>{cols.map((k) => <th key={k} className="r">{l.columns[k]!.label_ko}</th>)}<th>정의</th></tr></thead>
        <tbody>{loopRows(l.columns).map((r) => (
          <tr key={r.label}><td>{r.label}</td>{r.cells.map((c, i) => c && <CellView key={i} c={c} />)}<td className="muted">{r.note ?? ''}</td></tr>
        ))}</tbody>
      </table></div>
      <p className="loop-line">같은 결정일 · 같은 종목에서 규칙 판단과 일치 <b className="num">{a.agree}/{a.compared}</b>
        {a.rate != null ? ` (${sharePct(a.rate, 0)})` : ''} — 규칙 열은 모델과 같은 날 · 같은 종목을 그날 선별로 규칙이 판단한 결과(재계산)</p>
      {rec && <p className="muted">{rec}</p>}
      <p className="muted">사람 처리 {l.human_excluded}건 · 규칙 청산 {l.rule_exits_excluded}건은 채점 제외. {l.note_ko}</p>
    </div>
  )
}

/** Shown under the 6-A header once a period is picked; nothing for the whole book. */
export function PeriodNote({ from, day }: { from: string | undefined; day: (d: string) => string }) {
  if (!from) return null
  return <p className="muted period-note">{day(from)} 이후만 표시 — 수익률 · 드로다운은 모의 시작일 기준 그대로입니다</p>
}

export function PeriodControl({ value, onChange }: { value: Period; onChange: (p: Period) => void }) {
  return (
    <div className="period-ctl" role="group" aria-label="기간">
      <span className="muted">기간</span>
      <AgentSegmentedControl value={value} onChange={(v) => onChange(v as Period)} options={PERIODS} />
    </div>
  )
}
