// Mirrors backend/main.py responses (docs/04). Values stay English (rating, rule id, status); *_ko is for display.

export type Rating = 'Buy' | 'Overweight' | 'Hold' | 'Underweight' | 'Sell' | 'REVIEW'
export type Mode = 'auto' | 'manual' | 'paper'
export type OrderStatus = 'filled' | 'partial' | 'open' | 'blocked' | 'rejected' | 'exception' | 'cancelled'
export type ExceptionKind = 'review' | 'cap_exceeded' | 'order_rejected' | 'vendor_unavailable'

export interface FilterResult {
  rule: string
  value: number | string | null
  threshold: number | string | null
  passed: boolean
  note: string
  rule_ko?: string
}

export interface Candidate {
  ticker: string
  name: string | null
  filters: FilterResult[]
  score: number | null
  rank: number | null
  selected: boolean
  failed_rules: string[]
  failed_rules_ko: string[]
}

export interface ScreenSummary {
  run_id: string
  as_of: string
  recorded_at: string
  universe: string
  universe_size: number
  selected_count: number
  rules: string[]
  rules_ko: string[]
  stage_counts: number[]
}

export interface ScreenDetail extends ScreenSummary {
  candidates: Candidate[]
}

export interface Evidence {
  source: 'market' | 'fundamentals' | 'news' | 'sentiment' | 'macro' | 'memory' | 'portfolio' | 'other'
  summary: string
  as_of: string | null
  ref: string | null
}

export interface Outcome {
  pending: boolean
  raw_return: number | null
  alpha: number | null
  holding: string | null
  resolved: string | null
  reflection: string
}

/** One order the paper execution layer placed or refused (docs/02 v0.3 §9), in its current state. */
export interface Order {
  order_id: string
  ticker: string
  name: string | null
  trade_date: string
  placed_at: string
  side: 'Buy' | 'Sell'
  side_ko: string
  quantity: number
  limit_price: number | null
  filled_quantity: number
  filled_price: number | null
  fill_date: string | null
  amount: number | null
  status: OrderStatus
  status_ko: string
  exception_kind: ExceptionKind | null
  reason: string
  rules_applied: string[]
}

export interface Decision {
  ticker: string
  name: string | null
  trade_date: string
  recorded_at: string
  screen_run_id: string | null
  rating: Rating
  rating_ko: string
  action: 'Buy' | 'Hold' | 'Sell' | null
  entry_price: number | null
  stop_loss: number | null
  position_sizing: string | null
  confidence: number | null
  thesis: string
  evidence: Evidence[]
  hard_rule_overrides: string[]
  settings: Record<string, unknown>
  tradeable: boolean
  outcome: Outcome | null
  orders: Order[]
}

/** Exception queue item: a REVIEW decision or a refused order a human must look at. */
export interface ExceptionItem {
  kind: ExceptionKind
  kind_ko: string
  ticker: string
  name: string | null
  trade_date: string
  summary: string
  detail: string
  ref: string | null
}

export interface TradingState {
  mode: Mode
  mode_ko: string
  running: boolean
  halted_at: string | null
  halted_by: string | null
  halt_reason: string | null
  keep_exits: boolean
  mode_changed_at: string | null
  label_ko: string
  admin_token_required: boolean
}

export interface HaltResult {
  state: TradingState
  effects: { new_orders: string; open_orders_cancelled: number | null; positions: string; exits_active: boolean; note: string }
}

export interface TickerRow {
  ticker: string
  name: string | null
  latest_selected: boolean | null
  latest_rank: number | null
}

export interface ScreeningHistoryRow {
  run_id: string
  as_of: string
  selected: boolean
  rank: number | null
  score: number | null
  filters: FilterResult[]
  first_failed_rule: string | null
  first_failed_rule_ko: string | null
}

export interface TickerDetail {
  ticker: string
  name: string | null
  screening: ScreeningHistoryRow[]
  decisions: Decision[]
  outcomes: (Outcome & { trade_date: string; rating: Rating; rating_ko: string })[]
  orders: Order[]
  timeline: { date: string; rating: Rating }[]
}

export interface RatingScore {
  count: number
  hit_rate: number | null
  mean_alpha: number
  rating_ko: string
}

export interface Reflection {
  date: string
  ticker: string
  name: string | null
  rating: Rating
  rating_ko: string
  raw_return: number | null
  alpha: number | null
  resolved: string | null
  reflection: string
}

export interface Performance {
  run_id: string
  cells_run: number
  resolved: number
  pending: number
  unscored: number
  holding: string
  dates: string[]
  universe: string[]
  by_rating: Record<string, RatingScore>
  per_date: { date: string; resolved: number; long_mean_alpha: number | null; short_mean_alpha: number | null }[]
  reflections: Reflection[]
  caveat: string
}

