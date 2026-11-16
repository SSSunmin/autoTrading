// Mirrors backend/main.py responses (docs/04). Values stay English (rating, rule id, status); *_ko is for display.

export type Rating = 'Buy' | 'Overweight' | 'Hold' | 'Underweight' | 'Sell' | 'REVIEW'
/** config.MARKETS key: a separate book with its own currency, calendar and trading state (docs/08) */
export type Market = 'KRX' | 'US'
export type Mode = 'auto' | 'manual' | 'paper'
export type OrderStatus = 'filled' | 'partial' | 'open' | 'blocked' | 'rejected' | 'exception' | 'cancelled'
/** luld_halt · pdt_blocked: value and label only (the server cannot raise them without live data) */
export type ExceptionKind = 'review' | 'cap_exceeded' | 'order_rejected' | 'vendor_unavailable' | 'data_delayed' | 'luld_halt' | 'pdt_blocked'

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

/** One column of the decision card's 원안 vs 최종 grid (docs/10 단계 4). */
export interface PlanSide {
  side: 'Buy' | 'Sell' | 'Hold'
  weight_pct: number | null
  qty: number
  amount: number
}

/** A hard rule that cut (before → after) or blocked (note only) an order. */
export interface Intervention {
  rule: string
  before: string | null
  after: string | null
  note: string
}

export interface OrderPlan {
  proposed: PlanSide
  final: PlanSide
  interventions: Intervention[]
}

export type ExceptionAction = 'rerun' | 'hold' | 'dismiss' | 'retry_next' | 'dismiss_order'

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
  /** the session open it filled at (filled · partial) or will (open: the next trading day), market time and KST (ISO);
   *  null for an order that does not fill */
  fill_at_local: string | null
  fill_at_kst: string | null
  fill_planned: boolean
  /** null: written before decision cards, or not sized (already held, no close, a sell) */
  plan: OrderPlan | null
  exception_action: ExceptionAction | null
  exception_resolved: boolean
  reruns: number
  retries: number
  retry_on: string | null
  /** a REVIEW exception the daily loop closed by its rerun: the decision that answered it (null otherwise) */
  rerun_answer: RerunAnswer | null
}

export interface RerunAnswer {
  ticker: string
  trade_date: string
  rating: Rating
  rating_ko: string
  attempt: number
  /** the day's own decision on the ticker was the answer (no second decision written) */
  same_day: boolean
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
  /** agent: the manager's call; rule: a hard-rule exit (stop-loss, trailing, expiry, drawdown stop) the executor recorded;
   *  human: an operator's "보유로 처리" of an exception (Hold only, never scored) */
  source: 'agent' | 'rule' | 'human' | 'baseline'
  source_ko: string
  /** the card's source badge: the rule-only baseline writes source agent, told apart by the server */
  source_kind: 'agent' | 'rule' | 'human' | 'baseline'
  source_kind_ko: string
  /** a hard rule cut or blocked one of its orders (the 하드룰 개입 tab) */
  has_intervention: boolean
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
  market: Market
  /** the order the actions write to; null: a REVIEW decision the executor has not run yet */
  order_id: string | null
  actions: { action: ExceptionAction; label_ko: string; used: number | null; limit: number | null; available: boolean }[]
  pending_action: 'rerun' | 'retry_next' | null
  pending_action_ko: string | null
  reruns: number
  retries: number
}

export interface ExceptionActionResult {
  order: Order
  action: ExceptionAction
  action_ko: string
  replayed: boolean
  attempt: number | null
  limit: number | null
  result_ko: string
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
  /** the standing halt's scope as it was sent (all | KRX | US); null while running */
  halt_scope: 'all' | Market | null
  halt_scope_ko: string | null
  /** the session open on the day a daily-loss halt ends (KST ISO) and "07-28(화) 09:00 KST"; null for any other state */
  auto_resume_open_kst: string | null
  auto_resume_label_ko: string | null
}

/** GET /api/trading/resume-preview: the modal's variant, and after a −20% stop the three drawdown lines the resume
 *  would leave (the same peak-reset calculation the resume writes). */
