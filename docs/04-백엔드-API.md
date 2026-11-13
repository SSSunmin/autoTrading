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
| `operations.jsonl` | `OperationRecord` (kind halt/resume/mode_change/rule_change·at·operator·role·reason·target·before·after·market·scope) | 이 API의 쓰기 3종 · 실행기 자신의 정지·재가동(`operator=system`, `role=auto`). 정지·재가동의 `target`: `manual`(사람 — 이전 기록의 `running`도 manual로 읽음) · `daily_loss`(일일 손실) · `dd_stop`(−20% 드로다운) |

## 읽기 엔드포인트

| 메서드·경로 | 화면 | 반환 |
|---|---|---|
| `GET /api/health?market` | 상태 바 | ok, 코어 버전, `market`·`market_ko`(고정 "KRX" 없음)·`markets[]`, 그 시장의 기록 개수(screens·decisions·orders·memory_entries), `decisions_by_source{agent, rule}`(규칙 청산은 에이전트 판단 수가 아니다), 서버 시각 |
| `GET /api/labels` | 공통 (시장 무관) | `rating_ko`, `rule_ko`, `rule_desc_ko`, `sell_reason_ko`, `mode_ko`, `order_status_ko`, `exception_kind_ko`, `exit_reason_ko`, `market_ko`, `halt_scope_ko`, `halt_target_ko`(정지 종류 manual · daily_loss · dd_stop → 한국어), `markets{KRX, US: {market, label_ko, currency, currency_symbol, benchmark, benchmark_name_ko, timezone, session(현지), session_kst(오늘 기준, 서머타임 반영), name_style(name / name_ticker)}}`, `color_convention{up: red, down: blue, note}`(두 시장 공통) |
| `GET /api/markets` | 1 시장 요약 카드 | 시장마다 한 줄(합산 없음): 위 시장 라벨 + `as_of, equity, initial_cash, day_return, drawdown, positions`(보유 종목 수)`, state`(아래 운용 상태)`, next_run_kst`(ISO, 다음 평일 장 마감 5분 뒤)`, next_run_label_ko`("11-14 06:05 KST")`, error`. 한 시장 장부가 파손되면 그 카드만 `error` 문구와 숫자·state `null`이고 다른 카드는 정상이다. `market` 쿼리는 받지 않는다(두 시장 전용). 다음 실행은 평일만 본다(휴장일 미반영 — 가격 캐시에 앞날 달력이 없음) |
| `GET /api/trading/mode?market` | 상태 바·설정 | 그 시장의 `market`·`market_ko`·`mode`(auto/manual/paper)·`mode_ko`·`running`·`halted_at`·`halted_by`·`halt_reason`·`keep_exits`·`halted_by_ko`(시스템 정지면 "시스템 자동 정지", 아니면 운영자 이름)·`halt_target`(`manual`/`daily_loss`/`dd_stop`, 실행 중이면 null)·`halt_target_ko`("운영자 긴급 정지" / "시스템 · 일일 손실 정지" / "시스템 · 드로다운 정지")·`dd_stop`(−20% 정지가 서 있음 — 사람이 그 위에 정지를 더 걸었어도 true, 재가동에서 피크 재설정을 고를 수 있음)·`label_ko`("국내 자동 매매 실행 중" / "미국 정지됨 · 시각 UTC · 정지 주체" — 시스템 정지면 주체 자리에 `halt_target_ko` / "국내 모의투자 모드")·`admin_token_required` |
| `GET /api/screens?from&to&universe&market` | 2-A | 선별 실행 요약 목록: `run_id, as_of, universe, universe_size, selected_count, rules, rules_ko, stage_counts, recorded_at` |
| `GET /api/screens/{run_id}?market` | 2-B | `ScreenRun` 전체 + 요약 필드. 후보마다 `name, failed_rules(_ko), filters[{rule, value, threshold, passed, note}], score, rank, selected` |
| `GET /api/decisions/dates?market` | 3 | 결정일 목록(내림차순) |
| `GET /api/decisions?date&rating&review_only&overrides_only&ticker&source&market` (`source=agent`면 에이전트 판단만 — 등급 필터 "매도"에 규칙 청산을 빼려면 함께 쓴다, 그 외 값은 422) | 1·3 | `DecisionRecord` + `source`(agent/rule)·`source_ko`("에이전트 결정"/"규칙 청산")·`name, rating_ko, tradeable, outcome{…}`(rule이면 null), `orders[]`(같은 ticker·trade_date의 주문 중 자기 것: rule 결정은 `-exit` 주문만, agent 결정은 그 외) |
| `GET /api/orders?date&status&ticker&market` | 1 오늘 자동 실행 · 3 | 주문마다 현재 상태 한 줄: `OrderRecord`(+ `market`) + `name, side_ko, status_ko, amount`(그 시장 통화). status는 filled/partial/open/blocked/rejected/exception/cancelled. date 형식·status 값이 틀리면 **400** |
| `GET /api/exceptions?date&all_dates&market` | 1 예외 띠 · 3 예외 큐 · 사이드바 배지 | `[{kind(review/cap_exceeded/order_rejected/vendor_unavailable), kind_ko, ticker, name, trade_date, summary, detail, ref}]`. date 없으면 최신 결정일만. 주문의 kind는 `exception_kind`(없으면 규칙 id·사유에서 추론). REVIEW 결정의 예외 주문은 결정 항목과 같아 한 번만 나온다 |
| `GET /api/tickers?market` | 4 선택 | `ticker, name, latest_selected, latest_rank` |
| `GET /api/tickers/{ticker}?market` | 4 | `name, screening[], decisions[], outcomes[], orders[], timeline[]` |
| `GET /api/performance?market` | 6 · 1 최근 정산 | 국내만(`baseline_summary.json`). 미국은 결정 베이스라인이 아직 없어 **404**.  `by_rating{…}, per_date[], reflections[{date, ticker, name, rating(_ko), raw_return, alpha, resolved, reflection}], holding, caveat` |
| `GET /api/paper?market` | 6 포트폴리오 실측 탭 · 국내 2차 ↔ 미국 3차 비교 | `scripts/paper_backtest.py [--market US]`의 `summary.json`(국내 `results/paper/`, 미국 `results/paper_us/`) + `market, currency`: `benchmark{metrics, windows}`, `variants{rules_off, rules_on, rules_on_regime: {label_ko, metrics(total_return·cagr·max_drawdown·dd_peak/trough/recovery·worst_day·volatility…), windows, exits_by_reason(_ko), trades, win_rate, mean_exposure, event_counts, events_tail[40], trades_sample}}`, `criteria[]`(PAPER_TRADING), `caveats[]`. 없으면 404("…scripts/paper_backtest.py --market US를 먼저 실행") |
| `GET /api/settings/rules?market` | 7 | `screen_rules[]`(layer screen), `screen_params`, `risk_rules[]`(RiskConfig 19개, layer hard_rule, 두 시장 공통), `paper_criteria[]`, `auto_rules[]`(`config.auto_execution(market)`: 공통 auto_daily_loss_limit·kill_switch_reset·default_mode + 그 시장 금액 auto_daily_order_cap·auto_order_max — 금액 행은 `currency`, 그 외 `null`), `auto_by_market{KRX, US}`, `market`(그 시장 상수)·`market_code`·`markets`(두 시장 `config.MARKETS`), `mode`(그 시장 상태), `change_history[]`(**두 시장** 운영 기록 합침, 최신순 — 같은 초면 기록 순서 — + `kind_ko`·`market_ko`), `history_errors[]`(다른 시장 운영 기록을 못 읽으면 그 시장 행을 빼고 사유를 적음 — 고른 시장 자신의 파손은 500) |
| `GET /api/positions?as_of&market` | 1 KPI·리스크·Protections · 5 | 그 시장의 모의투자 장부(체결 기록을 캐시 종가로 재생, 저장 상태 없음): `market, currency, as_of, initial_cash`(KRX 1억 · US $100,000)`, day_return`(전일 평가 대비)`, cash, holdings_value, equity, peak, drawdown, gross_exposure, realized_pnl, realized_today, unrealized_pnl, regime_on, peak_reset{at, to}|null`(마지막 −20% 정지 뒤 관리자 피크 재설정, 없으면 null — `peak`·`drawdown`이 이 값부터 계산됨)`, positions[{ticker, name, quantity, avg_price, entry_date, last_price, last_date, market_value, unrealized(_pct), weight, stop_price, trailing_active, trailing_high, trailing_stop_price, holding_days, expiry_date, exit_signal{reason, note}|null}], protections[{id, label_ko, active, detail, until, tickers[]}], exits_by_reason(_ko)`(stop_loss·trailing·expired·force·exit_signal(등급 하향)별 건수)`, closed[{…, reason}], limits{…, auto_daily_order_cap}`(그 시장 통화)`, note, mode`. `exit_signal`은 실행기가 그날 종가에 규칙 청산 주문으로 바꾸는 판정과 같은 함수(`execution.exit_signals`). as_of 기본값은 데이터의 마지막 거래일, 형식이 틀리면 **400**. 이전에는 **501**이었다(본문 `{implemented:false}`) |