export interface Health {
  ok: boolean
  core_version: string
  screens: number
  decisions: number
  orders: number
  memory_entries: number
  server_time: string
}

export interface Labels {
  ratings: string[]
  rating_ko: Record<string, string>
  rule_ko: Record<string, string>
  rule_desc_ko: Record<string, string>
  sell_reason_ko: Record<string, string>
  mode_ko: Record<string, string>
  order_status_ko: Record<string, string>
  exception_kind_ko: Record<string, string>
}

export interface RuleRow {
  id: string
  label_ko: string
  description_ko?: string
  value?: number | string
  layer: 'screen' | 'auto_execution'
  layer_ko: string
}

export interface OperationRow {
  kind: 'halt' | 'resume' | 'mode_change' | 'rule_change'
  kind_ko: string
  at: string
  operator: string
  role: string
  reason: string
  target: string
  before: Record<string, unknown>
  after: Record<string, unknown>
}

export interface Criterion {
  id: string
  label_ko: string
  value: number | string
}

export interface RulesSettings {
  screen_rules: RuleRow[]
  screen_params: Record<string, number | string>
  risk_rules: RuleRow[]
  auto_rules: RuleRow[]
  paper_criteria: Criterion[]
  market: Record<string, number | string>
  mode: TradingState
  change_history: OperationRow[]
}

/** 2nd measurement (docs/05): the same book with the hard rules off / on / on + regime filter. */
export interface SimMetrics {
  total_return: number
  cagr: number
  max_drawdown: number
  dd_peak: string
  dd_trough: string
  dd_recovery: string | null
  dd_length_days: number
  dd_recovery_days: number | null
  worst_day: number
  worst_day_date: string
  worst_20d: number | null
  volatility: number
  sharpe: number
  days: number
}

export interface WindowMetric {
  start: string
  end: string
  return: number
  max_drawdown: number
}

export interface PaperVariant {
  label: string
  label_ko: string
  risk: Record<string, number> | null
  regime_filter: boolean
  rebalances: number
  metrics: SimMetrics
  windows: Record<string, WindowMetric>
  exits_by_reason: Record<string, number>
  exits_by_reason_ko: Record<string, number>
  trades: number
  win_rate: number | null
  mean_exposure: number
  event_counts: Record<string, number>
  events_tail: { date: string; kind: string; text: string }[]
  trades_sample: { ticker: string; entry_date: string; exit_date: string; reason: string; pnl_pct: number; holding_days: number }[]
}

export interface PaperSummary {
  start: string
  end: string
  universe: string[]
  universe_ko: Record<string, string | null>
  index: string
  windows: Record<string, [string, string]>
  benchmark: { metrics: SimMetrics; windows: Record<string, WindowMetric> }
  variants: Record<string, PaperVariant>
  caveats: string[]
  criteria: Criterion[]
}

/** 5 and the home KPIs: the paper book replayed from the orders log (agent_trading_core/execution.positions_view). */
export interface PositionRow {
  ticker: string
  name: string | null
  quantity: number
  avg_price: number
  entry_date: string
  last_price: number | null
  last_date: string | null
  market_value: number | null
  unrealized: number | null
  unrealized_pct: number | null
  weight: number | null
  stop_price: number
  trailing_active: boolean
  trailing_high: number
  trailing_stop_price: number | null
  holding_days: number
  expiry_date: string | null
  exit_signal: { reason: string; note: string } | null
}

export interface ProtectionState {
  id: 'StoplossGuard' | 'MaxDrawdown' | 'Cooldown' | 'LowProfit'
  label_ko: string
  active: boolean
  detail: string | null
  until: string | null
  tickers: { ticker: string; until: string | null }[]
}

export interface ClosedTrade {
  ticker: string
  name: string | null
  entry_date: string
  exit_date: string
  quantity: number
  entry_price: number
  exit_price: number
  pnl: number
  pnl_pct: number
}

export interface Positions {
  as_of: string
  initial_cash: number
  cash: number
  holdings_value: number
  equity: number
  peak: number
  drawdown: number
  gross_exposure: number
  realized_pnl: number
  realized_today: number
  unrealized_pnl: number
  regime_on: boolean | null
  positions: PositionRow[]
  protections: ProtectionState[]
  exits_by_reason: Record<string, number>
  exits_by_reason_ko: Record<string, number>
  closed: ClosedTrade[]
  limits: { stop_loss_pct: number; dd_halve: number; dd_stop: number; max_position_pct: number; max_gross_pct: number;
            daily_loss_limit: number; auto_daily_order_cap: number }
  note: string
  mode: TradingState
}
