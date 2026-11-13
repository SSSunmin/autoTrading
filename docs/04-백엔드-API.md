# 백엔드 API (기획서 v0.3 기준)

구현 `backend/main.py` (FastAPI) · 실행 `.venv\Scripts\python -m uvicorn backend.main:app --reload --port 8000` · 자동 문서 `http://127.0.0.1:8000/docs`

원칙은 기획서 §0과 같다. 화면이 보여주는 숫자는 전부 기록(`data/audit/<시장>/*.jsonl`, 메모리 로그, `baseline_summary.json`)에서 읽고 서버가 그대로 서빙한다. 한글 라벨(규칙명·등급명·종목명·주문 상태·운용 모드)은 서버가 붙여 주므로 프론트는 번역 테이블을 갖지 않는다. 데이터의 **값**(rating `Buy`, rule id `price_min`, status `filled`)은 영문 고정이고, 표시용 `*_ko` 필드가 따로 온다.

쓰기는 기획서 §0-5의 세 가지만 있다: **긴급 정지**, **재가동**, **운용 모드 변경**. 셋 다 그 시장의 `operations.jsonl`에 append-only로 기록되고, 현재 상태는 그 기록을 재생해서 만든다(`audit.trading_state`). 상태를 따로 저장하지 않으므로 상태와 감사 이력이 어긋날 수 없다.

## 시장 축 (docs/08)

국내(`KRX`)와 미국(`US`)은 **별도 포트폴리오**다. 기록·장부·운용 상태·모드·자동 실행 한도가 시장마다 따로 있고, 환율 환산·두 시장 합산·통합 노출 한도는 없다. 금액은 그 시장 통화(KRW / USD)로 오고 섞이지 않는다.

- **읽기는 전부 `market` 쿼리**를 받는다. 기본값 `KRX`, `KRX`·`US` 외 값(`JP`, 소문자 `us`, 빈 문자열)은 **400** `"market은 KRX, US 중 하나여야 합니다: 'JP'"`. 기본값은 쿼리가 **없을 때만** 쓴다(오타가 국내 장부를 읽지 않게).
- 쓰기: 긴급 정지·재가동은 `scope`(`all` 기본 | `KRX` | `US`), 모드 변경은 `market`(필수 — 다른 시장 모드를 실수로 바꾸지 않게 기본값 없음), 승인은 `market` 쿼리.
- 색 관례는 시장과 무관하게 하나(국내 관례: 빨강=상승·매수, 파랑=하락·매도, docs/08 §4 Q1).
- 미국 종목 표시는 "한글명 티커"(`애플 AAPL`), 국내는 한글명만(Q4). 서버는 `name`(한글명)을 주고, 화면이 시장 라벨의 `name_style`로 조합한다.

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `TRADING_DATA_DIR` | `<repo>/data` | `audit/`, `baseline_summary.json`, `results/paper*/`, `cache/`가 있는 폴더 |
| `TRADING_MEMORY_LOG` | summary의 `log_path` | 읽을 메모리 로그(국내만 — 미국 결정 베이스라인은 아직 없음) |
| `TRADING_CORS` | localhost 5173·3000 | 허용 오리진, 쉼표 구분 |
| `TRADING_ADMIN_TOKEN` | 없음 | 설정하면 재가동·모드 변경에 `X-Admin-Token` 헤더 필수. 없으면 개발 모드로 누구나 가능하고 기록의 `role`이 `admin(dev, no token)`으로 남는다 |

파일 mtime이 바뀌면 다음 요청에서 다시 읽는다(append-only라 안전).

## 기록 파일

시장마다 폴더 하나: `audit/KRX/`, `audit/US/`. 세 기록 모델(`DecisionRecord`·`OrderRecord`·`OperationRecord`)에 `market` 필드(기본 `"KRX"`)가 있고, `OperationRecord`에는 `scope`(정지·재가동의 요청 범위 `all`/`KRX`/`US`, 그 외 `null`)가 있다.

**호환 읽기(이전 기록):** 시장 축 이전의 `audit/*.jsonl`(루트)은 **국내 기록으로 읽고 옮기지 않는다**. 국내 장부는 루트 파일을 먼저, 그 뒤 `audit/KRX/`를 이어 읽고(`audit.market_audit`), 실행기·API가 쓰는 새 국내 기록은 `audit/KRX/`에만 append된다. 예외: `scripts/baseline_krx.py`(선별·결정)는 아직 루트 `audit/`에 쓰고, `scripts/build_mockup.py`는 루트만 읽는다(core 소유 스크립트, 후속). 읽기는 둘 다 이어 읽으므로 국내 장부는 섞이지 않는다. 루트 파일은 한 바이트도 바뀌지 않는다(옮기거나 다시 쓰면 append-only 감사 기록을 고치는 셈이라 택하지 않음). 미국 장부는 `audit/US/`만 읽는다.

**시장 검증:** `AuditLog(directory, market=…)`는 다른 시장의 기록을 쓰기·읽기 모두에서 거부한다(`ValueError` "…: US 기록이 KRX 장부에 들어올 수 없습니다"). 실행기는 이를 기록 파손으로 보고 종료 코드 4.

| 파일 (`audit/<시장>/`) | 모델 | 쓰는 쪽 |
|---|---|---|
| `screens.jsonl` | `ScreenRun` | 선별 |
| `decisions.jsonl` | `DecisionRecord` (+ `source`: `agent` 기본 / `rule`) | 에이전트·베이스라인(`agent`) · 모의 실행기의 규칙 청산(`rule`, 주문보다 먼저 기록) |
| `orders.jsonl` | `OrderRecord` (order_id·ticker·trade_date·side·quantity·filled_*·fill_date·status·exception_kind·reason·rules_applied) | 모의 실행 `scripts/paper_execute.py`(`execution.py`) · 긴급 정지(미체결 취소). 같은 order_id의 마지막 줄이 현재 상태. 규칙 청산은 `paper-<date>-<ticker>-exit`, `rules_applied=[stop_loss/trailing/expired/force]` |
| `operations.jsonl` | `OperationRecord` (kind halt/resume/mode_change/rule_change·at·operator·role·reason·target·before·after·market·scope) | 이 API의 쓰기 3종 · 실행기 자신의 정지·재가동(`operator=system`, `role=auto`). 정지·재가동의 `target`: `manual`(사람 — 이전 기록의 `running`도 manual로 읽음) · `daily_loss`(일일 손실) · `dd_stop`(−20% 드로다운). `effective_from`(system 일일 손실 자동 해제의 효력 거래일 — `at`은 실제로 쓴 시각 그대로). 실거래 전환 `mode_change`의 `before.paper_gate`(판정 스냅샷) |

## 읽기 엔드포인트