export interface ResumePreview {
  market: Market
  variant: 'running' | 'manual' | 'daily_loss' | 'dd_stop'
  variant_ko: string
  can_resume: boolean
  reset_peak: boolean
  equity: number | null
  as_of: string | null
  peak_before: number | null
  base: number | null
  /** the base is the old peak and the equity already reads −20% from it: the next run halts again */
  restops: boolean
  lines: { id: 'peak' | 'dd_halve' | 'dd_stop'; label_ko: string; level: number; amount: number; vs_equity: number | null }[]
}

export type BookEventKind = 'guard' | 'lock' | 'protections' | 'intervention' | 'peak_reset'

/** GET /api/events: the guard / lock history, newest first. */
export interface BookEvents {
  market: Market
  total: number
  counts: Partial<Record<BookEventKind, number>>
  events: { date: string; kind: BookEventKind; kind_ko: string; ticker: string | null; name: string | null; text: string }[]
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

/** GET /api/performance/series: one day of the paper book (6-A). Percentages are fractions. */
export interface SeriesRow {
  date: string
  equity: number
  /** time-weighted NAV against the starting cash — a deposit or withdrawal is no gain */
  nav_pct: number
  /** that NAV against its running peak (≤ 0) */
  drawdown_pct: number
  /** holdings / equity */
  exposure_pct: number
  cash_pct: number
  /** the reference backtest's equity against its last row before the paper start; null where it does not reach */
  baseline_pct: number | null
}

export interface PerformanceSeries {
  market: Market
  currency: 'KRW' | 'USD'
  start: string | null
  as_of: string | null
  from: string | null
  to: string | null
  initial_cash: number
  rows: SeriesRow[]
  reference: { variant: string; variant_ko: string; measurement_ko: string; covered_until: string | null; error: string | null }
  cache: 'hit' | 'miss'
  price_source: PriceSource | null
  price_as_of: string | null
  price_provisional: boolean
  note_ko: string
}

/** /api/performance/loop (loop-scoring): one source's daily-loop decisions settled five sessions on, as
 *  scripts/llm_trial_report.py settles them. Fractions; null where nothing counts. */
export interface LoopColumn {
  source: 'agent' | 'rule'
  label_ko: string
  decisions: number
  ratings: Record<string, number>
  review: number
  stand_ins: number
  first: string | null
  last: string | null
  settled: number
  pending: number
  pending_by_reason: { id: 'window' | 'no_price' | 'no_bench' | 'price_ended'; label_ko: string; count: number }[]
  settle_failed: string[]
  buy_settled: number
  mean_raw: number | null
  mean_alpha: number | null
  alpha_by_rating: Record<string, { n: number; mean_alpha: number | null }>
  directional: { n: number; mean_return: number | null; mean_alpha: number | null; hit_rate: number | null }
  hold: { n: number; mean_alpha: number | null }
}

export interface LoopScoring {
  market: Market
  source: 'agent' | 'rule' | 'all'
  from: string | null
  to: string | null
  holding_days: number
  columns: Partial<Record<'agent' | 'rule', LoopColumn>>
  /** the rule column is the rule's call on these pairs (that day's screen through the rule) */
  agreement: { compared: number; agree: number; rate: number | null; no_screen: number }
  /** the rule's own records (outside the trial, stand-ins for the model): counted, in no column */
  rule_recorded: { decisions: number; first: string | null; last: string | null; stand_ins: number }
  trial: { sessions: number | null; days: string[]; remaining: number | null } | null
  human_excluded: number
  rule_exits_excluded: number
  note_ko: string
}

/** /api/performance rating bars (6-B): the scoring path's numbers, every rating listed (count 0 → nulls). */
export interface RatingBar {
  rating: Rating
  rating_ko: string
  count: number
  mean_alpha: number | null
  mean_raw: number | null
  hit_rate: number | null
}

export interface Settlement {
  failed: number
  by_reason: { id: 'no_price' | 'price_ended'; label_ko: string; count: number; tickers: string[] }[]
  waiting: number
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
  /** a person's "보유로 처리" decisions: never in the scores above, counted apart */
  human_handled: number
  caveat: string
  rating_bars: RatingBar[]
  settlement: Settlement
  by_source: { decisions: Record<string, number>; human: number; scored_source: string; scored: number; note_ko: string }
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
  /** the daily loop as the scheduler sees it (scheduler.plan · next_wake, computed by the server) */
  loop: LoopStatus
  /** the market's latest cache refresh (scripts/refresh_cache.py log); null: never refreshed */
  refresh: RefreshInfo | null
  price_source: PriceSource | null
  price_as_of: string | null
  price_provisional: boolean
}

/** kis: the KIS cache (default), yahoo: the Yahoo file (KIS has none for it), frames: test data */
export type PriceSource = 'kis' | 'yahoo' | 'frames'

export interface RunRow {
  market: Market
  stage: 'decide' | 'fill'
  stage_ko: string
  trade_date: string
  started_at: string
  ended_at: string
  status: 'ok' | 'skipped' | 'failed'
  status_ko: string
  reason: string | null
  error: string | null
  counts: Record<string, number>
  notes: string[]
}

export interface LoopStatus {
  /** never: no stage record yet; failed: a scheduled stage's latest record failed */
  status: 'ok' | 'failed' | 'never'
  status_ko: string
  last: Record<string, RunRow | null>
  failed: RunRow[]
  next: { at_kst: string; stage: 'decide' | 'fill'; stage_ko: string; label_ko: string }
  /** a due stage failed: the scheduler tries it again at this time (before the next planned stage) */
  retry_at_kst: string | null
}

export interface RefreshInfo {
  at: string | null
  source: string | null
  symbols: number | null
  last_final_day: string | null
  failed_count: number
  failed_symbols: string[]
  failed_error: string | null
  /** the latest refresh of the market with no symbol failed; null: none */
  last_success_at: string | null
}

export interface VendorStatus {
  id: string
  label_ko: string
  use_ko: string
  key_required: boolean
  /** environment name → is it set. Never a value */
  keys: Record<string, boolean>
  state: 'reachable' | 'unreachable' | 'no_key' | 'unknown'
  state_ko: string
  last_refresh_at: string | null
}

export interface DataStatus {
  cache: Record<'kis' | 'yahoo', { files: number; kb: number }>
  refresh: Record<Market, RefreshInfo | null>
  vendors: VendorStatus[]
  price_source: Record<Market, { price_source: PriceSource | null; price_as_of: string | null; price_provisional: boolean }>
  /** the market's last daily-loop run's price fallback lines (RunRecord.notes); null when it had none */
  price_fallback: Record<Market, { stage: string; trade_date: string; ended_at: string; notes: string[] } | null>
  note: string
}

export interface SectorRow {
  /** the sector file's name, or 미분류 (the sum of holdings it gives none) */
  sector: string
  pct: number
  classified: boolean
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
  exception_action_ko: Record<string, string>
  /** the rule's exit reasons (stop_loss · trailing · expired · exit_signal · force), not the agent's sell reasons */
  exit_reason_ko: Record<string, string>
  halt_target_ko: Record<string, string>
  market_ko: Record<Market, string>
  halt_scope_ko: Record<string, string>
  book_event_ko: Record<string, string>
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
  /** null when the market's holiday calendar could not be read (/api/markets ``schedule_error``) */
  session_kst: string | null
  name_style: 'name' | 'name_ticker'
}

/** /api/markets: one card per book on the home screen. Each in its own currency; no total. */
/** One home card. A broken book of that market gives ``error`` and nulls; the other card still loads. */
export interface MarketSummary extends MarketInfo {
  as_of: string | null
  equity: number | null
  initial_cash: number | null
  /** starting cash + cash flows (capital_set, docs/10 D5) */
  capital: number | null
  day_return: number | null
  drawdown: number | null
  positions: number | null
  state: TradingState | null
  /** home flow cards, counted by the server; null with ``error`` */
  flow: MarketFlow | null
  error: string | null
  /** null: the next run and today's session are unknown — the calendar file is broken (no weekday guess) */
  next_run_kst: string | null
  next_run_stage: 'decide' | 'fill' | null
  next_run_label_ko: string
  schedule_error: string | null
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
  /** the screen per market: the US differs by its preset only (screening.us_preset) */
  screen_params_by_market: Record<Market, Record<string, number | string>>
  regime_index: Record<Market, { benchmark: string; name_ko: string }>
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
  /** the guard's own window in trading days and its triggers inside it, up to as_of */
  lookback_days: number
  lookback_hits: number
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
  /** starting cash + the cash flows to as_of (capital_set while running) */
  capital: number
  cash_flows: { date: string; amount: number }[]
  small_account: boolean
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
            daily_loss_limit: number; auto_daily_order_cap: number; near_stop_pct: number; regime_ma_days: number;
            max_sector_pct: number }
  note: string
  mode: TradingState
  /** held weight by sector: the three largest, then 미분류 (empty with no holdings) */
  sector_exposure: SectorRow[]
  price_source: PriceSource | null
  price_as_of: string | null
  price_provisional: boolean
  /** holdings priced from another file than the market's (Yahoo where KIS has none) */
  price_fallback: string[]
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
  /** starting cash + cash flows to as_of (capital_set while running) */
  capital: number
  cash_flows: { date: string; amount: number }[]
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
    capital: number; capital_source: 'live' | 'paper'; capital_source_ko: string; auto_order_max: number; auto_daily_order_cap: number
    daily_loss_limit: number; daily_loss_amount: number; other_markets_ko: string[]; note_ko: string
  }
  expected: PaperExpected | null
}

