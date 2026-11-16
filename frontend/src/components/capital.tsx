import { useState } from 'react'
import { AgentAlert, AgentButton, AgentModal, AgentSectionPanel, AgentSegmentedControl, AgentTextarea, AgentTextField } from '@bv-ds/ui'

import { announceStateChange, api, money, utc } from '../api/client'
import type { CapitalAccount, CapitalInfo, CapitalPreview, Market, UniverseKind } from '../api/types'
import { useApi } from '../hooks/useApi'
import { resumeReady } from './riskLogic'
import { capitalAuditLine, capitalProblem, changePreview, parseAmount } from './capitalLogic'

const UNIVERSES: { value: UniverseKind; label: string }[] = [
  { value: 'loop', label: '지금 루프 유니버스' }, { value: 'index', label: '지수 구성(오늘)' }, { value: 'custom', label: '직접 지정' },
]

/** 실거래 자본 · 유니버스 (mockup v0.7.2 Config, docs/10 D5): both accounts' capital from the server (capital_set
 *  replay), the "rules at this capital" preview the server computes, and the change through a confirm modal. */
export function CapitalPanel({ market }: { market: Market }) {
  const [tick, setTick] = useState(0)
  const info = useApi(() => api.capital(market), [market, tick])
  const [notice, setNotice] = useState<string | null>(null)
  return (
    <AgentSectionPanel title={`실거래 자본 · 유니버스 · ${info.data?.market_ko ?? ''}`}
      description="시장마다 따로 · 바꾸면 다음 주문부터 적용 · 관리자. 아래 표는 이 자본에서 어떤 규칙이 어떤 금액으로 걸리는지 서버가 계산합니다">
      {notice && <AgentAlert tone="success" title="자본 설정">{notice}</AgentAlert>}
      {info.error && <AgentAlert tone="error" title="자본을 읽지 못함">{info.error}</AgentAlert>}
      {info.data && <CapitalForm key={`${market}-${tick}`} info={info.data}
        onDone={(msg) => { setNotice(msg); setTick((x) => x + 1); announceStateChange() }} />}
    </AgentSectionPanel>
  )
}

function CapitalForm({ info, onDone }: { info: CapitalInfo; onDone: (msg: string) => void }) {
  const m = info.market
  const [account, setAccount] = useState<CapitalAccount>('paper')
  const [text, setText] = useState<Record<CapitalAccount, string>>({
    paper: String(info.paper.amount), live: info.live.amount != null ? String(info.live.amount) : '' })
  const [universe, setUniverse] = useState<UniverseKind>('loop')
  const [tickers, setTickers] = useState('')
  const [open, setOpen] = useState(false)
  const amount = parseAmount(text[account], m)
  const fmt = (v: number) => money(v, m)
  const problem = capitalProblem(amount, info, account, fmt)
  const valid = amount != null && amount > 0 && amount <= info.max_amount
  const preview = useApi<CapitalPreview | null>(
    () => (valid && (universe !== 'custom' || tickers.trim()) ? api.capitalPreview(m, { account, amount: amount ?? undefined, universe, tickers })
      : Promise.resolve(null)), [m, account, valid ? amount : null, universe, universe === 'custom' ? tickers : ''])
  return (
    <div className="stack-gap" style={{ gap: '1rem' }}>
      <CapitalRows info={info} />
      <div className="toolbar form-row" style={{ marginBottom: 0, alignItems: 'flex-end' }}>
        <AgentSegmentedControl value={account} onChange={(v) => setAccount(v as CapitalAccount)}
          options={[{ value: 'paper', label: '모의투자 자본' }, { value: 'live', label: '실거래 자본' }]} />
        <AgentTextField label={`${account === 'paper' ? '모의투자' : '실거래'} 자본 (${m === 'KRX' ? '원' : '달러'})`}
          inputMode="decimal" value={text[account]} onChange={(e) => setText((t) => ({ ...t, [account]: e.target.value }))} />
        <AgentButton variant="primary" disabled={problem != null} onClick={() => setOpen(true)}>변경…</AgentButton>
        <span className="muted cap-next">{account === 'paper' ? `지금 바꾸면: ${info.next_paper_change_ko}` : '저장 · 미리보기만 (실주문 없음)'}</span>
      </div>
      {problem && <div className="muted cap-problem">{problem}</div>}
      <div className="toolbar" style={{ marginBottom: 0 }}>
        <span className="muted">유니버스</span>
        <AgentSegmentedControl value={universe} onChange={(v) => setUniverse(v as UniverseKind)} options={UNIVERSES} />
      </div>
      {universe === 'custom' && <AgentTextarea label="종목 코드 (쉼표 · 줄바꿈)" rows={2} value={tickers}
        placeholder={m === 'KRX' ? '6자리 코드.KS' : '티커'} onChange={(e) => setTickers(e.target.value)} />}
      {preview.error && <AgentAlert tone="error" title="미리보기 실패">{preview.error}</AgentAlert>}
      {preview.data && <PreviewBody p={preview.data} />}
      {open && amount != null && <CapitalModal info={info} account={account} amount={amount}
        onClose={(msg) => { setOpen(false); if (msg) onDone(msg) }} />}
    </div>
  )
}