| 메서드·경로 | 화면 | 반환 |
|---|---|---|
| `GET /api/health?market` | 상태 바 | ok, 코어 버전, `market`·`market_ko`(고정 "KRX" 없음)·`markets[]`, 그 시장의 기록 개수(screens·decisions·orders·memory_entries), `decisions_by_source{agent, rule}`(규칙 청산은 에이전트 판단 수가 아니다), 서버 시각 |
| `GET /api/labels` | 공통 (시장 무관) | `rating_ko`, `rule_ko`, `rule_desc_ko`, `sell_reason_ko`, `mode_ko`, `order_status_ko`, `exception_kind_ko`, `exit_reason_ko`, `market_ko`, `halt_scope_ko`, `halt_target_ko`(정지 종류 manual · daily_loss · dd_stop → 한국어), `book_event_ko`(가드·잠금 이력 종류 → 가드 · 잠금 · Protections · 개입 · 피크 재설정), `markets{KRX, US: {market, label_ko, currency, currency_symbol, benchmark, benchmark_name_ko, timezone, session(현지), session_kst(오늘 기준, 서머타임 반영), name_style(name / name_ticker)}}`, `color_convention{up: red, down: blue, note}`(두 시장 공통) |
| `GET /api/markets` | 1 시장 요약 카드 | 시장마다 한 줄(합산 없음): 위 시장 라벨 + `as_of, equity, initial_cash, day_return, drawdown, positions`(보유 종목 수)`, state`(아래 운용 상태)`, flow`(홈 흐름 카드 4장: `screen{run_id, as_of, universe, universe_size, selected_count}`\|null 최신 선별 · `decisions{date, total, by_rating, by_rating_ko, rule}` 최신 결정일의 에이전트 결정 등급별 수(5단계 → REVIEW 순, 있는 것만 · `total`도 에이전트만)와 같은 날 규칙 청산 수 `rule` · `exceptions{date, total, by_kind, by_kind_ko}` `/api/exceptions` 기본값과 같은 날의 예외 큐 종류별 수 · `holdings{count, near_stop, protections_active}` — 서버가 센다, 화면은 합산하지 않음)`, next_run_kst`(ISO, 다음 평일 장 마감 5분 뒤)`, next_run_label_ko`("11-14 06:05 KST")`, error`. 한 시장 장부가 파손되면 그 카드만 `error` 문구와 숫자·state·flow `null`이고 다른 카드는 정상이다. `market` 쿼리는 받지 않는다(두 시장 전용). 다음 실행은 평일만 본다(휴장일 미반영 — 가격 캐시에 앞날 달력이 없음) |
| `GET /api/trading/mode?market` | 상태 바·설정 | 그 시장의 `market`·`market_ko`·`mode`(auto/manual/paper)·`mode_ko`·`running`·`halted_at`·`halted_by`·`halt_reason`·`keep_exits`·`halted_by_ko`(시스템 정지면 "시스템 자동 정지", 아니면 운영자 이름)·`halt_target`(`manual`/`daily_loss`/`dd_stop`, 실행 중이면 null)·`halt_target_ko`("운영자 긴급 정지" / "시스템 · 일일 손실 정지" / "시스템 · 드로다운 정지")·`dd_stop`(−20% 정지가 서 있음 — 사람이 그 위에 정지를 더 걸었어도 true, 재가동에서 피크 재설정을 고를 수 있음)·`label_ko`("국내 자동 매매 실행 중" / "미국 정지됨 · 시각 UTC · 정지 주체" — 시스템 정지면 주체 자리에 `halt_target_ko` / "국내 모의투자 모드")·`admin_token_required`·**두 축**(파생, 저장 형식 불변): `account_mode`(paper/live — mode가 paper면 paper, 아니면 live)·`account_mode_ko`(모의투자/실거래)·`execution_mode`(auto/manual — mode가 manual이면 manual, 아니면 auto)·`execution_mode_ko`(자동 실행/수동 승인)·`auto_resume_at`(system 일일 손실 정지가 풀리는 시각: 정지일 다음 **거래일** 00:00, 그 시장 시간대 ISO — 주말·휴장일은 건너뜀, 아니면 null)·`auto_resumed`(그 시각이 지나 실행 기록 전이라도 실행 중으로 읽힘)·`auto_resume_open_kst`(그날 정규장 개장 시각, KST ISO — 해제는 현지 00:00이지만 주문은 개장부터)·`auto_resume_label_ko`("07-28(화) 09:00 KST", 상단 배지 · 정지 띠 · 재가동 모달 문구, 일일 손실 정지가 아니면 null)·`halt_scope`(서 있는 마지막 정지를 보낸 범위 all/KRX/US, 실행 중이면 null)·`halt_scope_ko`(전체 시장 / 국내만 / 미국만). 한계: 다음 거래일은 지수 가격 캐시에서 찾는데 정지가 서 있는 동안 캐시는 보통 정지일에서 끝나므로 **다음 평일**이 된다(평일 휴장 미반영 — 휴장일 00:00부터 실행 중으로 읽힘. 주문 영향은 없다: 실행기는 휴장일에 돌지 않고, 해제 줄의 `effective_from`은 실행 때의 달력으로 정해짐) — 유효 상태는 `execution.effective_state`, 서 있는 정지가 전부 system `daily_loss`일 때만 |
| `GET /api/screens?from&to&universe&market` | 2-A | 선별 실행 요약 목록: `run_id, as_of, universe, universe_size, selected_count, rules, rules_ko, rule_count, stage_counts, recorded_at`. `from`·`to`(as_of 포함 범위)·`universe`(정확히 같은 이름)로 거른다 |
| `GET /api/screens/{run_id}?market` | 2-B | `ScreenRun` 전체 + 요약 필드. 후보마다 `name, failed_rules(_ko), filters[{rule, value, threshold, passed, note}], score, rank, selected` |
| `GET /api/decisions/dates?market` | 3 | 결정일 목록(내림차순) |
| `GET /api/decisions?date&rating&review_only&overrides_only&ticker&source&market` (`source=agent`면 에이전트 판단만 — 등급 필터 "매도"에 규칙 청산을 빼려면 함께 쓴다, 그 외 값은 422) | 1·3 | `DecisionRecord` + `source`(agent/rule)·`source_ko`("에이전트 결정"/"규칙 청산")·`name, rating_ko, tradeable, outcome{…}`(rule이면 null), `orders[]`(같은 ticker·trade_date의 주문 중 자기 것: rule 결정은 `-exit` 주문만, agent 결정은 그 외) |
| `GET /api/orders?date&status&ticker&market` | 1 오늘 자동 실행 · 3 | 주문마다 현재 상태 한 줄: `OrderRecord`(+ `market`) + `name, side_ko, status_ko, amount`(그 시장 통화) + **체결 시각** `fill_at_local`·`fill_at_kst`(그 세션 개장 시각, 현지 · KST ISO — zoneinfo가 서머타임 반영, 뉴욕 09:30 = 여름 22:30 · 겨울 23:30 KST)·`fill_planned`(filled·partial은 `fill_date`의 개장 · false, open은 결정일 다음 거래일 개장 · true — 지수 캐시 밖이면 다음 평일, 휴장 미반영. blocked·rejected·cancelled·exception은 셋 다 null/false). 화면은 Intl로 서식만(국내는 KST만). status는 filled/partial/open/blocked/rejected/exception/cancelled. date 형식·status 값이 틀리면 **400** |
| `GET /api/exceptions?date&all_dates&market` | 1 예외 띠 · 3 예외 큐 · 사이드바 배지 | `[{kind(review/cap_exceeded/order_rejected/vendor_unavailable/data_delayed — luld_halt·pdt_blocked는 값·라벨만), kind_ko, ticker, name, trade_date, summary, detail, ref}]`. date 없으면 최신 결정일만. 주문의 kind는 `exception_kind`(없으면 규칙 id·사유에서 추론). REVIEW 결정의 예외 주문은 결정 항목과 같아 한 번만 나온다 |
| `GET /api/tickers?market` | 4 선택 | `ticker, name, latest_selected, latest_rank` |
| `GET /api/tickers/{ticker}?market` | 4 | `name, screening[], decisions[], outcomes[], orders[], timeline[]` |
| `GET /api/performance?market` | 6 · 1 최근 정산 | 시장별 규칙 베이스라인 요약(`audit.baseline_summary_path`: 국내 `data/baseline_summary.json`, 미국 `data/audit/US/baseline_summary.json` — `scripts/baseline_krx.py --market US`)과 요약의 `log_path` 메모리 로그. 그 시장 요약이 없으면 **404**(`TRADING_MEMORY_LOG`는 국내 전용 덮어쓰기). `by_rating{…}, per_date[], reflections[{date, ticker, name, rating(_ko), raw_return, alpha, resolved, reflection}], holding, caveat` |
| `GET /api/paper?market` | 6 포트폴리오 실측 탭 · 국내 2차 ↔ 미국 3차 비교 | `scripts/paper_backtest.py [--market US]`의 `summary.json`(국내 `results/paper/`, 미국 `results/paper_us/`) + `market, currency`: `benchmark{metrics, windows}`, `variants{rules_off, rules_on, rules_on_regime: {label_ko, metrics(total_return·cagr·max_drawdown·dd_peak/trough/recovery·worst_day·volatility…), windows, exits_by_reason(_ko), trades, win_rate, mean_exposure, event_counts, events_tail[40], trades_sample}}`, `criteria[]`(PAPER_TRADING), `caveats[]`. 없으면 404("…scripts/paper_backtest.py --market US를 먼저 실행") |
| `GET /api/paper/progress?market` | 8 모의투자 검증 · 사이드바 배지 · 실거래 전환 모달 | 그 시장의 모의투자 통과 판정(서버가 6개 기준을 전부 판정 — 화면은 다시 계산하지 않는다). 아래 "모의투자 통과 판정" 절. 기록이 없으면 404가 아니라 `started: false`(전 기준 `in_progress`, `eligible: false`). market이 틀리면 400 |
| `GET /api/settings/rules?market` | 7 | `screen_rules[]`(layer screen), `screen_params`, `risk_rules[]`(RiskConfig 19개, layer hard_rule, 두 시장 공통), `paper_criteria[]`, `auto_rules[]`(`config.auto_execution(market)`: 공통 auto_daily_loss_limit·kill_switch_reset·default_mode + 그 시장 금액 auto_daily_order_cap·auto_order_max — 금액 행은 `currency`, 그 외 `null`), `auto_by_market{KRX, US}`, `market`(그 시장 상수)·`market_code`·`markets`(두 시장 `config.MARKETS`), `mode`(그 시장 상태), `change_history[]`(**두 시장** 운영 기록 합침, 최신순 — 같은 초면 기록 순서 — + `kind_ko`·`market_ko`), `history_errors[]`(다른 시장 운영 기록을 못 읽으면 그 시장 행을 빼고 사유를 적음 — 고른 시장 자신의 파손은 500) |
| `GET /api/positions?as_of&market` | 1 KPI·리스크·Protections · 5 | 그 시장의 모의투자 장부(체결 기록을 캐시 종가로 재생, 저장 상태 없음): `market, currency, as_of, initial_cash`(KRX 1억 · US $100,000)`, day_return`(전일 평가 대비)`, cash, holdings_value, equity, peak, drawdown, gross_exposure, realized_pnl, realized_today, unrealized_pnl, regime_on, regime_gap`(지수 종가 ÷ `limits.regime_ma_days`일 단순평균 − 1, 창보다 데이터가 짧으면 null)`, peak_reset{at, to}|null`(마지막 −20% 정지 뒤 관리자 피크 재설정, 없으면 null — `peak`·`drawdown`이 이 값부터 계산됨)`, positions[{ticker, name, quantity, avg_price, entry_date, last_price, last_date, market_value, unrealized(_pct), weight, stop_price`(체결 평단 × (1 + stop_loss_pct))`, stop_gap`(종가 ÷ 손절선 − 1)`, near_stop`(stop_gap ≤ `limits.near_stop_pct` 2%)`, trailing_active, trailing_high, trailing_stop_price, holding_days, expiry_date, exit_signal{reason, note}|null}], protections[{id, label_ko, active, detail, until, tickers[], lookback_days, lookback_hits}]`(카드의 "lookback 내 트리거 n건": as_of까지 그 가드 창의 거래일 안 — StoplossGuard 손절 청산 수(10일), MaxDrawdown −20% 정지 기록 수(dd_stop_lock_days 20일), Cooldown 청산 수(5일), LowProfit 발동 수(30일))`, sector_exposure`(섹터 데이터 미연결이라 항상 null — 화면은 "미연결" 한 줄)`, protections_active`(발동 수, 상단바 칩)`, near_stop_count, max_position_weight, max_position_ticker`(가장 큰 보유, 없으면 0 · null)`, today_session, today_buy_amount, today_sell_amount, today_buy_cap_used`(아래)`, exits_by_reason(_ko)`(stop_loss·trailing·expired·force·exit_signal(등급 하향)별 건수)`, closed[{…, reason}], limits{…, auto_daily_order_cap, near_stop_pct, regime_ma_days}`(금액은 그 시장 통화)`, note, mode`. **오늘 주문 금액**: `as_of`에 결정된 주문이 체결될 세션(`today_session`, 데이터에 아직 없으면 null)에 체결되는 모든 주문(결정일 무관 — 휴장일 결정은 앞 거래일과 세션을 공유)의 매수·매도 금액. 체결분은 체결가 × 수량, 미체결은 결정일 종가 × 수량. 일일 상한(`auto_daily_order_cap`)은 **매수만** 세므로 `today_buy_cap_used` = 매수 ÷ 상한(실행기의 상한 판정과 같은 함수 `execution.session_amounts`). `exit_signal`은 실행기가 그날 종가에 규칙 청산 주문으로 바꾸는 판정과 같은 함수(`execution.exit_signals`). as_of 기본값은 데이터의 마지막 거래일, 형식이 틀리면 **400**. 이전에는 **501**이었다(본문 `{implemented:false}`) |
| `GET /api/events?market&limit` | 5 가드·잠금 이력 | `{market, total, counts{종류: 수}, events[{date, kind, kind_ko, ticker, name, text}]}` 최신순(`limit` 기본 50, 1~500 밖이면 **422**). 주문·운영 기록에서 모은다(`execution.book_events`, 저장 이력 없음): `guard` = system 정지(일일 손실 · −20%)와 드로다운이 처음 반감(−10%)에 들어간 날, `lock` = 잠금이 막은 신규 매수(전 종목 잠금 dd_stop · daily_loss_limit · regime · StoplossGuard는 결정일·잠금 묶음마다 한 줄, 종목 잠금 Cooldown · LowProfit은 주문마다), `protections` = 청산이 새로 건 잠금(청산마다 Cooldown, 발동 때 StoplossGuard · LowProfit), `intervention` = 상한·반감 스케일이 비중을 자른 매수(`max_position_pct`·`sector_cap`·`max_gross_pct`·`dd_halve`), `peak_reset` = 피크를 재설정한 관리자 재가동. 사람 정지는 넣지 않는다 |
| `GET /api/trading/resume-preview?market&reset_peak` | 재가동 모달 | 읽기 전용. `{market, variant(running/manual/daily_loss/dd_stop), variant_ko, can_resume, reset_peak, equity, as_of, peak_before, base, restops, lines[{id(peak/dd_halve/dd_stop), label_ko, level, amount, vs_equity}]}`. 변형: 실행 중 · 일일 손실(서 있는 정지가 전부 system 일일 손실 — `can_resume` false, 다음 개장 자동 해제를 기다림) · −20% 정지가 서 있음(사람이 그 위에 정지했어도) · 그 밖은 사람 정지. −20%일 때만 `lines`: `base` = `reset_peak`(기본 true)면 지금 평가액, false면 옛 피크, 금액 = `base × (1 + level)`, `vs_equity` = 평가액 ÷ 금액 − 1, `restops` = 옛 피크 기준으로 이미 −20% 이하(재가동 직후 다시 정지). 재설정 금액은 재가동이 쓰는 것과 같은 함수(`_peak_reset`). `reset_peak`가 bool이 아니면 **422**, 시장이 틀리면 400 |

