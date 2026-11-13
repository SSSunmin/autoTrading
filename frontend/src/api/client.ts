import type {
  Decision, ExceptionItem, HaltResult, Health, Labels, Mode, Order, PaperSummary, Performance, Positions, RulesSettings, ScreenDetail,
  ScreenSummary, TickerDetail, TickerRow, TradingState,
} from './types'

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function parse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body)
    } catch { /* keep statusText */ }
    throw new ApiError(res.status, detail)
  }
  return res.json() as Promise<T>
}

async function get<T>(path: string, params?: Record<string, string | number | boolean | undefined>): Promise<T> {
  const url = new URL(BASE + path, window.location.origin)
  for (const [k, v] of Object.entries(params ?? {})) if (v !== undefined && v !== '') url.searchParams.set(k, String(v))
  return parse<T>(await fetch(url.toString()))
}

/** The three write actions the plan allows (docs/02 §0-5). `adminToken` is only needed when the server has one set. */
async function post<T>(path: string, body: unknown, adminToken?: string): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (adminToken) headers['X-Admin-Token'] = adminToken
  return parse<T>(await fetch(new URL(BASE + path, window.location.origin).toString(), { method: 'POST', headers, body: JSON.stringify(body) }))
}

export const api = {
  health: () => get<Health>('/api/health'),
  labels: () => get<Labels>('/api/labels'),
  screens: (params?: { from?: string; to?: string; universe?: string }) => get<ScreenSummary[]>('/api/screens', params),
  screen: (runId: string) => get<ScreenDetail>(`/api/screens/${encodeURIComponent(runId)}`),
  decisionDates: () => get<string[]>('/api/decisions/dates'),
  decisions: (params?: { date?: string; rating?: string; review_only?: boolean; overrides_only?: boolean; ticker?: string }) =>
    get<Decision[]>('/api/decisions', params),
  orders: (params?: { date?: string; status?: string; ticker?: string }) => get<Order[]>('/api/orders', params),
  exceptions: (params?: { date?: string; all_dates?: boolean }) => get<ExceptionItem[]>('/api/exceptions', params),
  tickers: () => get<TickerRow[]>('/api/tickers'),
  ticker: (ticker: string) => get<TickerDetail>(`/api/tickers/${encodeURIComponent(ticker)}`),
  performance: () => get<Performance>('/api/performance'),
  paper: () => get<PaperSummary>('/api/paper'),
  positions: (params?: { as_of?: string }) => get<Positions>('/api/positions', params),
  rules: () => get<RulesSettings>('/api/settings/rules'),
  mode: () => get<TradingState>('/api/trading/mode'),
  halt: (body: { operator: string; reason: string; keep_exits: boolean }) => post<HaltResult>('/api/trading/halt', body),
  resume: (body: { operator: string; reason: string }, adminToken?: string) =>
    post<{ state: TradingState }>('/api/trading/resume', body, adminToken),
  setMode: (body: { operator: string; mode: Mode; reason: string }, adminToken?: string) =>
    post<{ state: TradingState }>('/api/trading/mode', body, adminToken),
}

/** Fired after a write so the status bar and settings reload the trading state without a page refresh. */
export const STATE_CHANGED = 'trading-state-changed'
export function announceStateChange() { window.dispatchEvent(new Event(STATE_CHANGED)) }

/** 0.0123 → "+1.2%". null → "–". */
export function pct(v: number | null | undefined, digits = 1): string {
  if (v == null || Number.isNaN(v)) return '–'
  return `${v > 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
}

export function won(v: number | null | undefined): string {
  if (v == null) return '–'
  return v.toLocaleString('ko-KR', { maximumFractionDigits: Math.abs(v) < 1000 ? 2 : 0 })
}

export function utc(iso: string | null | undefined): string {
  return iso ? `${iso.slice(0, 16).replace('T', ' ')} UTC` : '–'
}