## 쓰기 엔드포인트

| 메서드·경로 | 권한 | body | 동작 |
|---|---|---|---|
| `POST /api/trading/halt` | 운영자 (토큰 불필요 — 사람이 항상 멈출 수 있어야 함) | `{operator, reason, keep_exits=true, scope="all"}` (operator·reason 비면 422, scope가 all·KRX·US 외면 **400**) | **범위**: `all`(기본)이면 실행 중인 모든 시장에, 시장 이름이면 그 시장에만 정지 한 줄씩 — 같은 `at`·같은 사유, 기록의 `scope`에 요청 범위. 이미 정지된 시장은 `skipped`(`keep_exits` 포함 — true면 그 시장 청산은 계속 돈다)로 건너뛴다. 단, 청산이 도는 정지(`keep_exits=true`)에 `keep_exits=false`가 오면 건너뛰지 않고 정지 한 줄을 더 써서 청산까지 멈춘다(상태는 마지막 정지 줄을 따름). 건너뛸 시장만 남으면 **409**. **한 시장 기록이 파손돼도 다른 시장 정지는 막히지 않는다**: `operations.jsonl`을 못 읽는 시장에도 정지 줄을 append하고(그 시장 실행기는 어차피 그 파일을 못 읽어 exit 4로 멈춘다) 결과에 `error`를 싣는다. 시장마다: 신규 주문 중단, 정지 기록(취소할 order_id 목록 포함)을 쓴 뒤 미체결(open) 주문마다 `cancelled` 줄을 orders.jsonl에 append, 보유 포지션 유지. `keep_exits=true`면 미체결 **매도**는 남기고, 정지 중에도 실행기가 청산만 계속 돈다(미체결 매도 체결 + 그날 규칙 청산; 신규 매수·에이전트 결정 주문은 없음). `keep_exits=false`면 실행기가 거부(종료 코드 3). orders.jsonl이 파손돼 읽히지 않아도 정지는 기록되고 `open_orders_cancelled: null` + 사유 note. 반환 `{scope, scope_ko, results[{market, state, error, effects{new_orders, open_orders_cancelled, positions, exits_active, note}}], skipped[{market, reason, keep_exits}]}` |
| `POST /api/trading/resume` | 관리자 | `{operator, reason, scope="all", reset_peak=true}` (`reset_peak`가 bool이 아니면 422, scope가 틀리면 400) | 재가동(기록 `target=manual`). **−20% 정지가 서 있는 시장**에서 `reset_peak=true`(기본)면 그 시장의 마지막 종가 평가액을 새 드로다운 피크로 기록한다(`after{peak_reset_to, peak_reset_at(종가일), peak_before}`). `false`면 피크를 그대로 두며, 드로다운이 여전히 −20% 이하면 다음 실행에서 다시 정지한다. 다른 정지(사람·일일 손실)에서는 `reset_peak`가 아무것도 하지 않는다. `all`이면 정지된 모든 시장, 시장 이름이면 그 시장만(다른 시장은 그대로 정지). 실행 중인 시장은 `skipped`. 시장마다 정지 전에 나간 미체결이 남아 있으면(정지 때 orders.jsonl을 못 읽은 경우) 취소 기록(`keep_exits`면 매도는 유지). 범위 안 시장이 전부 실행 중이면 409. 토큰 설정 시 헤더 없거나 틀리면 **403**. 반환 `{scope, scope_ko, resumed[], skipped[{market, reason}], peak_resets{시장: {to, at, before}}, states{시장: 상태}}` |
| `POST /api/trading/mode` | 관리자 | `{operator, mode, reason, market}` (market 없으면 422, 틀리면 400) | **그 시장의** 자동/수동 승인/모의투자 전환. 같은 모드면 409. paper에서 나갈 때 미체결 모의 주문을 취소 기록(기록의 `after.open_orders_cancelled`) |
| `POST /api/decisions/{ticker}/{date}/approve?market` | 운영자 | `{approver, note}` | **수동 승인 모드 전용.** 자동·모의 모드에서는 **409**("승인 단계가 없습니다"). 수동 모드에서는 실행 레이어 전까지 **501** |

