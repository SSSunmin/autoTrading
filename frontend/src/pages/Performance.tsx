import { useState } from 'react'
import { AgentAlert, AgentKpiCard, AgentSectionPanel, AgentTabs, Grid } from '@bv-ds/ui'

import { api, pct } from '../api/client'
import { useApi } from '../hooks/useApi'
import { AlphaBar, Loaded, Pct, RatingBadge, TickerCell } from '../components/common'
import type { PaperSummary, RatingScore, SimMetrics } from '../api/types'

const ORDER = ['Buy', 'Overweight', 'Hold', 'Underweight', 'Sell']

export function RatingTable({ byRating }: { byRating: Record<string, RatingScore> }) {
  const rows = ORDER.filter((r) => byRating[r])
  if (!rows.length) return <p className="muted">정산된 결정이 없습니다</p>
  return (
    <div className="matrix">
      <table>
        <thead><tr><th>등급</th><th className="r">n</th><th className="r">방향 적중률</th><th className="r">평균 알파</th><th style={{ width: '40%' }}>±10% 스케일</th></tr></thead>
        <tbody>
          {rows.map((r) => {
            const s = byRating[r]
            return (
              <tr key={r}>
                <td><RatingBadge rating={r as never} label={s.rating_ko} /></td>
                <td className="r num">{s.count}</td>
                <td className="r num">{s.hit_rate == null ? '방향 없음' : `${(s.hit_rate * 100).toFixed(0)}%`}</td>
                <td className="r"><Pct value={s.mean_alpha} digits={2} /></td>
                <td><AlphaBar value={s.mean_alpha} /></td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <p className="muted" style={{ marginTop: '0.6rem' }}>Hold는 방향을 주장하지 않으므로 적중률이 없습니다. Sell·Underweight는 알파가 음수일 때 적중입니다.</p>
    </div>
  )
}

/** 6: tab 1 scores single decisions by alpha (1st measurement); tab 2 runs the whole book with the hard rules
 *  off / on / on + regime filter and reads the drawdown first (2nd measurement, docs/05). */
export default function Performance() {
  const [tab, setTab] = useState('portfolio')
  const perf = useApi(() => api.performance(), [])
  const paper = useApi(() => api.paper(), [])
  return (
    <>
      <h1 className="page-title">성과·백테스트</h1>
      <AgentTabs value={tab} onChange={setTab} label="측정" items={[
        { id: 'portfolio', label: '포트폴리오 실측 (2차 · 드로다운)' },
        { id: 'decisions', label: '결정 채점 (1차 · 알파)' },
      ]} />
      <div className="section">
        {tab === 'portfolio' && <Loaded state={paper}>{(p) => <Portfolio p={p} />}</Loaded>}
        {tab === 'decisions' && <Loaded state={perf}>{(p) => (
          <>
            <p className="page-sub">실행 <b className="num">{p.run_id}</b>: {p.universe.length}종목 × {p.dates.length}일 = {p.cells_run}셀. 규칙 전용 베이스라인이라 에이전트가 더하는 알파를 재는 기준선입니다.</p>
            <Grid columns={4} gap="1.4rem">
              <AgentKpiCard label="정산 완료" value={p.resolved} />
              <AgentKpiCard label="정산 대기" value={p.pending} />
              <AgentKpiCard label="미채점 (REVIEW)" value={p.unscored} />
              <AgentKpiCard label="보유기간" value={p.holding} />
            </Grid>
            <div className="section"><AgentSectionPanel title="등급별 성과"><RatingTable byRating={p.by_rating} /></AgentSectionPanel></div>
            <div className="section">
              <AgentSectionPanel title="결정일별 평균 알파" description="롱 콜(매수·비중확대)과 숏 콜(매도·비중축소)을 따로 평균">
                <div className="matrix"><table>
                  <thead><tr><th>결정일</th><th className="r">정산 n</th><th className="r">롱 콜 평균 알파</th><th className="r">숏 콜 평균 알파</th><th style={{ width: '36%' }}>롱 콜</th></tr></thead>
                  <tbody>{p.per_date.map((r) => (
                    <tr key={r.date}><td className="num">{r.date}</td><td className="r num">{r.resolved}</td>
                      <td className="r"><Pct value={r.long_mean_alpha} digits={2} /></td><td className="r"><Pct value={r.short_mean_alpha} digits={2} /></td>
                      <td><AlphaBar value={r.long_mean_alpha} /></td></tr>
                  ))}</tbody>
                </table></div>
              </AgentSectionPanel>
            </div>
            <div className="section">
              <AgentSectionPanel title="반성 피드" description="정산 시 기록된 교훈. 에이전트 연결 후에는 2~4문장 교훈이 들어오고 다음 판단 프롬프트에 주입됩니다">
                <div className="matrix"><table>
                  <thead><tr><th>결정일</th><th>종목</th><th>등급</th><th className="r">알파</th><th>교훈</th></tr></thead>
                  <tbody>{p.reflections.slice(0, 20).map((r) => (
                    <tr key={r.ticker + r.date}><td className="num">{r.date}</td><td><TickerCell ticker={r.ticker} name={r.name} /></td>
                      <td><RatingBadge rating={r.rating} label={r.rating_ko} /></td><td className="r">{pct(r.alpha)}</td><td className="wrap muted">{r.reflection}</td></tr>
                  ))}</tbody>
                </table></div>
              </AgentSectionPanel>
            </div>
            <div className="section"><AgentAlert tone="warning" title="해석 주의">{p.caveat}</AgentAlert></div>
          </>
        )}</Loaded>}
      </div>
    </>
  )
}

const VARIANTS = ['rules_off', 'rules_on', 'rules_on_regime']

function DdCell({ m }: { m: SimMetrics }) {
  return (
    <td className="r">
      <Pct value={m.max_drawdown} />
      <div className="muted num">{m.dd_peak.slice(2)} → {m.dd_trough.slice(2)} · {m.dd_recovery ? `회복 ${m.dd_recovery_days}일` : '미회복'}</div>
    </td>
  )
}

function Portfolio({ p }: { p: PaperSummary }) {
  const on = p.variants.rules_on
  const off = p.variants.rules_off
  const b = p.benchmark.metrics
  const windows = Object.keys(p.windows)
  return (
    <>
      <p className="page-sub">
        KOSPI 대형주 {p.universe.length}종목, <span className="num">{p.start} ~ {p.end}</span>. 같은 선별·등급·목표 비중을 하드 룰 끔 / 켬 / 켬 + 200일선 국면 필터로 돌렸습니다.
        1차 지표는 수익이 아니라 <b>최대 드로다운과 회복</b>입니다. 규칙 전용(LLM 없음).
      </p>
      <Grid columns={4} gap="1.4rem">
        <AgentKpiCard label="KOSPI 최대 드로다운" value={pct(b.max_drawdown)} delta={`${b.dd_peak} → ${b.dd_trough}${b.dd_recovery ? '' : ' · 미회복'}`} direction="down" />
        <AgentKpiCard label="하드 룰 끔" value={pct(off.metrics.max_drawdown)} delta={`수익 ${pct(off.metrics.total_return)} · 노출 ${(off.mean_exposure * 100).toFixed(0)}%`} direction="down" />
        <AgentKpiCard label="하드 룰 켬" value={pct(on.metrics.max_drawdown)} delta={`수익 ${pct(on.metrics.total_return)} · 노출 ${(on.mean_exposure * 100).toFixed(0)}%`} />
        <AgentKpiCard label="하드 룰 + 국면 필터" value={pct(p.variants.rules_on_regime.metrics.max_drawdown)}
          delta={`수익 ${pct(p.variants.rules_on_regime.metrics.total_return)} · 노출 ${(p.variants.rules_on_regime.mean_exposure * 100).toFixed(0)}%`} />
      </Grid>

      <div className="section">
        <AgentSectionPanel title="변형 비교" description="결정은 종가, 체결은 다음날 시가(갭은 손절을 그대로 뚫음). 수수료 0.015%·거래세 0.20%·슬리피지 0.05%">
          <div className="matrix"><table>
            <thead><tr><th>변형</th><th className="r">수익</th><th className="r">CAGR</th><th className="r">최대 드로다운</th><th className="r">최악의 날</th><th className="r">변동성</th><th className="r">거래</th><th>청산 사유</th><th className="r">평균 노출</th></tr></thead>
            <tbody>
              <tr><td><b>KOSPI</b> <span className="muted">매수 보유</span></td><td className="r"><Pct value={b.total_return} /></td><td className="r"><Pct value={b.cagr} /></td>
                <DdCell m={b} /><td className="r"><Pct value={b.worst_day} /><div className="muted num">{b.worst_day_date}</div></td><td className="r num">{pct(b.volatility, 0)}</td><td className="r">–</td><td>–</td><td className="r num">100%</td></tr>
              {VARIANTS.map((k) => { const v = p.variants[k]; if (!v) return null; const m = v.metrics; return (
                <tr key={k}><td><b>{v.label_ko}</b><div className="muted">{v.risk ? `손절 ${pct(v.risk.stop_loss_pct, 0)} · 비중 ${pct(v.risk.max_position_pct, 0)} · DD ${pct(v.risk.dd_halve, 0)}/${pct(v.risk.dd_stop, 0)}` : '동일 비중 · 전액 투자 · 등급 하향 시만 매도'}{v.regime_filter ? ` · KOSPI ${v.risk?.regime_ma_days}일선` : ''}</div></td>
                  <td className="r"><Pct value={m.total_return} /></td><td className="r"><Pct value={m.cagr} /></td><DdCell m={m} />
                  <td className="r"><Pct value={m.worst_day} /><div className="muted num">{m.worst_day_date}</div></td>
                  <td className="r num">{pct(m.volatility, 0)}</td><td className="r num">{v.trades}{v.win_rate != null ? <div className="muted">승률 {(v.win_rate * 100).toFixed(0)}%</div> : null}</td>
                  <td className="wrap muted">{Object.entries(v.exits_by_reason_ko).map(([r, n]) => `${r} ${n}`).join(' · ')}</td>
                  <td className="r num">{(v.mean_exposure * 100).toFixed(0)}%</td></tr>
              ) })}
            </tbody>
          </table></div>
        </AgentSectionPanel>
      </div>

      <div className="section">
        <AgentSectionPanel title="급락 구간별" description="각 구간은 그 구간 시작일부터 측정. 수익 / 구간 내 최대 드로다운">
          <div className="matrix"><table>
            <thead><tr><th>구간</th><th>기간</th><th className="r">KOSPI</th>{VARIANTS.map((k) => <th key={k} className="r">{p.variants[k]?.label_ko}</th>)}</tr></thead>
            <tbody>{windows.map((w) => (
              <tr key={w}><td><b>{w}</b></td><td className="num muted">{p.windows[w][0]} ~ {p.windows[w][1]}</td>
                <td className="r"><Pct value={p.benchmark.windows[w]?.return} /> <span className="muted">/</span> <Pct value={p.benchmark.windows[w]?.max_drawdown} /></td>
                {VARIANTS.map((k) => { const x = p.variants[k]?.windows[w]; return <td key={k} className="r">{x ? <><Pct value={x.return} /> <span className="muted">/</span> <Pct value={x.max_drawdown} /></> : '–'}</td> })}
              </tr>
            ))}</tbody>
          </table></div>
        </AgentSectionPanel>
      </div>

      <div className="cards section">
        <AgentSectionPanel title="가드·잠금 발동 (하드 룰 켬)" description="일일 손실 한도, StoplossGuard, Cooldown, LowProfit, 국면 필터가 신규 매수를 막은 기록">
          <div className="matrix"><table><tbody>
            {Object.entries(on.event_counts).map(([k, n]) => <tr key={k}><td>{k}</td><td className="r num">{n}</td></tr>)}
          </tbody></table></div>
          <div className="stack-gap" style={{ gap: '0.4rem', marginTop: '0.8rem' }}>
            {on.events_tail.filter((e) => e.kind !== 'rebalance').slice(-8).map((e, i) => <div key={i} className="muted"><span className="num">{e.date}</span> {e.text}</div>)}
          </div>
        </AgentSectionPanel>
        <AgentSectionPanel title="손절을 뚫은 갭 (하드 룰 켬, 최악 거래)" description="−8% 손절이 있어도 다음날 시가 갭은 그대로 맞습니다. 손실을 가둔 것은 종목당 비중 상한입니다">
          <div className="matrix"><table>
            <thead><tr><th>종목</th><th>청산일</th><th>사유</th><th className="r">손익</th><th className="r">보유</th></tr></thead>
            <tbody>{on.trades_sample.slice(0, 6).map((t, i) => (
              <tr key={i}><td><TickerCell ticker={t.ticker} name={p.universe_ko[t.ticker]} /></td><td className="num">{t.exit_date}</td><td>{t.reason}</td><td className="r"><Pct value={t.pnl_pct} /></td><td className="r num">{t.holding_days}일</td></tr>
            ))}</tbody>
          </table></div>
        </AgentSectionPanel>
      </div>

      <div className="section">
        <AgentSectionPanel title="모의투자 통과 기준" description="같은 자동 루프를 모의로 돌려 전 기간 충족해야 실거래로 전환. 기준값은 config.PAPER_TRADING">
          <div className="matrix"><table><tbody>
            {p.criteria.map((c) => <tr key={c.id}><td>{c.label_ko} <span className="muted num">{c.id}</span></td><td className="r num">{typeof c.value === 'number' && Math.abs(c.value) < 1 && c.value !== 0 ? pct(c.value, 0) : String(c.value)}</td></tr>)}
          </tbody></table></div>
        </AgentSectionPanel>
      </div>

      <div className="section"><AgentAlert tone="warning" title="해석 주의"><ul className="effects">{p.caveats.map((c, i) => <li key={i}>{c}</li>)}</ul></AgentAlert></div>
    </>
  )
}