/** The two capitals now: amount, where it comes from, and the cash flows the paper book took. */
export function CapitalRows({ info }: { info: CapitalInfo }) {
  const m = info.market
  const by = (s: CapitalInfo['paper']['set']) => (s ? `${utc(s.at)} · ${s.operator}: ${s.reason}` : null)
  return (
    <div className="matrix"><table>
      <thead><tr><th>계좌</th><th className="r">자본</th><th>출처</th><th>메모</th></tr></thead>
      <tbody>
        <tr className="cap-row"><td><b>{info.paper.label_ko}</b></td><td className="r num">{money(info.paper.amount, m)}</td>
          <td className="muted">{by(info.paper.set) ?? '기본값 (설정 기록 없음)'}</td>
          <td className="wrap">{info.paper.start !== info.paper.amount ? `시작 ${money(info.paper.start, m)} + 입출금 ${info.cash_flows.length}건` : ''}
            {info.paper.small_account && <span className="cap-small"> 소액 1종목 모드</span>}</td></tr>
        <tr className="cap-row"><td><b>{info.live.label_ko}</b></td>
          <td className="r num">{info.live.amount != null ? money(info.live.amount, m) : '미설정'}</td>
          <td className="muted">{by(info.live.set) ?? '설정 기록 없음'}</td>
          <td className="wrap">{info.live.small_account && <span className="cap-small">소액 1종목 모드</span>}
            {info.account_mode === 'paper' ? ' 지금 계좌는 모의투자 — 저장 · 미리보기만' : ''}</td></tr>
        {info.cash_flows.map((f, i) => (
          <tr key={`${f.date}-${i}`} className="cap-flow"><td className="muted">입출금 {f.date}</td>
            <td className={`r num ${f.amount < 0 ? 'down' : ''}`}>{f.amount > 0 ? '+' : '−'}{money(Math.abs(f.amount), m)}</td>
            <td className="muted">{f.operator ? `${f.operator}: ${f.reason}` : ''}</td>
            <td className="muted">{f.pending ? '그날 종가가 가격 데이터에 들어오면 장부에 반영' : '장부 반영됨'}</td></tr>
        ))}
      </tbody>
    </table></div>
  )
}

/** The server's preview: warnings, the universe readiness line, the rules at this capital, names over the per-order cap. */
export function PreviewBody({ p }: { p: CapitalPreview }) {
  const m = p.market
  const u = p.universe
  return (
    <div className="stack-gap" style={{ gap: '0.8rem' }}>
      {p.small_account && <AgentAlert tone="warning" title="소액 1종목 모드">
        {money(p.small_threshold, m)} 미만이라 종목당 {`상한`} 대신 1종목만 보유하고 자본 전체를 그 종목에 씁니다.</AgentAlert>}
      {p.none_buyable && <AgentAlert tone="error" title="살 수 있는 종목 없음">
        이 금액으로는 이 유니버스에서 1주도 살 수 없습니다{p.cheapest ? ` — 가장 싼 종목이 ${p.cheapest.name ?? p.cheapest.ticker} ${money(p.cheapest.price, m)}` : ''}.</AgentAlert>}
      <div className="muted cap-ready">{u.label} · 가격 {u.priced}/{u.size} · 섹터 {u.sector_ready}/{u.size} · 한글명 {u.name_ready}/{u.size} 준비됨 · {p.as_of} 종가
        {u.missing_count > 0 && ` · 가격 없음 ${u.missing.join(', ')}${u.missing_count > u.missing.length ? ' …' : ''}`}</div>
      <div className="matrix"><table>
        <thead><tr><th>규칙</th><th>설정</th><th className="r">이 자본에서</th><th>메모</th></tr></thead>
        <tbody>{p.rules.map((r) => (
          <tr key={r.id} className={r.warn ? 'cap-warn' : undefined}><td>{r.label_ko}</td><td className="muted num">{r.setting_ko}</td>
            <td className="r num">{r.value_ko}</td><td className="wrap muted">{r.note_ko}</td></tr>
        ))}</tbody>
      </table></div>
      {p.over_order_cap_count > 0 && <div className="muted">1주가 주문당 상한을 넘는 종목 {p.over_order_cap_count}개: {p.over_order_cap.map((x) =>
        `${x.name ?? x.ticker} ${money(x.price, m)}`).join(' · ')}{p.over_order_cap_count > p.over_order_cap.length ? ' …' : ''}</div>}
      <div className="muted">{p.note_ko}</div>
    </div>
  )
}

