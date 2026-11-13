import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { AgentBreadcrumb, AgentSectionPanel, AgentSegmentedControl, SearchField } from '@bv-ds/ui'

import { api } from '../api/client'
import { useApi } from '../hooks/useApi'
import { useLabels } from '../hooks/useLabels'
import { useMarket, useMarketPath } from '../hooks/useMarket'
import { Loaded, RuleCell, TickerCell } from '../components/common'
import type { Candidate, ScreenDetail as ScreenDetailT } from '../api/types'

/** 2-B: the funnel plus the ticker × rule matrix. Clicking a stage filters to the tickers that rule rejected. */
export default function ScreenDetail() {
  const { runId = '' } = useParams()
  const navigate = useNavigate()
  const [market] = useMarket()
  const to = useMarketPath()
  const state = useApi(() => api.screen(market, runId), [market, runId])
  const labels = useLabels()
  const [stage, setStage] = useState<number | null>(null)
  const [filter, setFilter] = useState('all')
  const [q, setQ] = useState('')

  return (
    <>
      <AgentBreadcrumb items={[{ label: '종목 선별', href: to('/screens') }, { label: runId }]} />
      <Loaded state={state}>
        {(s) => <Detail s={s} stage={stage} setStage={setStage} filter={filter} setFilter={setFilter} q={q} setQ={setQ}
                        ruleDesc={labels.data?.rule_desc_ko ?? {}} onTicker={(t) => navigate(to(`/tickers/${encodeURIComponent(t)}`))} />}
      </Loaded>
    </>
  )
}

function Detail({ s, stage, setStage, filter, setFilter, q, setQ, ruleDesc, onTicker }: {
  s: ScreenDetailT; stage: number | null; setStage: (v: number | null) => void; filter: string; setFilter: (v: string) => void
  q: string; setQ: (v: string) => void; ruleDesc: Record<string, string>; onTicker: (t: string) => void
}) {
  const prev = (i: number) => (i === 0 ? s.universe_size : s.stage_counts[i - 1])
  const rows = useMemo(() => {
    let c: Candidate[] = s.candidates
    if (stage != null) { const rule = s.rules[stage]; c = c.filter((x) => x.filters.some((f) => f.rule === rule && !f.passed)) }
    if (filter === 'selected') c = c.filter((x) => x.selected)
    if (filter === 'rejected') c = c.filter((x) => !x.selected)
    if (q) { const k = q.toLowerCase(); c = c.filter((x) => x.ticker.toLowerCase().includes(k) || (x.name ?? '').toLowerCase().includes(k)) }
    return [...c].sort((a, b) => (a.rank ?? 999) - (b.rank ?? 999) || a.ticker.localeCompare(b.ticker))
  }, [s, stage, filter, q])

  return (
    <>
      <h1 className="page-title">{s.universe} · <span className="num">{s.as_of}</span></h1>
      <p className="page-sub"><span className="num">{s.universe_size} → {s.selected_count}</span> 종목. 단계를 누르면 그 규칙에서 탈락한 종목만 표에 남습니다.</p>

      <div className="funnel" role="group" aria-label="선별 깔때기">
        <button type="button" className={`stage ${stage == null ? 'active' : ''}`} onClick={() => setStage(null)}>
          <div className="n num">{s.universe_size}</div><div className="d">유니버스</div>
        </button>
        {s.rules.map((r, i) => (
          <button type="button" key={r} className={`stage ${stage === i ? 'active' : ''}`} onClick={() => setStage(stage === i ? null : i)} title={ruleDesc[r]}>
            <div className="n num">{s.stage_counts[i]}</div>
            <div className="d">{s.rules_ko[i]}</div>
            <div className="drop num">−{prev(i) - s.stage_counts[i]}</div>
          </button>
        ))}
      </div>

      <AgentSectionPanel title="종목 × 규칙" description="셀 = 값 (임계). 초록 통과, 빨강 탈락, 회색은 이전 규칙에서 떨어져 평가하지 않음"
        action={
          <div className="toolbar" style={{ marginBottom: 0 }}>
            <AgentSegmentedControl size="sm" value={filter} onChange={setFilter}
              options={[{ value: 'all', label: '전체' }, { value: 'selected', label: '선정만' }, { value: 'rejected', label: '탈락만' }]} />
            <SearchField size="small" aria-label="티커·이름 검색" placeholder="티커·이름 검색" value={q} onChange={(e) => setQ(e.target.value)} onClear={() => setQ('')} />
          </div>
        }>
        <div className="matrix">
          <table>
            <thead>
              <tr>
                <th>선정</th><th className="r">순위</th><th>종목</th><th className="r">점수</th>
                {s.rules.map((r, i) => <th key={r} title={ruleDesc[r]}>{s.rules_ko[i]}</th>)}
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => {
                const data = c.filters.find((f) => f.rule === 'data')
                return (
                  <tr key={c.ticker} className="click" onClick={() => onTicker(c.ticker)}>
                    <td>{c.selected ? '✓' : '✗'}</td>
                    <td className="r num">{c.rank ?? ''}</td>
                    <td><TickerCell ticker={c.ticker} name={c.name} /></td>
                    <td className="r num">{c.score != null ? c.score.toFixed(2) : ''}</td>
                    {s.rules.map((r, i) => <td key={r}><RuleCell f={c.filters.find((f) => f.rule === r)} dataNote={data && i === 0 ? data.note : undefined} /></td>)}
                  </tr>
                )
              })}
              {rows.length === 0 && <tr><td colSpan={4 + s.rules.length} className="muted">조건에 맞는 종목이 없습니다</td></tr>}
            </tbody>
          </table>
        </div>
      </AgentSectionPanel>

      <div className="section">
        <AgentSectionPanel title="규칙" description="이번 실행에 적용된 순서와 단계별 탈락 수">
          <div className="matrix">
            <table>
              <thead><tr><th>#</th><th>규칙</th><th>설명</th><th className="r">탈락</th></tr></thead>
              <tbody>
                {s.rules.map((r, i) => (
                  <tr key={r}><td className="num">{i + 1}</td><td><b>{s.rules_ko[i]}</b> <span className="muted num">{r}</span></td>
                    <td className="wrap">{ruleDesc[r] ?? ''}</td><td className="r num">{prev(i) - s.stage_counts[i]}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </AgentSectionPanel>
      </div>
    </>
  )
}