## 쓰기 엔드포인트

| 메서드·경로 | 권한 | body | 동작 |
|---|---|---|---|
| `POST /api/trading/halt` | 운영자 (토큰 불필요 — 사람이 항상 멈출 수 있어야 함) | `{operator, reason, keep_exits=true, scope="all"}` (operator·reason 비면 422, scope가 all·KRX·US 외면 **400**) | **범위**: `all`(기본)이면 실행 중인 모든 시장에, 시장 이름이면 그 시장에만 정지 한 줄씩 — 같은 `at`·같은 사유, 기록의 `scope`에 요청 범위. 이미 정지된 시장은 `skipped`(`keep_exits` 포함 — true면 그 시장 청산은 계속 돈다)로 건너뛴다. 단, 청산이 도는 정지(`keep_exits=true`)에 `keep_exits=false`가 오면 건너뛰지 않고 정지 한 줄을 더 써서 청산까지 멈춘다(상태는 마지막 정지 줄을 따름). 건너뛸 시장만 남으면 **409**. **한 시장 기록이 파손돼도 다른 시장 정지는 막히지 않는다**: `operations.jsonl`을 못 읽는 시장에도 정지 줄을 append하고(그 시장 실행기는 어차피 그 파일을 못 읽어 exit 4로 멈춘다) 결과에 `error`를 싣는다. 시장마다: 신규 주문 중단, 정지 기록(취소할 order_id 목록 포함)을 쓴 뒤 미체결(open) 주문마다 `cancelled` 줄을 orders.jsonl에 append, 보유 포지션 유지. `keep_exits=true`면 미체결 **매도**는 남기고, 정지 중에도 실행기가 청산만 계속 돈다(미체결 매도 체결 + 그날 규칙 청산; 신규 매수·에이전트 결정 주문은 없음). `keep_exits=false`면 실행기가 거부(종료 코드 3). orders.jsonl이 파손돼 읽히지 않아도 정지는 기록되고 `open_orders_cancelled: null` + 사유 note. 반환 `{scope, scope_ko, results[{market, state, error, effects{new_orders, open_orders_cancelled, positions, exits_active, note}}], skipped[{market, reason, keep_exits}]}` |
| `POST /api/trading/resume` | 관리자 | `{operator, reason, scope="all", reset_peak=true}` (`reset_peak`가 bool이 아니면 422, scope가 틀리면 400) | 재가동(기록 `target=manual`). **−20% 정지가 서 있는 시장**에서 `reset_peak=true`(기본)면 그 시장의 마지막 종가 평가액을 새 드로다운 피크로 기록한다(`after{peak_reset_to, peak_reset_at(종가일), peak_before}`). `false`면 피크를 그대로 두며, 드로다운이 여전히 −20% 이하면 다음 실행에서 다시 정지한다. 다른 정지(사람·일일 손실)에서는 `reset_peak`가 아무것도 하지 않는다. 피크 재설정 값은 `resume-preview`와 같은 함수다. 일일 손실 정지는 서버가 관리자 조기 재가동을 여전히 받는다(halt-semantics 결정), 화면의 재가동 모달은 그 변형에서 버튼을 내지 않는다(`can_resume` false) — `all` 범위로 다른 시장을 재가동하면 그 시장의 일일 손실 정지도 함께 풀리고, 모달이 그 사실을 미리 적는다. `all`이면 정지된 모든 시장, 시장 이름이면 그 시장만(다른 시장은 그대로 정지). 실행 중인 시장은 `skipped`. 시장마다 정지 전에 나간 미체결이 남아 있으면(정지 때 orders.jsonl을 못 읽은 경우) 취소 기록(`keep_exits`면 매도는 유지). 범위 안 시장이 전부 실행 중이면 409. 토큰 설정 시 헤더 없거나 틀리면 **403**. 반환 `{scope, scope_ko, resumed[], skipped[{market, reason}], peak_resets{시장: {to, at, before}}, states{시장: 상태}}` |
| `POST /api/trading/mode` | 관리자 | `{operator, reason, market, mode \| account_mode, confirmed=false}` — `mode`(auto/manual/paper, 저장 형식 그대로)와 `account_mode`(paper/live, 계좌 축) 중 **정확히 하나**(둘 다 · 둘 다 없음 422). market 없으면 422, 틀리면 400 | **그 시장의** 모드 전환. 토큰을 먼저 본다(403). 같은 모드면 409. `account_mode: live`는 paper에서 auto로(실행 축은 저장 형식상 auto), `account_mode: paper`는 paper로. **모의 → 실거래 게이트**(paper에서 paper가 아닌 모드로 — `mode: auto`·`manual`로 요청해도 같다): `confirmed: true`가 없으면 **422**, 서버가 그 자리에서 `/api/paper/progress`를 다시 판정해 `eligible`이 아니면 **409**("국내 모의투자 통과 기준 미충족 — 실거래로 전환할 수 없습니다: 기준(상태 · 값); …", 기록은 쓰지 않음). 통과하면 운영 기록 `before{mode, account_mode: paper, paper_gate{as_of, start, weeks, eligible, summary_ko, criteria{id: {value, status}}}}`, `after{mode, account_mode: live}`. 다른 시장은 그대로. 실거래 → 모의(또는 실행 축만 바꾸기)는 게이트 없음. paper에서 나갈 때 미체결 모의 주문을 취소 기록(`after.open_orders_cancelled`). 반환 `{state, note}` — 저장 형식은 그대로라 실행기는 paper가 아닌 모드에서 주문하지 않는다(`ExecutionRefused`, 실주문 경로 없음): `note`가 그 사실을 적는다(paper로 돌릴 때는 null) |
| `POST /api/decisions/{ticker}/{date}/approve?market` | 운영자 | `{approver, note}` | **수동 승인 모드 전용.** 자동·모의 모드에서는 **409**("승인 단계가 없습니다"). 수동 모드에서는 실행 레이어 전까지 **501** |

