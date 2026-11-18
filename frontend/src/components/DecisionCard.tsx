import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { AgentButton, AgentChip } from '@bv-ds/ui'

import { money, utc } from '../api/client'
import type { Decision, ExceptionItem } from '../api/types'
import { useLabels } from '../hooks/useLabels'
import { useMarketPath } from '../hooks/useMarket'
import { Pct, RatingBadge } from './common'
import { actionWord, bodySlot, confidenceText, evidenceSlot, execLines, execSlot, kindTag, mainOrder, priceCells, ratingPill, sections, sourceTag } from './cardLogic'
import { ExceptionButtons, Interventions, PlanGrid } from './decisionParts'
import { FillTime } from './risk'

/** One decision as the v0.8 card (mockup DecisionCard): six slots of fixed height — ① head (name · ticker / who and what
 *  kind · rating · action / decision day · confidence), ② prices (four cells), ③ the execution state (badge, time, three
 *  cells), ④ the body (a memo's thesis with bull and bear, a sell review's reasons and reasoning), ⑤ evidence (two, "+n건"),
 *  ⑥ the buttons — so the same thing sits at the same height on every card in a row. "전체 보기" opens the card in place:
 *  every cut line in full, the memo's other fields under Korean headings (each text once — card-dedupe), every piece of
 *  evidence. An exception still on the
 *  queue adds its action row above the buttons (below every slot). ``showPlan``: the 하드룰 개입 tab shows the 원안 vs
 *  최종 grid without opening. ``defaultOpen``: start open (the tests render both states without a DOM). */
