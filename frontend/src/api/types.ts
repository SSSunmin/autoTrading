// Mirrors backend/main.py responses (docs/04). Values stay English (rating, rule id, status); *_ko is for display.

export type Rating = 'Buy' | 'Overweight' | 'Hold' | 'Underweight' | 'Sell' | 'REVIEW'
/** config.MARKETS key: a separate book with its own currency, calendar and trading state (docs/08) */
export type Market = 'KRX' | 'US'
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
  rule_count: number
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
  market: Market
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
  /** agent: the manager's call; rule: a hard-rule exit (stop-loss, trailing, expiry, drawdown stop) the executor recorded */
  source: 'agent' | 'rule'
  source_ko: string
  market: Market
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
  /** "시스템 자동 정지" for the executor's daily-loss halt, else the operator's name */
  halted_by_ko: string | null
  halt_reason: string | null
  keep_exits: boolean
  mode_changed_at: string | null
  label_ko: string
  admin_token_required: boolean
  market: Market
  market_ko: string
  /** what the standing halt is: a person's, the daily loss (lifted by the executor next session), the −20% stop */
  halt_target: 'manual' | 'daily_loss' | 'dd_stop' | null
  halt_target_ko: string | null
  /** a −20% stop stands (perhaps under a person's halt): the admin resume offers the peak reset */
  dd_stop: boolean
  /** a daily-loss halt ends at the next trading day's 00:00, market time (ISO); null when none stands */
  auto_resume_at: string | null
  /** that time has come and the executor has not written its resume line yet */
  auto_resumed: boolean
  /** the stored mode on two axes (docs/10 D4): account paper | live, execution auto | manual approval */
  account_mode: 'paper' | 'live'
  account_mode_ko: string
  execution_mode: 'auto' | 'manual'
  execution_mode_ko: string
}

export interface HaltEffects { new_orders: string; open_orders_cancelled: number | null; positions: string; exits_active: boolean; note: string }

/** scope all: one result per market that was running (same at, same reason); markets already halted are skipped. */
export interface HaltResult {
  scope: 'all' | Market
  scope_ko: string
  results: { market: Market; state: TradingState; effects: HaltEffects; error: string | null }[]
  /** already halted, left as is; ``keep_exits`` true means that market's exits keep running */
  skipped: { market: Market; reason: string; keep_exits: boolean }[]
}

export interface ResumeResult {
  scope: 'all' | Market
  scope_ko: string
  resumed: Market[]
  skipped: { market: Market; reason: string }[]
  states: Partial<Record<Market, TradingState>>
  /** markets whose drawdown peak the resume reset: to the equity at the close ``at``, from the peak ``before`` */
  peak_resets: Partial<Record<Market, { to: number; at: string; before: number }>>
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
  market: Market
  market_ko: string
  markets: Market[]
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
  /** the rule's exit reasons (stop_loss · trailing · expired · exit_signal · force), not the agent's sell reasons */
  exit_reason_ko: Record<string, string>
  halt_target_ko: Record<string, string>
  market_ko: Record<Market, string>
  halt_scope_ko: Record<string, string>
  markets: Record<Market, MarketInfo>
  color_convention: { up: string; down: string; note: string }
}

/** /api/labels markets[m]: what a screen needs to show a market (currency, index, session; KST converted on screen). */
export interface MarketInfo {
  market: Market
  label_ko: string
  currency: 'KRW' | 'USD'
  currency_symbol: string
  benchmark: string
  benchmark_name_ko: string
  timezone: string
  session: string
  session_kst: string
  name_style: 'name' | 'name_ticker'
}

/** /api/markets: one card per book on the home screen. Each in its own currency; no total. */
/** One home card. A broken book of that market gives ``error`` and nulls; the other card still loads. */
export interface MarketSummary extends MarketInfo {
  as_of: string | null
  equity: number | null
  initial_cash: number | null
  day_return: number | null
  drawdown: number | null
  positions: number | null
  state: TradingState | null
  /** home flow cards, counted by the server; null with ``error`` */
  flow: MarketFlow | null
  error: string | null
  next_run_kst: string
  next_run_label_ko: string
}

export interface MarketFlow {
  screen: { run_id: string; as_of: string; universe: string; universe_size: number; selected_count: number } | null
  /** the latest decision day, by rating (5 tiers then REVIEW, only those present) */
  decisions: { date: string | null; total: number; by_rating: Record<string, number>; by_rating_ko: Record<string, number>; rule: number }
  /** that day's exception queue (/api/exceptions default), by kind */
  exceptions: { date: string | null; total: number; by_kind: Record<string, number>; by_kind_ko: Record<string, number> }
  holdings: { count: number; near_stop: number; protections_active: number }
}

