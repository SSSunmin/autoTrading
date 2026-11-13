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
| `audit/decisions.jsonl` | `DecisionRecord` (+ `source`: `agent` 기본 / `rule`) | 에이전트·베이스라인(`agent`) · 모의 실행기의 규칙 청산(`rule`, 주문보다 먼저 기록) |
| `audit/orders.jsonl` | `OrderRecord` (order_id·ticker·trade_date·side·quantity·filled_*·fill_date·status·exception_kind·reason·rules_applied) | 모의 실행 `scripts/paper_execute.py`(`execution.py`) · 긴급 정지(미체결 취소). 같은 order_id의 마지막 줄이 현재 상태. 규칙 청산은 `paper-<date>-<ticker>-exit`, `rules_applied=[stop_loss/trailing/expired/force]` |
| `audit/operations.jsonl` | `OperationRecord` (kind halt/resume/mode_change/rule_change·at·operator·role·reason·target·before·after) | 이 API의 쓰기 3종 · 실행기의 일일 손실 자동 정지(`operator=system`, `role=auto`, `target=daily_loss`) |

## 읽기 엔드포인트

| 메서드·경로 | 화면 | 반환 |
|---|---|---|
| `GET /api/health` | 상태 바 | ok, 코어 버전, 기록 개수(screens·decisions·orders·memory_entries), `decisions_by_source{agent, rule}`(규칙 청산은 에이전트 판단 수가 아니다), 서버 시각 |
| `GET /api/labels` | 공통 | `rating_ko`, `rule_ko`, `rule_desc_ko`, `sell_reason_ko`, `mode_ko`, `order_status_ko`, `exception_kind_ko`, 색 관례 |
| `GET /api/trading/mode` | 상태 바·설정 | `mode`(auto/manual/paper)·`mode_ko`·`running`·`halted_at`·`halted_by`·`halt_reason`·`keep_exits`·`halted_by_ko`(시스템 정지면 "시스템 자동 정지", 아니면 운영자 이름)·`label_ko`("자동 매매 실행 중" / "정지됨 · 시각 UTC · 정지 주체" / "모의투자 모드")·`admin_token_required` |
| `GET /api/screens?from&to&universe` | 2-A | 선별 실행 요약 목록: `run_id, as_of, universe, universe_size, selected_count, rules, rules_ko, stage_counts, recorded_at` |
| `GET /api/screens/{run_id}` | 2-B | `ScreenRun` 전체 + 요약 필드. 후보마다 `name, failed_rules(_ko), filters[{rule, value, threshold, passed, note}], score, rank, selected` |
| `GET /api/decisions/dates` | 3 | 결정일 목록(내림차순) |
| `GET /api/decisions?date&rating&review_only&overrides_only&ticker&source` (`source=agent`면 에이전트 판단만 — 등급 필터 "매도"에 규칙 청산을 빼려면 함께 쓴다, 그 외 값은 422) | 1·3 | `DecisionRecord` + `source`(agent/rule)·`source_ko`("에이전트 결정"/"규칙 청산")·`name, rating_ko, tradeable, outcome{…}`(rule이면 null), `orders[]`(같은 ticker·trade_date의 주문 중 자기 것: rule 결정은 `-exit` 주문만, agent 결정은 그 외) |
| `GET /api/orders?date&status&ticker` | 1 오늘 자동 실행 · 3 | 주문마다 현재 상태 한 줄: `OrderRecord` + `name, side_ko, status_ko, amount`. status는 filled/partial/open/blocked/rejected/exception/cancelled. date 형식·status 값이 틀리면 **400** |
| `GET /api/exceptions?date&all_dates` | 1 예외 띠 · 3 예외 큐 · 사이드바 배지 | `[{kind(review/cap_exceeded/order_rejected/vendor_unavailable), kind_ko, ticker, name, trade_date, summary, detail, ref}]`. date 없으면 최신 결정일만. 주문의 kind는 `exception_kind`(없으면 규칙 id·사유에서 추론). REVIEW 결정의 예외 주문은 결정 항목과 같아 한 번만 나온다 |
| `GET /api/tickers` | 4 선택 | `ticker, name, latest_selected, latest_rank` |
| `GET /api/tickers/{ticker}` | 4 | `name, screening[], decisions[], outcomes[], orders[], timeline[]` |
| `GET /api/performance` | 6 · 1 최근 정산 | `by_rating{…}, per_date[], reflections[{date, ticker, name, rating(_ko), raw_return, alpha, resolved, reflection}], holding, caveat` |
| `GET /api/paper` | 6 포트폴리오 실측 탭 | `scripts/paper_backtest.py`의 `summary.json`: `benchmark{metrics, windows}`, `variants{rules_off, rules_on, rules_on_regime: {label_ko, metrics(total_return·cagr·max_drawdown·dd_peak/trough/recovery·worst_day·volatility…), windows, exits_by_reason(_ko), trades, win_rate, mean_exposure, event_counts, events_tail[40], trades_sample}}`, `criteria[]`(PAPER_TRADING), `caveats[]`. 없으면 404 |
| `GET /api/settings/rules` | 7 | `screen_rules[]`(layer screen), `screen_params`, `risk_rules[]`(RiskConfig 19개, layer hard_rule), `paper_criteria[]`, `auto_rules[]`(`AUTO_EXECUTION`: auto_daily_order_cap·auto_daily_loss_limit·auto_order_max·kill_switch_reset·default_mode), `market`, `mode`(위 상태), `change_history[]`(operations 최신순 + `kind_ko`) |
| `GET /api/positions?as_of` | 1 KPI·리스크·Protections · 5 | 모의투자 장부(체결 기록을 캐시 종가로 재생, 저장 상태 없음): `as_of, initial_cash, cash, holdings_value, equity, peak, drawdown, gross_exposure, realized_pnl, realized_today, unrealized_pnl, regime_on, positions[{ticker, name, quantity, avg_price, entry_date, last_price, last_date, market_value, unrealized(_pct), weight, stop_price, trailing_active, trailing_high, trailing_stop_price, holding_days, expiry_date, exit_signal{reason, note}|null}], protections[{id, label_ko, active, detail, until, tickers[]}], exits_by_reason(_ko)`(stop_loss·trailing·expired·force·exit_signal(등급 하향)별 건수)`, closed[{…, reason}], limits{…}, note, mode`. `exit_signal`은 실행기가 그날 종가에 규칙 청산 주문으로 바꾸는 판정과 같은 함수(`execution.exit_signals`). as_of 기본값은 데이터의 마지막 거래일, 형식이 틀리면 **400**. 이전에는 **501**이었다(본문 `{implemented:false}`) |

