import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AgentEmptyState, AgentSectionPanel, AgentSelect } from '@bv-ds/ui'

import { api } from '../api/client'
import { screenEmpty, weeksBefore } from '../api/screenFilter'
import type { Market } from '../api/types'
import { useApi } from '../hooks/useApi'
import { useMarket, useMarketPath } from '../hooks/useMarket'
import { Loaded } from '../components/common'
import { PageHeader } from '../components/parts'

const PERIODS = [{ value: 'all', label: '전체' }, { value: '8', label: '최근 8주' }, { value: '4', label: '최근 4주' }]

/** 2-A: screening runs. Each row is one ScreenRun; the funnel column previews the stage counts.
 *  The filters go to the API (from · universe); the list remounts per market so a filter never carries across. */
export default function Screens() {
  const [market] = useMarket()
  return <ScreenList key={market} market={market} />
}

function ScreenList({ market }: { market: Market }) {
  const navigate = useNavigate()
  const to = useMarketPath()
  const [period, setPeriod] = useState('all')
  const [universe, setUniverse] = useState('')
  const all = useApi(() => api.screens(market), [market])
  const last = all.data?.[0]
  // the period counts back from the latest run, not from today: the records are dated by their trading day
  const from = period === 'all' || !last ? undefined : weeksBefore(last.as_of, Number(period))
  const rows = useApi(() => api.screens(market, { from, universe: universe || undefined }), [market, from, universe])
  const universes = [...new Set((all.data ?? []).map((s) => s.universe))]
  const empty = screenEmpty(all.data?.length ?? 0, last?.as_of)
  return (
    <>
      <PageHeader title="종목 선별" description="선별 실행 목록입니다. 행을 열면 유니버스 N개가 규칙을 하나씩 지나며 몇 개 남는지, 각 종목이 어느 규칙에서 어떤 값으로 떨어졌는지 봅니다." />
      <div className="toolbar">
        <AgentSelect label="기간" size="sm" value={period} onChange={(e) => setPeriod(e.target.value)} options={PERIODS} />
        <AgentSelect label="유니버스" size="sm" value={universe} onChange={(e) => setUniverse(e.target.value)}
          options={[{ value: '', label: '전체' }, ...universes.map((u) => ({ value: u, label: u }))]} />
        <span className="muted" style={{ marginLeft: 'auto' }}>기준일 내림차순</span>
      </div>
      <AgentSectionPanel title="선별 실행">
        <Loaded state={rows} empty={empty.title} emptyDescription={empty.description}>
          {(list) => list.length === 0 ? <AgentEmptyState title={empty.title} description={empty.description} /> : (
            <div className="matrix">
              <table>
                <thead><tr><th>실행 ID</th><th>기준일</th><th>유니버스</th><th className="r">크기</th><th className="r">통과</th><th className="r">규칙 수</th><th>깔때기</th><th>실행 시각</th></tr></thead>
                <tbody>
                  {list.map((s) => (
                    <tr key={s.run_id} className="click" onClick={() => navigate(to(`/screens/${encodeURIComponent(s.run_id)}`))}>
                      <td className="mono">{s.run_id}</td>
                      <td className="num">{s.as_of}</td>
                      <td>{s.universe}</td>
                      <td className="r num">{s.universe_size}</td>
                      <td className="r num"><b>{s.selected_count}</b></td>
                      <td className="r num">{s.rule_count}</td>
                      <td className="num muted">{s.universe_size} → {s.stage_counts.join(' → ')}</td>
                      <td className="num">{s.recorded_at.slice(0, 16).replace('T', ' ')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Loaded>
      </AgentSectionPanel>
    </>
  )
}
