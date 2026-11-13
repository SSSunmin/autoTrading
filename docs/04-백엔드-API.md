# 백엔드 API (기획서 v0.3 기준)

구현 `backend/main.py` (FastAPI) · 실행 `.venv\Scripts\python -m uvicorn backend.main:app --reload --port 8000` · 자동 문서 `http://127.0.0.1:8000/docs`

원칙은 기획서 §0과 같다. 화면이 보여주는 숫자는 전부 기록(`data/audit/*.jsonl`, 메모리 로그, `baseline_summary.json`)에서 읽고 서버가 그대로 서빙한다. 한글 라벨(규칙명·등급명·종목명·주문 상태·운용 모드)은 서버가 붙여 주므로 프론트는 번역 테이블을 갖지 않는다. 데이터의 **값**(rating `Buy`, rule id `price_min`, status `filled`)은 영문 고정이고, 표시용 `*_ko` 필드가 따로 온다.

쓰기는 기획서 §0-5의 세 가지만 있다: **긴급 정지**, **재가동**, **운용 모드 변경**. 셋 다 `data/audit/operations.jsonl`에 append-only로 기록되고, 현재 상태는 그 기록을 재생해서 만든다(`audit.trading_state`). 상태를 따로 저장하지 않으므로 상태와 감사 이력이 어긋날 수 없다.

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `TRADING_DATA_DIR` | `<repo>/data` | `audit/`, `baseline_summary.json`이 있는 폴더 |
| `TRADING_MEMORY_LOG` | summary의 `log_path` | 읽을 메모리 로그 |
| `TRADING_CORS` | localhost 5173·3000 | 허용 오리진, 쉼표 구분 |
| `TRADING_ADMIN_TOKEN` | 없음 | 설정하면 재가동·모드 변경에 `X-Admin-Token` 헤더 필수. 없으면 개발 모드로 누구나 가능하고 기록의 `role`이 `admin(dev, no token)`으로 남는다 |

파일 mtime이 바뀌면 다음 요청에서 다시 읽는다(append-only라 안전).

## 기록 파일

| 파일 | 모델 | 쓰는 쪽 |
|---|---|---|
| `audit/screens.jsonl` | `ScreenRun` | 선별 |
| `audit/decisions.jsonl` | `DecisionRecord` | 에이전트·베이스라인 |
| `audit/orders.jsonl` | `OrderRecord` (order_id·ticker·trade_date·side·quantity·filled_*·status·reason·rules_applied) | 실행 레이어 (아직 없음 → 빈 목록) |
| `audit/operations.jsonl` | `OperationRecord` (kind halt/resume/mode_change/rule_change·at·operator·role·reason·target·before·after) | 이 API의 쓰기 3종 |

## 읽기 엔드포인트

| 메서드·경로 | 화면 | 반환 |
|---|---|---|
| `GET /api/health` | 상태 바 | ok, 코어 버전, 기록 개수(screens·decisions·orders·memory_entries), 서버 시각 |
| `GET /api/labels` | 공통 | `rating_ko`, `rule_ko`, `rule_desc_ko`, `sell_reason_ko`, `mode_ko`, `order_status_ko`, `exception_kind_ko`, 색 관례 |
| `GET /api/trading/mode` | 상태 바·설정 | `mode`(auto/manual/paper)·`mode_ko`·`running`·`halted_at`·`halted_by`·`halt_reason`·`keep_exits`·`label_ko`("자동 매매 실행 중" / "정지됨 · 시각" / "모의투자 모드")·`admin_token_required` |
| `GET /api/screens?from&to&universe` | 2-A | 선별 실행 요약 목록: `run_id, as_of, universe, universe_size, selected_count, rules, rules_ko, stage_counts, recorded_at` |
| `GET /api/screens/{run_id}` | 2-B | `ScreenRun` 전체 + 요약 필드. 후보마다 `name, failed_rules(_ko), filters[{rule, value, threshold, passed, note}], score, rank, selected` |
| `GET /api/decisions/dates` | 3 | 결정일 목록(내림차순) |
| `GET /api/decisions?date&rating&review_only&overrides_only&ticker` | 1·3 | `DecisionRecord` + `name, rating_ko, tradeable, outcome{…}, orders[]`(같은 ticker·trade_date의 주문, 실행 상태 띠) |
| `GET /api/orders?date&status&ticker` | 1 오늘 자동 실행 · 3 | `OrderRecord` + `name, side_ko, status_ko, amount`. 실행 레이어 전까지 `[]` |
| `GET /api/exceptions?date&all_dates` | 1 예외 띠 · 3 예외 큐 · 사이드바 배지 | `[{kind(review/cap_exceeded/order_rejected/vendor_unavailable), kind_ko, ticker, name, trade_date, summary, detail, ref}]`. date 없으면 최신 결정일만 |
| `GET /api/tickers` | 4 선택 | `ticker, name, latest_selected, latest_rank` |
| `GET /api/tickers/{ticker}` | 4 | `name, screening[], decisions[], outcomes[], orders[], timeline[]` |
| `GET /api/performance` | 6 · 1 최근 정산 | `by_rating{…}, per_date[], reflections[{date, ticker, name, rating(_ko), raw_return, alpha, resolved, reflection}], holding, caveat` |
| `GET /api/paper` | 6 포트폴리오 실측 탭 | `scripts/paper_backtest.py`의 `summary.json`: `benchmark{metrics, windows}`, `variants{rules_off, rules_on, rules_on_regime: {label_ko, metrics(total_return·cagr·max_drawdown·dd_peak/trough/recovery·worst_day·volatility…), windows, exits_by_reason(_ko), trades, win_rate, mean_exposure, event_counts, events_tail[40], trades_sample}}`, `criteria[]`(PAPER_TRADING), `caveats[]`. 없으면 404 |
| `GET /api/settings/rules` | 7 | `screen_rules[]`(layer screen), `screen_params`, `risk_rules[]`(RiskConfig 19개, layer hard_rule), `paper_criteria[]`, `auto_rules[]`(`AUTO_EXECUTION`: auto_daily_order_cap·auto_daily_loss_limit·auto_order_max·kill_switch_reset·default_mode), `market`, `mode`(위 상태), `change_history[]`(operations 최신순 + `kind_ko`) |
| `GET /api/positions` | 5 | **501**. 2차 범위 |