정지·재가동·모드 변경과 모의 실행은 **시장마다** 같은 잠금(`audit/<시장>/execution.lock`)을 잡는다. 범위 정지·재가동은 범위 안 모든 시장의 잠금을 `config.MARKETS` 순서로 잡아 교착이 없고, 실행기는 자기 시장 잠금만 잡는다. 실행기가 상태(정지·모드)를 확인한 뒤 주문을 쓰는 사이에 정지·모드 변경이 끼어들 수 없다.

기본 운용 모드는 **모의투자(paper)** 다. 기획서 §8-1대로 같은 자동 루프를 모의로 먼저 돌리고, 관리자가 모드 변경으로 자동(auto)에 올린다. 코드상 기본값은 `config.AUTO_EXECUTION["default_mode"]`. 모드는 시장마다 따로다.

자동 실행 금액 한도는 시장 통화로 시장마다(`config.AUTO_EXECUTION["by_market"]`, 읽기는 `config.auto_execution(market)`): 국내 일일 1억원 · 주문당 3천만원, 미국 일일 $70,000 · 주문당 $20,000(Q3). 비교는 같은 통화 안에서만 한다.

404: 없는 run_id·ticker·결정. 숫자 형식: 수익률·알파는 소수(0.012 = 1.2%), 가격·금액은 그 시장 통화 단위 float(국내 원, 미국 달러). 주문 사유 문구의 금액도 그 통화("1,234원" / "$1,234.56").