export function DecisionCard({ d, exception, tokenRequired = false, showPlan = false, defaultOpen = false }: {
  d: Decision; exception?: ExceptionItem; tokenRequired?: boolean; showPlan?: boolean; defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const to = useMarketPath()
  const navigate = useNavigate()
  const labels = useLabels().data
  const amt = (v: number | null | undefined) => money(v, d.market)    // the record's own market, not the toggle
  const kind = kindTag(d), pill = ratingPill(d)
  const exec = execSlot(d, amt, { exceptionKind: labels?.exception_kind_ko, action: labels?.exception_action_ko })
  const body = bodySlot(d, open, labels?.exit_reason_ko)
  const ev = evidenceSlot(d.evidence, open)
  const o = mainOrder(d)
  const plans = d.orders.filter((x) => x.plan && x.plan.interventions.length > 0)
  const detail = `/tickers/${encodeURIComponent(d.ticker)}`
  const planGrids = plans.map((x) => x.plan && (
    <div key={x.order_id} className="plan">
      <PlanGrid plan={x.plan} market={x.market} />
      <Interventions ivs={x.plan.interventions} />
    </div>
  ))
  return (
    <article className={`dc${open ? ' open' : ''}`}>
      <header className="dc-head">
        <div className="dc-l1">
          <Link to={to(detail)} className="dc-name c1">{d.name ?? d.ticker}</Link>
          {d.name && <span className="tag mono">{d.ticker}</span>}
        </div>
        <div className="dc-l2">
          <span className={`kind ${kind.cls}`}>{kind.label}</span>
          <RatingBadge rating={pill.rating} label={pill.label} />
          <span className="act">{actionWord(d)}</span>
        </div>
        <div className="dc-l3">
          <span>결정 <span className="num">{d.trade_date} {d.market === 'US' ? 'ET' : 'KST'}</span></span>
          <span>확신도 <b className="num">{confidenceText(d.confidence)}</b></span>
        </div>
      </header>

      <div className="dc-price">
        {priceCells(d, amt).map((c) => (
          <div key={c.label} className="pc">
            <div className="lb c1">{c.label}</div>
            <div className="c1"><b className="pv num">{c.value}</b> {c.sub && <span className={`sub ${c.tone}`}>{c.sub}</span>}</div>
          </div>
        ))}
      </div>

      <div className={`dc-exec ${exec.kind}`}>
        <div className="dc-exec-top">
          <span className={`xs ${exec.kind}`}>{exec.label}</span>
          <span className="c1 small muted num">{o ? (o.fill_at_kst ? <FillTime o={o} market={o.market} /> : utc(o.placed_at)) : ''}</span>
        </div>
        <div className="dc-exec-cells">
          {exec.cells.map((c, i) => (
            <div key={c.label}><div className="lb c1">{c.label}</div>
              {i === 2 && o?.rerun_answer ? (
                <Link className="c1 v" to={to(`/decisions?date=${o.rerun_answer.trade_date}`)}
                  title={o.rerun_answer.same_day ? '그날 결정이 이 종목을 이미 판단해 그 결정이 답입니다' : undefined}>{`${c.value} →`}</Link>
              ) : <div className={`c1 v ${c.tone}`}>{c.value}</div>}
            </div>
          ))}
        </div>
        {open && execLines(d, exec, body, amt, labels?.exception_action_ko).map((x) => (
          <div key={x.id} className="dc-exec-text">
            {x.text}{x.done && <> <AgentChip variant="secondary">{x.done}</AgentChip></>}
          </div>
        ))}
        {open && !o && d.tradeable && d.source !== 'human' && <div className="dc-exec-text">주문 기록 없음 · 아직 모의 실행 전 (결정은 다음 거래일 시가에 주문됩니다)</div>}
      </div>

      <div className="dc-body">
        <div className="dc-body-hd">
          <span className="lb">{body.label}</span>
          {body.chips.map((c) => <span key={c.text} className={`rc ${c.cls}`}>{c.text}</span>)}
        </div>
        <p className="dc-sum">{body.sum}</p>
        {body.rows.map((r, i) => (
          <div key={i} className="dc-row c1">{r.label && <b className={r.tone}>{r.label}</b>} {r.text}</div>
        ))}
      </div>

      {open && (
        <div className="dc-memo">
          {sections(d, amt).map((s) => (
            <section key={s.label}>
              <div className={`lb ${s.tone ?? ''}`}>{s.label}</div>
              {s.text != null && <p className={s.pre ? 'pre' : undefined}>{s.text}</p>}
              {s.items && (
                <ul>{s.items.map((i, n) => (
                  <li key={n}>{i.chip && <span className={`rc ${i.chipCls}`}>{i.chip}</span>}<span>{i.text}{i.sub && <span className="muted"> — {i.sub}</span>}</span></li>
                ))}</ul>
              )}
            </section>
          ))}
          {d.hard_rule_overrides.length > 0 && (
            <section><div className="lb">에이전트가 적은 개입</div>
              <div className="dc-chips">{d.hard_rule_overrides.map((x) => <AgentChip key={x} variant="hot">{x}</AgentChip>)}</div></section>
          )}
          <section><div className="lb">5일 정산</div>
            <p>{d.outcome == null ? '기록 없음' : d.outcome.pending ? '정산 대기' : <>실현 <Pct value={d.outcome.raw_return} /> · 알파 <Pct value={d.outcome.alpha} /></>}</p></section>
        </div>
      )}

      {open && planGrids}

      <div className="dc-ev">
        <div className="lb">증거 {ev.count}건 {ev.more > 0 && <span className="muted">· +{ev.more}건은 펼치면 보임</span>}</div>
        {ev.shown.map((e, i) => {
          const s = sourceTag(e.source)
          return (
            <div key={i} className="dc-ev-row">
              <span className={`src-tag ${s.cls}`}>{s.label}</span><span className="c1">{e.summary}</span><span className="small muted num">{e.as_of?.slice(5) ?? ''}</span>
            </div>
          )
        })}
      </div>

      {/* 하드룰 개입 tab, folded: the 원안 vs 최종 grid below every slot (review 1R #3), as the exception row */}
      {!open && showPlan && planGrids}

      {exception && (
        <div className="dc-xa">
          <div className="lb fail">예외 처리 · 하나를 고르세요</div>
          <div className="dc-xa-btns"><ExceptionButtons item={exception} tokenRequired={tokenRequired} /></div>
        </div>
      )}

      <footer className="dc-foot">
        <AgentButton variant="secondary" aria-expanded={open} onClick={() => setOpen((v) => !v)}>{open ? '접기' : '전체 보기'}</AgentButton>
        <AgentButton variant="secondary" onClick={() => navigate(to(detail))}>종목 상세</AgentButton>
      </footer>
    </article>
  )
}