export interface RuleRow {
  id: string
  label_ko: string
  description_ko?: string
  value?: number | string
  layer: 'screen' | 'auto_execution'
  layer_ko: string
  /** auto-execution amounts: the market's currency; null for fractions and settings */
  currency?: 'KRW' | 'USD' | null
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
  market: Market
  market_ko: string
  scope: string | null
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
  auto_by_market: Record<Market, Record<string, number | string>>
  paper_criteria: Criterion[]
  market: Record<string, number | string | boolean | null>
  market_code: Market
  markets: Record<Market, Record<string, number | string | boolean | null>>
  mode: TradingState
  change_history: OperationRow[]
  history_errors: string[]
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
  market?: Market
  currency?: string
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
  /** last close vs the stop line (0.004 = 0.4% above it); null without a close */
  stop_gap: number | null
  /** within limits.near_stop_pct of the stop line */
  near_stop: boolean
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
  /** the last peak reset in effect (an admin resume after a −20% stop), null before any */
  peak_reset: { at: string; to: number } | null
  market: Market
  currency: 'KRW' | 'USD'
  as_of: string
  /** vs the previous close; null off a trading day or before the first fill */
  day_return: number | null
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
  /** the index's last close vs its limits.regime_ma_days average; null with fewer bars than the window */
  regime_gap: number | null
  positions: PositionRow[]
  protections: ProtectionState[]
  protections_active: number
  near_stop_count: number
  max_position_weight: number
  max_position_ticker: string | null
  /** the session orders decided on as_of fill at (null: not in the data yet) and every order filling there */
  today_session: string | null
  today_buy_amount: number
  today_sell_amount: number
  /** buys against the daily cap (the cap counts buys only) */
  today_buy_cap_used: number | null
  exits_by_reason: Record<string, number>
  exits_by_reason_ko: Record<string, number>
  closed: (ClosedTrade & { reason: string })[]
  limits: { stop_loss_pct: number; dd_halve: number; dd_stop: number; max_position_pct: number; max_gross_pct: number;
            daily_loss_limit: number; auto_daily_order_cap: number; near_stop_pct: number; regime_ma_days: number }
  note: string
  mode: TradingState
}

/** GET /api/paper/progress — screen 8, judged by the server (docs/05 §4). */
export type PaperStatus = 'met' | 'unmet' | 'in_progress'

export interface PaperCriterion {
  id: string
  label_ko: string
  threshold: number
  threshold_ko: string
  value: number | null
  value_ko: string
  status: PaperStatus
  status_ko: string
  note_ko: string
  numerator?: number
  denominator?: number
}

export interface PaperRef { type: 'decision' | 'positions' | 'order'; date: string | null; ticker?: string; order_id?: string }

export interface PaperViolation {
  date: string | null
  kind: 'review' | 'daily_loss' | 'dd_stop' | 'unexplained_order'
  kind_ko: string
  ticker: string | null
  name: string | null
  value: number | null
  text: string
  ref: PaperRef
}

export interface PaperNote {
  date: string
  kind: 'order_exception' | 'stop_loss'
  kind_ko: string
  ticker: string
  name: string | null
  text: string
  ref: PaperRef
}

export interface PaperExpectedRow {
  id: 'mean_exposure' | 'new_buys' | 'exits' | 'period_return' | 'max_drawdown' | 'daily_loss_guard' | 'index_return'
  label_ko: string
  unit: 'pct' | 'count'
  reference: number | null
  paper: number | null
  /** paper − reproduction; null where a difference means nothing (the index row) */
  diff: number | null
  paper_open?: number
  reference_by_reason?: Record<string, number>
  paper_by_reason?: Record<string, number>
  reference_dates?: string[]
  paper_dates?: string[]
}

export interface PaperExpected {
  /** never part of the verdict (docs/10 D6) */
  reference_only: true
  measurement_ko: string
  variant_ko: string
  start: string
  end: string
  covered_until: string | null
  rows: PaperExpectedRow[]
  error: string | null
}

export interface PaperProgress {
  market: Market
  market_ko: string
  currency: 'KRW' | 'USD'
  timezone: string
  benchmark_name_ko: string
  started: boolean
  start: string | null
  as_of: string | null
  days: number | null
  weeks: number | null
  min_weeks: number
  judge_date: string | null
  initial_cash: number
  eligible: boolean
  counts: Record<PaperStatus, number>
  summary_ko: string
  verdict_ko: string
  weeks_badge_ko: string
  criteria: PaperCriterion[]
  violations: PaperViolation[]
  notes: PaperNote[]
  series: { date: string; equity: number; drawdown: number }[]
  drawdown: { current: number; min: number; min_date: string | null } | null
  limits: { max_drawdown: number; dd_halve: number; dd_stop: number }
  go_live: {
    capital: number; capital_source_ko: string; auto_order_max: number; auto_daily_order_cap: number
    daily_loss_limit: number; daily_loss_amount: number; other_markets_ko: string[]; note_ko: string
  }
  expected: PaperExpected | null
}
