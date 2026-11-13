import type {
  BookEvents, Decision, ExceptionItem, HaltResult, Health, Labels, Market, MarketSummary, Mode, Order, PaperProgress, PaperSummary, Performance, Positions,
  ResumePreview, ResumeResult, RulesSettings, ScreenDetail, ScreenSummary, TickerDetail, TickerRow, TradingState,
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

/** Every read names its market (docs/08: separate books); a caller lists `market` in its useApi deps, so a response
 *  for the market just left is dropped (useApi's alive flag) instead of painting over the new one. */
export const api = {
  health: (market: Market) => get<Health>('/api/health', { market }),
  labels: () => get<Labels>('/api/labels'),
  markets: () => get<MarketSummary[]>('/api/markets'),
  screens: (market: Market, params?: { from?: string; to?: string; universe?: string }) =>
    get<ScreenSummary[]>('/api/screens', { ...params, market }),
  screen: (market: Market, runId: string) => get<ScreenDetail>(`/api/screens/${encodeURIComponent(runId)}`, { market }),
  decisionDates: (market: Market) => get<string[]>('/api/decisions/dates', { market }),
  decisions: (market: Market, params?: { date?: string; rating?: string; review_only?: boolean; overrides_only?: boolean; ticker?: string }) =>
    get<Decision[]>('/api/decisions', { ...params, market }),
  orders: (market: Market, params?: { date?: string; status?: string; ticker?: string }) => get<Order[]>('/api/orders', { ...params, market }),
  exceptions: (market: Market, params?: { date?: string; all_dates?: boolean }) =>
    get<ExceptionItem[]>('/api/exceptions', { ...params, market }),
  tickers: (market: Market) => get<TickerRow[]>('/api/tickers', { market }),
  ticker: (market: Market, ticker: string) => get<TickerDetail>(`/api/tickers/${encodeURIComponent(ticker)}`, { market }),
  performance: (market: Market) => get<Performance>('/api/performance', { market }),
  paper: (market: Market) => get<PaperSummary>('/api/paper', { market }),
  paperProgress: (market: Market) => get<PaperProgress>('/api/paper/progress', { market }),
  positions: (market: Market, params?: { as_of?: string }) => get<Positions>('/api/positions', { ...params, market }),
  events: (market: Market, limit = 50) => get<BookEvents>('/api/events', { market, limit }),
  resumePreview: (market: Market, resetPeak: boolean) => get<ResumePreview>('/api/trading/resume-preview', { market, reset_peak: resetPeak }),
  rules: (market: Market) => get<RulesSettings>('/api/settings/rules', { market }),
  mode: (market: Market) => get<TradingState>('/api/trading/mode', { market }),
  halt: (body: { operator: string; reason: string; keep_exits: boolean; scope: 'all' | Market }) => post<HaltResult>('/api/trading/halt', body),
  resume: (body: { operator: string; reason: string; scope: 'all' | Market; reset_peak?: boolean }, adminToken?: string) =>
    post<ResumeResult>('/api/trading/resume', body, adminToken),
  setMode: (body: { operator: string; mode: Mode; reason: string; market: Market }, adminToken?: string) =>
    post<{ state: TradingState; note: string | null }>('/api/trading/mode', body, adminToken),
  /** The account axis only (docs/10 D4). Paper → live is taken only when the server's gate is eligible, with `confirmed`. */
  setAccount: (body: { operator: string; account_mode: 'paper' | 'live'; reason: string; market: Market; confirmed: boolean }, adminToken?: string) =>
    post<{ state: TradingState; note: string | null }>('/api/trading/mode', body, adminToken),
}

/** Fired after a write so the status bar and settings reload the trading state without a page refresh. */
export const STATE_CHANGED = 'trading-state-changed'
export function announceStateChange() { window.dispatchEvent(new Event(STATE_CHANGED)) }

/** 0.0123 → "+1.2%". null → "–". */
export function pct(v: number | null | undefined, digits = 1): string {
  if (v == null || Number.isNaN(v)) return '–'
  return `${v > 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
}

/** config.MARKETS[market].currency — a book's amounts are in its own currency, never converted (docs/08 §0). */
export const CURRENCY: Record<Market, 'KRW' | 'USD'> = { KRX: 'KRW', US: 'USD' }
const MONEY = {
  KRW: new Intl.NumberFormat('ko-KR', { style: 'currency', currency: 'KRW', maximumFractionDigits: 0 }),
  USD: new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 }),
}

/** An amount of `market`'s book: ₩1,234,567 (whole won) / $1,234.56. null → "–". Every amount on screen goes through here. */
export function money(v: number | null | undefined, market: Market): string {
  if (v == null || Number.isNaN(v)) return '–'
  return MONEY[CURRENCY[market]].format(v)
}

/** Instrument in running text (docs/08 §4 Q4): US "애플 AAPL", KRX the Korean name only. */
export function stockName(ticker: string, name: string | null | undefined, market: Market): string {
  if (market === 'US') return name ? `${name} ${ticker}` : ticker
  return name ?? ticker
}

export function utc(iso: string | null | undefined): string {
  return iso ? `${iso.slice(0, 16).replace('T', ' ')} UTC` : '–'
}