정지·재가동·모드 변경과 모의 실행은 **시장마다** 같은 잠금(`audit/<시장>/execution.lock`)을 잡는다. 범위 정지·재가동은 범위 안 모든 시장의 잠금을 `config.MARKETS` 순서로 잡아 교착이 없고, 실행기는 자기 시장 잠금만 잡는다. 실행기가 상태(정지·모드)를 확인한 뒤 주문을 쓰는 사이에 정지·모드 변경이 끼어들 수 없다.

기본 운용 모드는 **모의투자(paper)** 다. 기획서 §8-1대로 같은 자동 루프를 모의로 먼저 돌리고, 관리자가 모드 변경으로 자동(auto)에 올린다. 코드상 기본값은 `config.AUTO_EXECUTION["default_mode"]`. 모드는 시장마다 따로다.

자동 실행 금액 한도는 시장 통화로 시장마다(`config.AUTO_EXECUTION["by_market"]`, 읽기는 `config.auto_execution(market)`): 국내 일일 1억원 · 주문당 3천만원, 미국 일일 $70,000 · 주문당 $20,000(Q3). 비교는 같은 통화 안에서만 한다.

404: 없는 run_id·ticker·결정. 숫자 형식: 수익률·알파는 소수(0.012 = 1.2%), 가격·금액은 그 시장 통화 단위 float(국내 원, 미국 달러). 주문 사유 문구의 금액도 그 통화("1,234원" / "$1,234.56").

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
- **일일 손실 정지는 그날 세션까지**(2026-11-13 사용자 결정): 정지일보다 뒤인 **다음 거래일**의 첫 실행이 잠금 안에서 `resume`(`operator=system, role=auto, target=daily_loss, reason="일일 손실 정지 자동 해제(정지일 YYYY-MM-DD)", after{running:true, date, halted_on}`)을 먼저 쓰고 정상 진행한다. 한 번만(해제 뒤엔 실행 중이라 다시 쓰지 않음), 그 시장만, 휴장일 날짜 실행에서는 해제하지 않는다. 서 있는 정지가 **전부** system의 `daily_loss`일 때만 해제한다: 사람이 그 위에 정지를 걸었거나 −20% 정지가 함께 서 있으면 관리자 재가동을 기다린다. 관리자가 먼저 재가동해도 된다. 자동 정지 횟수(`PAPER_TRADING.max_daily_loss_breaches`)는 정지 기록 수로 그대로 센다.
- **−20% 드로다운 정지**: 드로다운이 `RiskConfig.dd_stop`(−20%) 이하인 거래일 종가에 실행기가 전량 규칙 청산(`force`, 각 종목의 손절이 먼저면 그 사유)을 낸 뒤 `halt`(`operator=system, role=auto, target=dd_stop, after{keep_exits:true, date, equity, peak, open_orders_cancelled}`)를 쓰고 미체결 매수를 취소한다. 청산 매도는 정지 중에도 체결된다. 같은 날 일일 손실 정지가 먼저 쓰였어도 함께 쓰고, 이 정지가 서 있는 동안 다시 쓰지 않으며, 관리자 재가동(피크 재설정)이 그 날짜 이후에 이미 있으면 그 날짜를 다시 돌려도 쓰지 않는다. 실행기는 해제하지 않는다(관리자만).
- **드로다운 피크**: 마지막 피크 재설정(`peak_reset_at` ≤ 그날) 이후의 종가 평가액 최고치(재설정 값 포함), 재설정이 없으면 초기 자본부터. 신규 비중 절반(−10%) · 정지(−20%) · 사이징 스케일(`drawdown_scale`) · `/api/positions`의 `peak`·`drawdown`·MaxDrawdown 가드가 모두 이 피크 하나를 쓴다.
- 규칙 청산(손절·트레일링·만료·DD −20% 전량): 실행기가 그날 종가로 판정해 `source=rule` 결정(평가 근거 `evidence[portfolio]`)을 먼저 기록하고 `-exit` 매도 주문을 낸다. 같은 날 같은 종목의 에이전트 매도는 `already_selling`으로 차단. 매도는 자동 실행 상한 대상이 아니다. 백테스트와 다른 점: `simulate.run_simulation`은 −20% 뒤 `dd_stop_lock_days`(20거래일) 잠금 후 스스로 피크를 재설정하고, 이전 피크 근처로 회복할 때까지 절반 비중으로 산다. 실행기는 관리자 재가동 때 피크를 재설정하고(고정 잠금 없음) 절반 비중 단계가 없다(새 피크 대비 −10%부터 절반). 섹터 상한은 섹터 조회(네트워크)가 붙을 때.