## 모의투자 통과 판정 (화면 8, `GET /api/paper/progress`)

`config.PAPER_TRADING`의 기준 6개를 그 시장의 모의 장부(`audit/<시장>/`)로 판정한다(`execution.paper_progress`). 시장마다 따로, 합산 없음.

- **시작일**: 첫 주문의 `trade_date`. 주문이 없으면 그 시장의 마지막 paper 전환 기록(`mode_change` → paper)을 그 시장 현지 날짜로. 둘 다 없으면 `started: false`.
- **기간**: 시작일 → `as_of`(그 시장 달력의 마지막 날). `days` = 달력 일수, `weeks` = `days // 7`. `min_weeks`(8) 충족은 `days ≥ 56` — 8주 정확히가 경계(통과). 미달이면 `in_progress`와 "n일 남음 · YYYY-MM-DD 장 마감 후 판정"(`judge_date`).
- **최대 드로다운**: 초기 자본을 포함한 종가 평가액의 누적 최고치 대비(관리자 피크 재설정은 무시 — 판정은 모의 전 기간의 실제 낙폭). `≤ −10% + EPS`면 `unmet`(`risk.EPS` 1e-9 — 하드 룰과 같은 경계 관례: −10.0%는 미충족, 그래서 `threshold_ko`는 "> −10%").
- **일일 손실 위반**: 전일 대비 장부 손익 `≤ auto_daily_loss_limit(−2%) + EPS`인 날 수. `> max_daily_loss_breaches(2)`면 `unmet`.
- **−20% 정지**: 기간 안의 system `dd_stop` 정지 기록(`after.date`) 수. `> 0`이면 `unmet`.
- **REVIEW 비율**: REVIEW 결정 ÷ **에이전트 결정**(`source=agent`, 규칙 청산 제외). `> 5% + EPS`면 `unmet`, 에이전트 결정이 0건이면 `in_progress`.
- **결정 없는 주문**: 기간 안 주문 중 결정 기록과 연결되지 않은 것(`execution.unexplained_orders`). `> 0`이면 `unmet`.
- `eligible` = 6개 전부 `met`. `in_progress`가 하나라도 있으면 전환 불가.

응답: `market, market_ko, currency, timezone, benchmark_name_ko, started, start, as_of, days, weeks, min_weeks, judge_date, initial_cash, eligible, counts{met, unmet, in_progress}, summary_ko`("충족 5 · 미충족 1 (REVIEW 결정 비율 상한) · 진행 중 0")`, verdict_ko`(전환 가능 / 전환 불가)`, weeks_badge_ko`(사이드바 "n/8주", 8에서 멈춤)`, criteria[{id, label_ko, threshold, threshold_ko, value, value_ko, status(met/unmet/in_progress), status_ko, note_ko, numerator?, denominator?}]`, `violations[{date, kind(review/daily_loss/dd_stop/unexplained_order), kind_ko, ticker, name, value, text, ref}]`(최신순, `ref`는 화면 링크 대상: `{type: decision, date, ticker}` → 결정 예외 탭 · `{type: positions, date}` · `{type: order, order_id}`), `notes[{date, kind(order_exception/stop_loss), kind_ko, ticker, name, text, ref}]`(위반은 아니지만 사람이 본 것 — 두 시장 모두 계산, 화면은 있을 때만 표시), `series[{date, equity, drawdown}]`, `drawdown{current, min, min_date}`, `limits{max_drawdown, dd_halve, dd_stop}`(SVG 기준선), `go_live{capital, capital_source_ko, auto_order_max, auto_daily_order_cap, daily_loss_limit, daily_loss_amount, other_markets_ko[], note_ko}`(모달의 "자본에서 걸리는 규칙" — 지금 설정값, 실거래 자본 입력은 단계 5), `expected`(아래).

