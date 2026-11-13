import { useNavigate } from 'react-router-dom'
import { AgentSectionPanel } from '@bv-ds/ui'

import { api } from '../api/client'
import { useApi } from '../hooks/useApi'
import { Loaded } from '../components/common'

/** 2-A: screening runs. Each row is one ScreenRun; the funnel column previews the stage counts. */
export default function Screens() {
  const navigate = useNavigate()
  const screens = useApi(() => api.screens(), [])
  return (
    <>
      <h1 className="page-title">종목 선별</h1>
      <p className="page-sub">선별 실행 목록입니다. 행을 열면 유니버스 N개가 규칙을 하나씩 지나며 몇 개 남는지, 각 종목이 어느 규칙에서 어떤 값으로 떨어졌는지 봅니다.</p>
      <AgentSectionPanel title="선별 실행">
        <Loaded state={screens} empty="선별 실행 기록이 없습니다">
          {(rows) => (
            <div className="matrix">
              <table>
                <thead><tr><th>실행 ID</th><th>기준일</th><th>유니버스</th><th className="r">크기</th><th className="r">통과</th><th>깔때기</th><th>실행 시각</th></tr></thead>
                <tbody>
                  {rows.map((s) => (
                    <tr key={s.run_id} className="click" onClick={() => navigate(`/screens/${encodeURIComponent(s.run_id)}`)}>
                      <td className="num">{s.run_id}</td>
                      <td className="num">{s.as_of}</td>
                      <td>{s.universe}</td>
                      <td className="r num">{s.universe_size}</td>
                      <td className="r num"><b>{s.selected_count}</b></td>
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