## 쓰기 엔드포인트

| 메서드·경로 | 권한 | body | 동작 |
|---|---|---|---|
| `POST /api/trading/halt` | 운영자 (토큰 불필요 — 사람이 항상 멈출 수 있어야 함) | `{operator, reason, keep_exits=true}` (둘 다 비면 422) | 신규 주문 중단, 정지 기록(취소할 order_id 목록 포함)을 쓴 뒤 미체결(open) 주문마다 `cancelled` 줄을 orders.jsonl에 append, 보유 포지션 유지. `keep_exits=true`면 미체결 **매도**는 남기고, 정지 중에도 실행기가 청산만 계속 돈다(미체결 매도 체결 + 그날 규칙 청산; 신규 매수·에이전트 결정 주문은 없음). `keep_exits=false`면 실행기가 거부(종료 코드 3). orders.jsonl이 파손돼 읽히지 않아도 정지는 기록되고 `open_orders_cancelled: null` + 사유 note. 이미 정지면 **409**. 반환 `{state, effects{new_orders, open_orders_cancelled, positions, exits_active, note}}` |
| `POST /api/trading/resume` | 관리자 | `{operator, reason}` | 재가동. 정지 전에 나간 미체결이 남아 있으면(정지 때 orders.jsonl을 못 읽은 경우) 취소 기록(`keep_exits`면 매도는 유지). 실행 중이면 409. 토큰 설정 시 헤더 없거나 틀리면 **403** |
| `POST /api/trading/mode` | 관리자 | `{operator, mode, reason}` | 자동/수동 승인/모의투자 전환. 같은 모드면 409. paper에서 나갈 때 미체결 모의 주문을 취소 기록(기록의 `after.open_orders_cancelled`) |
| `POST /api/decisions/{ticker}/{date}/approve` | 운영자 | `{approver, note}` | **수동 승인 모드 전용.** 자동·모의 모드에서는 **409**("승인 단계가 없습니다"). 수동 모드에서는 실행 레이어 전까지 **501** |