**기대 대비(`expected`) — 참고 전용**(2026-11-13 결정 D6): `reference_only: true`. 같은 기간의 실측 재현(국내 2차 · 미국 3차, `results/paper*/`의 `rules_on_regime` 변형 equity CSV와 이벤트)과 모의 결과를 나란히: `rows[{id(mean_exposure/new_buys/exits/period_return/max_drawdown/daily_loss_guard/index_return), label_ko, unit(pct/count), reference, paper, diff}]` + 행별 세부(`paper_open`, `*_by_reason`, `*_dates`). `index_return`의 `diff`는 null(같은 값). `eligible`과 `counts` 계산에 **쓰이지 않는다**(테스트로 고정). 실측 결과가 없거나 기간이 겹치지 않으면 `rows: []` + `error`("2차 실측 결과를 읽지 못함(…)" / "재현 기록이 …까지라 모의 기간과 겹치지 않음"), 시작 전이면 `expected: null`.

`GET /api/paper/progress?market=KRX` (워크트리 장부 실측, 2026-11-13 — 줄임)

```json
{"market": "KRX", "started": true, "start": "2026-06-05", "as_of": "2026-11-13", "days": 161, "weeks": 23, "min_weeks": 8,
 "judge_date": "2026-07-31", "eligible": true, "counts": {"met": 6, "unmet": 0, "in_progress": 0},
 "summary_ko": "충족 6 · 미충족 0 · 진행 중 0", "verdict_ko": "전환 가능", "weeks_badge_ko": "8/8주",
 "criteria": [
   {"id": "min_weeks", "threshold_ko": "≥ 8주", "value_ko": "23주 (2026-06-05 시작)", "status": "met", "note_ko": "기간 충족", "...": "..."},
   {"id": "max_drawdown", "threshold_ko": "> −10%", "value": -0.0371, "value_ko": "-3.7% (2026-06-23)", "status": "met", "note_ko": "현재 -1.9%"},
   {"id": "max_daily_loss_breaches", "threshold_ko": "≤ 2회", "value": 1, "value_ko": "1회 (2026-06-23 -2.67%)", "status": "met", "note_ko": "1회 더 남음"},
   {"id": "max_stop_outs", "threshold_ko": "0회", "value": 0, "status": "met"},
   {"id": "max_review_rate", "threshold_ko": "≤ 5%", "value": 0.0, "value_ko": "0.0% (0 / 48)", "status": "met", "numerator": 0, "denominator": 48},
   {"id": "max_unexplained_orders", "threshold_ko": "0건", "value": 0, "status": "met", "note_ko": "주문 21건 중 21건이 결정 기록과 연결"}],
 "violations": [{"date": "2026-06-23", "kind": "daily_loss", "kind_ko": "일일 손실 한도", "ticker": null, "value": -0.0267,
                 "text": "-2.67% ≤ -2% → 다음 세션 신규 매수 중단", "ref": {"type": "positions", "date": "2026-06-23"}}],
 "notes": [{"date": "2026-06-25", "kind": "stop_loss", "ticker": "055550.KS", "text": "2026-06-08 진입 → 2026-06-25 시가 청산 -6.9%", "...": "..."}],
 "drawdown": {"current": -0.0187, "min": -0.0371, "min_date": "2026-06-23"}, "limits": {"max_drawdown": -0.1, "dd_halve": -0.1, "dd_stop": -0.2},
 "go_live": {"capital": 100000000.0, "capital_source_ko": "모의 자본(실거래 자본 입력은 단계 5)", "auto_order_max": 30000000,
             "auto_daily_order_cap": 100000000, "daily_loss_limit": -0.02, "daily_loss_amount": -2000000.0, "other_markets_ko": ["미국"], "note_ko": "실주문 경로는 아직 없습니다: …"},
 "expected": {"reference_only": true, "measurement_ko": "2차 실측", "variant_ko": "하드 룰 + 국면 필터", "start": "2026-06-05", "end": "2026-11-13",
              "covered_until": "2026-11-12", "error": null,
              "rows": [{"id": "mean_exposure", "unit": "pct", "reference": 0.188, "paper": 0.050, "diff": -0.138}, {"id": "new_buys", "unit": "count", "reference": 30, "paper": 5, "diff": -25},
                       {"id": "index_return", "unit": "pct", "reference": -0.382, "paper": -0.386, "diff": null}, "..."]},
 "series": ["… 110거래일"]}
```

`GET /api/paper/progress?market=US` — 같은 형태(달러, `timezone` America/New_York, 화면 날짜는 ET). 실측: 22주 · DD −3.4%(2026-08-04) · 일일 손실 위반 0회("2회 더 남음 · 가장 큰 하루 손실 -1.20% (2026-06-10)") · REVIEW 0/40 · 주문 53건 모두 연결 · `notes` 손절 4건(CAT · GOOGL · AVGO · XOM) · `expected` 3차 실측 하드 룰 + 국면 필터. 기록이 없는 시장(빈 `audit/`):

```json
{"market": "US", "started": false, "start": null, "as_of": "2026-11-12", "days": null, "weeks": null, "eligible": false,
 "counts": {"met": 0, "unmet": 0, "in_progress": 6}, "verdict_ko": "전환 불가", "weeks_badge_ko": "0/8주",
 "criteria": [{"id": "min_weeks", "value_ko": "–", "status": "in_progress", "note_ko": "모의 운용 기록 없음", "...": "..."}, "…"],
 "violations": [], "notes": [], "series": [], "drawdown": null, "expected": null, "...": "..."}
```

## 응답 예

`GET /api/trading/mode?market=US` (기록 없음 → 기본값)

```json
{"market": "US", "market_ko": "미국", "mode": "paper", "mode_ko": "모의투자", "running": true, "halted_at": null, "halted_by": null,
 "halt_reason": null, "keep_exits": true, "mode_changed_at": null, "label_ko": "미국 모의투자 모드", "admin_token_required": false}
```

`POST /api/trading/halt` body `{"operator": "운영자A", "reason": "벤더 지연", "keep_exits": false}` (scope 기본 all)