## 쓰기 엔드포인트

| 메서드·경로 | 권한 | body | 동작 |
|---|---|---|---|
| `POST /api/trading/halt` | 운영자 (토큰 불필요 — 사람이 항상 멈출 수 있어야 함) | `{operator, reason, keep_exits=true}` (둘 다 비면 422) | 신규 주문 중단, 미체결 주문 취소(건수 기록), 보유 포지션 유지. 이미 정지면 **409**. 반환 `{state, effects{new_orders, open_orders_cancelled, positions, exits_active, note}}` |
| `POST /api/trading/resume` | 관리자 | `{operator, reason}` | 재가동. 실행 중이면 409. 토큰 설정 시 헤더 없거나 틀리면 **403** |
| `POST /api/trading/mode` | 관리자 | `{operator, mode, reason}` | 자동/수동 승인/모의투자 전환. 같은 모드면 409 |
| `POST /api/decisions/{ticker}/{date}/approve` | 운영자 | `{approver, note}` | **수동 승인 모드 전용.** 자동·모의 모드에서는 **409**("승인 단계가 없습니다"). 수동 모드에서는 실행 레이어 전까지 **501** |

기본 운용 모드는 **모의투자(paper)** 다. 기획서 §8-1대로 같은 자동 루프를 모의로 먼저 돌리고, 관리자가 모드 변경으로 자동(auto)에 올린다. 코드상 기본값은 `config.AUTO_EXECUTION["default_mode"]`.

404: 없는 run_id·ticker·결정. 숫자 형식: 수익률·알파는 소수(0.012 = 1.2%), 가격·금액은 원 단위 float.

## 응답 예

`GET /api/trading/mode` (기록 없음 → 기본값)

```json
{"mode": "paper", "mode_ko": "모의투자", "running": true, "halted_at": null, "halted_by": null, "halt_reason": null,
 "keep_exits": true, "mode_changed_at": null, "label_ko": "모의투자 모드", "admin_token_required": false}
```

`POST /api/trading/halt` body `{"operator": "운영자A", "reason": "벤더 지연", "keep_exits": false}`

```json
{"state": {"mode": "paper", "running": false, "halted_at": "2026-11-13T03:10:22Z", "halted_by": "운영자A", "halt_reason": "벤더 지연",
           "keep_exits": false, "label_ko": "정지됨 · 2026-11-13 03:10 UTC", "...": "..."},
 "effects": {"new_orders": "stopped", "open_orders_cancelled": 0, "positions": "kept", "exits_active": false,
             "note": "실행 레이어 연결 전: 기록만 남기고 실제 취소 주문은 보내지 않았습니다"}}
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

## 앞으로 추가될 것

- 실행 레이어가 `orders.jsonl`을 쓰기 시작하면 `/api/orders`·결정 카드의 실행 상태 띠·홈 "오늘 자동 실행"이 그대로 채워진다. 예외 종류(`cap_exceeded`·`vendor_unavailable`)는 지금 규칙 id·사유 텍스트에서 추론하는데(ponytail), 실행 레이어가 직접 kind를 적게 바꾼다.
- 예외 큐의 [재실행] [수동 등급 지정] [무시]는 쓰기 API가 없어 화면에서 비활성. 결정 재실행이 가능해지는 에이전트 연결 단계에 추가.
- 규칙 변경(`rule_change`)은 `OperationRecord`에 자리만 있고 쓰기 API는 없다. 설정 화면의 [수정]은 그때 열린다.
- 에이전트 결정이 들어오면 `DecisionRecord.thesis`에 `render_investment_memo()` 출력이 담기고, 구조화 메모가 필요하면 `memo` 키가 추가된다.
- 매도 검토(`SellReview`)는 `GET /api/reviews?date` 로 별도 서빙 예정. 사유 라벨은 `/api/labels`의 `sell_reason_ko`.
- 포지션·Protections는 실행 레이어와 함께. 계약(필드)은 기획서 §3을 따른다.