정지·재가동·모드 변경과 모의 실행은 같은 잠금(`audit/execution.lock`)을 잡는다. 실행기가 상태(정지·모드)를 확인한 뒤 주문을 쓰는 사이에 정지·모드 변경이 끼어들 수 없다.

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
           "keep_exits": false, "label_ko": "정지됨 · 2026-11-13 03:10 UTC · 운영자A", "halted_by_ko": "운영자A", "...": "..."},
 "effects": {"new_orders": "stopped", "open_orders_cancelled": 0, "positions": "kept", "exits_active": false,
             "note": "모의투자: 미체결 주문 0건을 취소 기록했습니다(orders.jsonl). 실제 증권사 주문은 없습니다"}}
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
- 거래일 달력은 `^KS11` 캐시 한 종목이다. 벤더 공백일은 휴장으로 취급돼 체결이 하루 밀린다(미해결 — `^KS11` 이상치 검증과 같이). 결정일이 달력 끝보다 뒤면 실행을 거부한다.
- 모의 실행은 API가 아니라 CLI(`scripts/paper_execute.py --date D | --all`, 종료 코드 0 완료 · 2 인자 오류 · 3 거부 · 4 기록 파손)로 돈다. `--all`은 결정일만이 아니라 가격 데이터 끝까지 **모든 거래일**을 돌고(규칙 청산은 매일 종가 판정), 끝에 미설명 주문 수를 출력한다. 쓰기 API는 기획서 §0-5의 세 가지로 유지.
- 일일 손실 자동 정지: 그날 장부 손익(전일 평가 대비)이 `AUTO_EXECUTION.auto_daily_loss_limit`(−2%) 이하면 실행기가 잠금 안에서 정지 기록(`operator=system, role=auto, target=daily_loss, after{keep_exits:true, date, day_return, equity, open_orders_cancelled}`)을 쓰고 미체결 매수를 취소한 뒤 그날은 청산만. 날짜당 한 번(관리자가 재가동한 뒤 같은 날을 다시 돌려도 다시 정지하지 않는다). 재가동은 `POST /api/trading/resume`(관리자).
- 규칙 청산(손절·트레일링·만료·DD −20% 전량): 실행기가 그날 종가로 판정해 `source=rule` 결정(평가 근거 `evidence[portfolio]`)을 먼저 기록하고 `-exit` 매도 주문을 낸다. 같은 날 같은 종목의 에이전트 매도는 `already_selling`으로 차단. 매도는 자동 실행 상한 대상이 아니다. 한계: −20% 정지 뒤 피크 재설정(simulate의 `dd_stop_lock_days` 잠금 후 절반 비중 재가동)이 없다. 전량 청산 뒤 장부는 전액 현금이라 평가가 움직이지 않으므로 **신규 매수가 영구히 `dd_stop`으로 막힌다**. 이때 운영 기록에 정지가 없어 상태는 "실행 중"으로 보인다(결정 카드의 blocked `dd_stop`로만 보임). 이 지점부터 실행기와 백테스트 결과가 갈라진다. 섹터 상한은 섹터 조회(네트워크)가 붙을 때.