```json
{"scope": "all", "scope_ko": "전체 시장",
 "results": [
   {"market": "KRX", "state": {"market": "KRX", "running": false, "halted_at": "2026-11-13T03:10:22Z", "label_ko": "국내 정지됨 · 2026-11-13 03:10 UTC · 운영자A", "...": "..."},
    "error": null,
    "effects": {"new_orders": "stopped", "open_orders_cancelled": 0, "positions": "kept", "exits_active": false,
                "note": "국내 모의투자: 미체결 주문 0건을 취소 기록했습니다(orders.jsonl). 실제 증권사 주문은 없습니다"}},
   {"market": "US", "state": {"market": "US", "running": false, "halted_at": "2026-11-13T03:10:22Z", "...": "..."}, "error": null, "effects": {"...": "..."}}],
 "skipped": []}
```

이미 정지된 시장이 있을 때의 `skipped` 항목: `{"market": "KRX", "reason": "이미 정지된 상태 (2026-11-13T03:10:22Z, system)", "keep_exits": true}`

`POST /api/trading/resume` body `{"operator": "관리자", "reason": "미국만 재개", "scope": "US"}` (국내는 정지 유지)

```json
{"scope": "US", "scope_ko": "미국만", "resumed": ["US"], "skipped": [], "peak_resets": {},
 "states": {"US": {"market": "US", "running": true, "label_ko": "미국 모의투자 모드", "...": "..."}}}
```

`POST /api/trading/resume` body `{"operator": "관리자", "reason": "드로다운 정지 점검 후 재개", "scope": "KRX"}` (−20% 정지 뒤, reset_peak 기본 true — 합성 장부 실측)

```json
{"scope": "KRX", "resumed": ["KRX"], "skipped": [],
 "peak_resets": {"KRX": {"to": 49852743.70, "at": "2026-06-10", "before": 100000000.0}}, "states": {"KRX": {"running": true, "...": "..."}}}
```

이후 `GET /api/positions` → `peak` 49,852,743.70 · `drawdown` 0.0 · `peak_reset {at: "2026-06-10", to: 49852743.70}`.

`GET /api/markets` (한 줄, 시장마다 따로 — 합계 없음)

```json
[{"market": "US", "label_ko": "미국", "currency": "USD", "currency_symbol": "$", "benchmark": "^GSPC", "benchmark_name_ko": "S&P 500",
  "timezone": "America/New_York", "session": "09:30-16:00", "session_kst": "23:30-06:00", "name_style": "name_ticker",
  "as_of": "2026-11-12", "equity": 100000.0, "initial_cash": 100000.0, "day_return": 0.0, "drawdown": 0.0, "positions": 0,
  "state": {"market": "US", "running": true, "...": "..."}, "error": null, "next_run_kst": "2026-11-14T06:05:00+09:00", "next_run_label_ko": "11-14 06:05 KST"}]
```

`GET /api/exceptions` (REVIEW 결정 한 건)

```json
[{"kind": "review", "kind_ko": "등급 읽기 실패 (REVIEW)", "ticker": "000660.KS", "name": "SK하이닉스", "trade_date": "2026-06-05",
  "summary": "등급을 읽을 수 없어 주문하지 않았습니다", "detail": "<에이전트 원문 전체>", "ref": "r1"}]
```

`GET /api/decisions?date=2026-07-24` (한 건, 실측)

```json
{"ticker": "005930.KS", "name": "삼성전자", "trade_date": "2026-07-24", "screen_run_id": "baseline_krx_v2_2026-07-24",
 "rating": "Buy", "rating_ko": "매수", "action": "Buy", "confidence": 0.8, "tradeable": true, "orders": [],
 "thesis": "**Rating**: Buy\n\n**근거**: 규칙 전용 모멘텀 베이스라인. 모멘텀 순위 1/9 (12-1개월 +329.4%, 6-1개월 +135.3%).",
 "evidence": [{"source": "market", "summary": "모멘텀 순위 1/9, 12-1개월 +329.4%, 6-1개월 +135.3%", "as_of": "2026-07-24", "ref": "baseline_krx_v2_2026-07-24"}],
 "hard_rule_overrides": [], "settings": {"decider": "rule_momentum_baseline"},
 "outcome": {"pending": false, "raw_return": -0.063, "alpha": -0.021, "holding": "5d", "resolved": "2026-08-03", "reflection": "규칙 전용 베이스라인(LLM 없음). …"}}
```

`GET /api/decisions?date=2026-06-22&ticker=005380.KS` (규칙 청산 결정, 실측 — 스크래치 데이터)

```json
{"ticker": "005380.KS", "name": "현대차", "trade_date": "2026-06-22", "source": "rule", "source_ko": "규칙 청산",
 "rating": "Sell", "rating_ko": "매도", "action": "Sell", "confidence": null, "tradeable": true, "outcome": null,
 "thesis": "손절: 진입가 대비 -9.0% ≤ -8%",
 "evidence": [{"source": "portfolio", "summary": "손절: 진입가 대비 -9.0% ≤ -8% · 보유 16주 · 평균 체결가 634,309원 · 진입 2026-06-15",
               "as_of": "2026-06-22", "ref": "stop_loss"}],
 "hard_rule_overrides": [], "settings": {"decider": "rule_exit", "exit_reason": "stop_loss"},
 "orders": [{"order_id": "paper-2026-06-22-005380.KS-exit", "side": "Sell", "quantity": 16, "filled_quantity": 16,
             "filled_price": 578055.0336, "status": "filled", "fill_date": "2026-06-23", "rules_applied": ["stop_loss"],
             "reason": "규칙 청산(손절): 손절: 진입가 대비 -9.0% ≤ -8% · 보유 16주 전량 · 다음 거래일 시가 → 모의 체결 2026-06-23 시가 578,344원 (슬리피지 0.05%)", "...": "..."}]}
```

`GET /api/trading/mode` (실행기의 일일 손실 자동 정지 뒤, 실측)

```json
{"mode": "paper", "running": false, "halted_at": "2026-11-13T05:50:49Z", "halted_by": "system", "halted_by_ko": "시스템 자동 정지",
 "halt_reason": "일일 손실 한도 초과: -2.67% (한도 -2%)", "keep_exits": true, "mode_ko": "모의투자",
 "label_ko": "정지됨 · 2026-11-13 05:50 UTC · 시스템 자동 정지", "admin_token_required": true, "mode_changed_at": null}
```

## 앞으로 추가될 것