/** GET /api/capital (docs/10 D5): both accounts' capital replayed from capital_set operations. */
export type CapitalAccount = 'paper' | 'live'
export interface CapitalSetBy { at: string; operator: string; role: string; reason: string }
export interface CapitalInfo {
  market: Market
  market_ko: string
  currency: 'KRW' | 'USD'
  paper: { account: 'paper'; label_ko: string; amount: number; start: number; source: 'record' | 'default'; set: CapitalSetBy | null; small_account: boolean }
  live: { account: 'live'; label_ko: string; amount: number | null; source: 'record' | 'unset'; set: CapitalSetBy | null; small_account: boolean | null }
  cash_flows: ({ date: string; amount: number; pending: boolean } & Partial<CapitalSetBy>)[]
  /** what a paper change does now: no order yet → a new starting cash, else a cash flow at today's close */
  next_paper_change: 'start' | 'flow'
  next_paper_change_ko: string
  small_threshold: number
  max_amount: number
  unit: number
  account_mode: CapitalAccount
  admin_token_required: boolean
  note_ko: string
}
export interface CapitalResult {
  replayed: boolean
  change: 'start' | 'flow' | 'live'
  change_ko?: string
  flow: number | null
  effective_from: string | null
  capital: CapitalInfo
  result_ko: string
}
export type UniverseKind = 'loop' | 'index' | 'custom'
export interface PreviewRule { id: string; label_ko: string; setting_ko: string; amount: number | null; value_ko: string; note_ko: string; warn: boolean }
export interface PreviewName { ticker: string; name: string | null; price: number }
export interface CapitalPreview {
  market: Market
  currency: 'KRW' | 'USD'
  account: CapitalAccount
  account_ko: string
  amount: number
  small_account: boolean
  small_threshold: number
  as_of: string
  universe: { id: UniverseKind; label_ko: string; label: string; size: number; priced: number; missing: string[]; missing_count: number; sector_ready: number; name_ready: number }
  buyable: number
  none_buyable: boolean
  cheapest: PreviewName | null
  over_order_cap: PreviewName[]
  over_order_cap_count: number
  rules: PreviewRule[]
  note_ko: string
}
/** GET /api/model: config.LLM, read only. */
export interface ModelSettings {
  decider: 'rule' | 'llm'
  decider_ko: string
  decider_id: string
  model: string | null
  effort: string | null
  max_tokens: number | null
  timeout: number | null
  max_calls_per_run: number | null
  market: Market
  enabled: boolean
  /** the LLM trial (TRADING_LLM_TRIAL=1): model decide days in this market and how many are left; null when off */
  trial: { sessions: number | null; days: string[]; remaining: number | null } | null
  trial_mode: boolean
  config: Record<string, unknown>
  keys: Record<string, boolean>
  output_language: string
  note_ko: string
}
