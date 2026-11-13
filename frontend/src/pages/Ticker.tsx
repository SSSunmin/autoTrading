import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { AgentEmptyState, AgentSectionPanel, AgentSelect, AgentTabs } from '@bv-ds/ui'

import { api } from '../api/client'
import { useApi } from '../hooks/useApi'
import { Loaded, Pct, RatingBadge, RuleCell, TickerCell } from '../components/common'
import { DecisionCard } from '../components/DecisionCard'
import type { TickerDetail } from '../api/types'

/** 4: one instrument. Identity, decision history as the memo reads, screening history, outcomes and lessons. */
export default function Ticker() {
  const { ticker } = useParams()
  const navigate = useNavigate()
  const tickers = useApi(() => api.tickers(), [])
  const current = ticker ?? tickers.data?.[0]?.ticker ?? null
  const detail = useApi(() => (current ? api.ticker(current) : Promise.reject(new Error('종목을 선택하세요'))), [current])
  const [tab, setTab] = useState('decisions')

  return (
    <>
      <h1 className="page-title">종목 상세</h1>
      <div className="toolbar">
        <AgentSelect label="종목" size="sm" value={current ?? ''} onChange={(e) => navigate(`/tickers/${encodeURIComponent(e.target.value)}`)}
          options={(tickers.data ?? []).map((t) => ({ value: t.ticker, label: `${t.ticker} ${t.name ?? ''}` }))} />
      </div>
      <Loaded state={detail}>{(d) => <Body d={d} tab={tab} setTab={setTab} />}</Loaded>
    </>
  )
}

function Body({ d, tab, setTab }: { d: TickerDetail; tab: string; setTab: (t: string) => void }) {
  const latest = d.screening[0]
  return (
    <>
      <div className="card" style={{ marginBottom: '1.4rem' }}>
        <div className="hd">
          <div style={{ display: 'flex', gap: '1.2rem', alignItems: 'center' }}><TickerCell ticker={d.ticker} name={d.name} size="lg" />
            <span className="muted">정체는 실행 시작 시 프로필 조회로 고정됩니다</span></div>
          <div>최근 선별 {latest ? (latest.selected ? <RatingBadge rating="Buy" label={`선정 · ${latest.rank}위`} /> : <RatingBadge rating="Hold" label="탈락" />) : '–'}</div>
        </div>
      </div>
      <AgentTabs value={tab} onChange={setTab} label="종목 정보" items={[
        { id: 'decisions', label: '결정 이력' }, { id: 'screening', label: '선별 이력' }, { id: 'outcomes', label: '결과·교훈' }, { id: 'snapshot', label: '검증 스냅샷' },
      ]} />
      <div className="section">
        {tab === 'decisions' && (
          <>
            <div className="timeline" style={{ marginBottom: '1.2rem' }}>
              {d.timeline.map((t) => <span key={t.date} className={`tl ${t.rating}`} title={`${t.date} ${t.rating}`}>{t.date.slice(5).replace('-', '/')}</span>)}
            </div>
            {d.decisions.length ? <div className="cards">{d.decisions.map((x) => <DecisionCard key={x.trade_date} d={x} />)}</div>
              : <AgentEmptyState title="보유 외 결정이 없습니다" description="투자 메모(사업 요약 → 논지 → 불/베어 → 밸류에이션 → 촉매 → 논지 깨짐 조건 → 등급·비중 → 매도 계획)는 LLM 에이전트가 연결되면 같은 순서로 표시됩니다." />}
          </>
        )}
        {tab === 'screening' && (
          <AgentSectionPanel title="선별 이력">
            <div className="matrix"><table>
              <thead><tr><th>기준일</th><th>선정</th><th className="r">순위</th><th className="r">점수</th><th>첫 탈락 규칙</th><th>비고</th></tr></thead>
              <tbody>{d.screening.map((s) => {
                const fail = s.filters.find((f) => !f.passed)
                const mom = s.filters.find((f) => f.rule === 'momentum_top_k')
                return (
                  <tr key={s.run_id}><td className="num">{s.as_of}</td><td>{s.selected ? '✓' : '✗'}</td><td className="r num">{s.rank ?? ''}</td>
                    <td className="r num">{s.score?.toFixed(2) ?? ''}</td>
                    <td>{fail ? <span className="cell fail">{s.first_failed_rule_ko} {String(fail.value ?? '')} ({String(fail.threshold ?? '')})</span> : ''}</td>
                    <td className="wrap muted">{mom?.note || fail?.note || ''}</td></tr>
                )
              })}</tbody>
            </table></div>
          </AgentSectionPanel>
        )}
        {tab === 'outcomes' && (
          <AgentSectionPanel title="결과·교훈" description="보유기간이 지난 결정의 실현수익·알파와 반성">
            <div className="matrix"><table>
              <thead><tr><th>결정일</th><th>등급</th><th className="r">실현</th><th className="r">알파</th><th>보유</th><th>정산일</th><th>교훈</th></tr></thead>
              <tbody>{d.outcomes.map((o) => (
                <tr key={o.trade_date}><td className="num">{o.trade_date}</td><td><RatingBadge rating={o.rating} label={o.rating_ko} /></td>
                  <td className="r">{o.pending ? <RatingBadge rating="pending" label="대기" /> : <Pct value={o.raw_return} />}</td>
                  <td className="r">{o.pending ? '' : <Pct value={o.alpha} />}</td>
                  <td className="num">{o.holding ?? ''}</td><td className="num">{o.resolved ?? ''}</td><td className="wrap muted">{o.reflection}</td></tr>
              ))}</tbody>
            </table></div>
          </AgentSectionPanel>
        )}
        {tab === 'snapshot' && (
          <AgentSectionPanel title="검증 스냅샷" description="에이전트에게 주어지는 유일한 숫자 출처. 현재는 선별 시점의 규칙 값만 저장되어 있고, OHLCV·지표 표는 에이전트 연결 시 함께 기록됩니다">
            {latest ? (
              <div className="matrix"><table>
                <thead><tr><th>규칙</th><th className="r">값</th><th className="r">임계</th><th>판정</th><th>비고</th></tr></thead>
                <tbody>{latest.filters.map((f) => (
                  <tr key={f.rule}><td>{f.rule_ko}</td><td className="r"><RuleCell f={f} /></td><td className="r num">{String(f.threshold ?? '')}</td>
                    <td>{f.passed ? '통과' : '탈락'}</td><td className="wrap muted">{f.note}</td></tr>
                ))}</tbody>
              </table></div>
            ) : <AgentEmptyState title="선별 기록 없음" compact />}
          </AgentSectionPanel>
        )}
      </div>
    </>
  )
}