- 예외 큐의 [재실행] [수동 등급 지정] [무시]는 쓰기 API가 없어 화면에서 비활성. 결정 재실행이 가능해지는 에이전트 연결 단계에 추가.
- 규칙 변경(`rule_change`)은 `OperationRecord`에 자리만 있고 쓰기 API는 없다. 설정 화면의 [수정]은 그때 열린다.
- 에이전트 결정이 들어오면 `DecisionRecord.thesis`에 `render_investment_memo()` 출력이 담기고, 구조화 메모가 필요하면 `memo` 키가 추가된다.
- 매도 검토(`SellReview`)는 `GET /api/reviews?date` 로 별도 서빙 예정. 사유 라벨은 `/api/labels`의 `sell_reason_ko`.
- 거래일 달력은 시장 지수 캐시 한 종목이다(국내 `^KS11`, 미국 `^GSPC` — 6-19 준틴스처럼 한 시장만 쉬는 날은 그 시장만 체결이 밀린다). 벤더 공백일은 휴장으로 취급돼 체결이 하루 밀린다(미해결 — `^KS11` 이상치 검증과 같이). 결정일이 달력 끝보다 뒤면 실행을 거부한다.
- 모의 실행은 API가 아니라 CLI(`scripts/paper_execute.py [--market KRX|US] --date D | --all`, 기본 KRX, 쓰기는 `audit/<시장>/`에만, 매도 비용은 시장 설정(국내 거래세 · 미국 SEC fee·TAF), 마지막 줄 "US · 주문 …", 종료 코드 0 완료 · 2 인자 오류 · 3 거부 · 4 기록 파손)로 돈다. `--all`은 결정일만이 아니라 가격 데이터 끝까지 **모든 거래일**을 돌고(규칙 청산은 매일 종가 판정), 끝에 미설명 주문 수를 출력한다. 쓰기 API는 기획서 §0-5의 세 가지로 유지.
- 일일 손실 자동 정지: 그날 장부 손익(전일 평가 대비)이 `AUTO_EXECUTION.auto_daily_loss_limit`(−2%) 이하면 실행기가 잠금 안에서 정지 기록(`operator=system, role=auto, target=daily_loss, after{keep_exits:true, date, day_return, equity, open_orders_cancelled}`)을 쓰고 미체결 매수를 취소한 뒤 그날은 청산만. 날짜당 한 번(관리자가 재가동한 뒤 같은 날을 다시 돌려도 다시 정지하지 않는다).
- **일일 손실 정지는 그날 세션까지**(2026-11-13 사용자 결정): 정지일보다 뒤인 **다음 거래일**의 첫 실행이 잠금 안에서 `resume`(`operator=system, role=auto, target=daily_loss, reason="일일 손실 정지 자동 해제(정지일 YYYY-MM-DD)", after{running:true, date, halted_on}`)을 먼저 쓰고 정상 진행한다. 한 번만(해제 뒤엔 실행 중이라 다시 쓰지 않음), 그 시장만, 휴장일 날짜 실행에서는 해제하지 않는다. 서 있는 정지가 **전부** system의 `daily_loss`일 때만 해제한다: 사람이 그 위에 정지를 걸었거나 −20% 정지가 함께 서 있으면 관리자 재가동을 기다린다. 관리자가 먼저 재가동해도 된다. 자동 정지 횟수(`PAPER_TRADING.max_daily_loss_breaches`)는 정지 기록 수로 그대로 센다. **해제 시점**(paper-gate, 2026-11-13): 해제 줄의 `at`은 실제로 쓴 시각이고 `effective_from`에 다음 거래일을 적는다. 실행기가 아직 돌지 않았어도 API의 유효 상태는 다음 거래일 00:00(그 시장 시간대)부터 실행 중이다(`auto_resume_at`·`auto_resumed`).
- **−20% 드로다운 정지**: 드로다운이 `RiskConfig.dd_stop`(−20%) 이하인 거래일 종가에 실행기가 전량 규칙 청산(`force`, 각 종목의 손절이 먼저면 그 사유)을 낸 뒤 `halt`(`operator=system, role=auto, target=dd_stop, after{keep_exits:true, date, equity, peak, open_orders_cancelled}`)를 쓰고 미체결 매수를 취소한다. 청산 매도는 정지 중에도 체결된다. 같은 날 일일 손실 정지가 먼저 쓰였어도 함께 쓰고, 이 정지가 서 있는 동안 다시 쓰지 않으며, 관리자 재가동(피크 재설정)이 그 날짜 이후에 이미 있으면 그 날짜를 다시 돌려도 쓰지 않는다. 실행기는 해제하지 않는다(관리자만).
- **드로다운 피크**: 마지막 피크 재설정(`peak_reset_at` ≤ 그날) 이후의 종가 평가액 최고치(재설정 값 포함), 재설정이 없으면 초기 자본부터. 신규 비중 절반(−10%) · 정지(−20%) · 사이징 스케일(`drawdown_scale`) · `/api/positions`의 `peak`·`drawdown`·MaxDrawdown 가드가 모두 이 피크 하나를 쓴다.
- **체결일 시가 없음**(실행기 `_fill`): 그 종목 캐시의 마지막 날이 지수 달력의 **체결일 직전 거래일**이면(한 세션만 늦음) `exception_kind=data_delayed`(시세가 아직 안 들어옴 — 사유 "체결일 D 시세가 캐시에 아직 없음 (마지막 D') — 데이터 지연"), 그보다 여러 거래일 앞에서 끊겼거나(장기 거래정지 · 상장폐지 직전), 캐시가 체결일 뒤까지 있는데 그날만 없거나, 파일이 없으면 `vendor_unavailable`(거래정지 또는 데이터 공백). 둘 다 주문하지 않는다. 미국 `luld_halt`(LULD 거래 정지) · `pdt_blocked`(PDT 제한)는 **미구현 — 실시간 데이터 레이어 필요**: 값과 라벨(`EXCEPTION_KIND_KO`)만 있고 실행기는 내지 않는다. 기존 기록(`exception_kind` 없음 · 이전 4종)은 그대로 읽힌다.
- 규칙 청산(손절·트레일링·만료·DD −20% 전량): 실행기가 그날 종가로 판정해 `source=rule` 결정(평가 근거 `evidence[portfolio]`)을 먼저 기록하고 `-exit` 매도 주문을 낸다. 같은 날 같은 종목의 에이전트 매도는 `already_selling`으로 차단. 매도는 자동 실행 상한 대상이 아니다. 백테스트와 다른 점: 둘 다 −20% 뒤 피크를 재설정하고 정상 비중으로 돌아간다(`RiskConfig.dd_stop_recovery="reset_peak"`, dd-stop-align 2026-11-13). 백테스트(`simulate.run_simulation`)는 `dd_stop_lock_days`(20거래일) 뒤 스스로, 실행기는 관리자 재가동 때(고정 잠금 없음). 섹터 상한은 섹터 조회(네트워크)가 붙을 때.