type Form = { operator: string; reason: string; token: string }

/** Confirm modal (the decision-cards pattern): what happens, the audit line, operator · reason · token required. */
function CapitalModal({ info, account, amount, onClose }: { info: CapitalInfo; account: CapitalAccount; amount: number; onClose: (msg?: string) => void }) {
  const [form, setForm] = useState<Form>({ operator: '', reason: '', token: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const ready = resumeReady({ ...form, tokenRequired: info.admin_token_required }) && !busy
  const submit = async () => {
    setBusy(true); setError(null)
    try {
      const r = await api.setCapital(info.market, { account, amount, operator: form.operator.trim(), reason: form.reason.trim() }, form.token || undefined)
      setDone(r.result_ko)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  const close = () => onClose(done ?? undefined)
  return (
    <AgentModal open onClose={close} title={`${info.market_ko} ${account === 'paper' ? '모의투자' : '실거래'} 자본을 바꿀까요?`}
      description="사유 필수 · 감사 로그에 남습니다 · 권한: 관리자"
      actions={done ? <AgentButton variant="primary" onClick={close}>닫기</AgentButton> : (
        <>
          <AgentButton variant="ghost" onClick={close} disabled={busy}>취소</AgentButton>
          <AgentButton variant="primary" disabled={!ready} onClick={submit}>자본 변경</AgentButton>
        </>
      )}>
      {done ? <AgentAlert tone="success" title="기록됨">{done}</AgentAlert>
        : <CapitalModalBody info={info} account={account} amount={amount} form={form} set={(patch) => setForm((f) => ({ ...f, ...patch }))}
          error={error} />}
    </AgentModal>
  )
}

/** The modal's content, apart so the tests render each change kind before and after the required inputs. */
export function CapitalModalBody({ info, account, amount, form, set, error }: {
  info: CapitalInfo; account: CapitalAccount; amount: number; form: Form; set: (patch: Partial<Form>) => void; error: string | null
}) {
  const fmt = (v: number) => money(v, info.market)
  const ready = resumeReady({ ...form, tokenRequired: info.admin_token_required })
  return (
    <div className="xa">
      <div className="xa-sec"><b>처리 결과</b>
        <ul>{changePreview(info, account, amount, fmt).map((t) => <li key={t}>{t}</li>)}</ul>
      </div>
      <div className="xa-sec"><b>감사 로그에 남는 문구</b><code className="xa-log">{capitalAuditLine(info, account, amount, form.operator, form.reason, fmt)}</code></div>
      <AgentTextField label="관리자" placeholder="이름 또는 ID" value={form.operator} onChange={(e) => set({ operator: e.target.value })} />
      <AgentTextField label="사유 (필수)" placeholder="감사 로그에 남습니다" value={form.reason} onChange={(e) => set({ reason: e.target.value })} />
      {info.admin_token_required && <AgentTextField label="관리자 토큰" type="password" value={form.token} onChange={(e) => set({ token: e.target.value })} />}
      {!ready && <div className="muted xa-need">관리자 · 사유{info.admin_token_required ? ' · 토큰' : ''}을 적어야 변경 버튼이 켜집니다</div>}
      {error && <AgentAlert tone="error" title="변경 실패">{error}</AgentAlert>}
    </div>
  )
}
