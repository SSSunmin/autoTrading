# 백엔드 API (기획서 v0.3 기준)

구현 `backend/main.py` (FastAPI) · 실행 `.venv\Scripts\python -m uvicorn backend.main:app --reload --port 8000` · 자동 문서 `http://127.0.0.1:8000/docs`

원칙은 기획서 §0과 같다. 화면이 보여주는 숫자는 전부 기록(`data/trading.db` — `TRADING_STORE=jsonl`이면 `data/audit/<시장>/*.jsonl`, 메모리 로그, `baseline_summary.json`)에서 읽고 서버가 그대로 서빙한다. 한글 라벨(규칙명·등급명·종목명·주문 상태·운용 모드)은 서버가 붙여 주므로 프론트는 번역 테이블을 갖지 않는다. 데이터의 **값**(rating `Buy`, rule id `price_min`, status `filled`)은 영문 고정이고, 표시용 `*_ko` 필드가 따로 온다.

쓰기는 기획서 §0-5의 세 가지만 있다: **긴급 정지**, **재가동**, **운용 모드 변경**. 셋 다 그 시장의 운영 기록(`operations` 테이블 — JSONL이면 `operations.jsonl`)에 append-only로 기록되고, 현재 상태는 그 기록을 재생해서 만든다(`audit.trading_state`). 상태를 따로 저장하지 않으므로 상태와 감사 이력이 어긋날 수 없다.

## 시장 축 (docs/08)

국내(`KRX`)와 미국(`US`)은 **별도 포트폴리오**다. 기록·장부·운용 상태·모드·자동 실행 한도가 시장마다 따로 있고, 환율 환산·두 시장 합산·통합 노출 한도는 없다. 금액은 그 시장 통화(KRW / USD)로 오고 섞이지 않는다.

- **읽기는 전부 `market` 쿼리**를 받는다. 기본값 `KRX`, `KRX`·`US` 외 값(`JP`, 소문자 `us`, 빈 문자열)은 **400** `"market은 KRX, US 중 하나여야 합니다: 'JP'"`. 기본값은 쿼리가 **없을 때만** 쓴다(오타가 국내 장부를 읽지 않게).
- 쓰기: 긴급 정지·재가동은 `scope`(`all` 기본 | `KRX` | `US`), 모드 변경은 `market`(필수 — 다른 시장 모드를 실수로 바꾸지 않게 기본값 없음), 승인은 `market` 쿼리.
- 색 관례는 시장과 무관하게 하나(국내 관례: 빨강=상승·매수, 파랑=하락·매도, docs/08 §4 Q1).
- 미국 종목 표시는 "한글명 티커"(`애플 AAPL`), 국내는 한글명만(Q4). 서버는 `name`(한글명)을 주고, 화면이 시장 라벨의 `name_style`로 조합한다.

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `TRADING_DATA_DIR` | `<repo>/data` | `trading.db`, `audit/`, `baseline_summary.json`, `results/paper*/`, `cache/`가 있는 폴더 |
| `TRADING_STORE` | `sqlite` | 기록 정본: `sqlite`(`TRADING_DATA_DIR/trading.db`) 또는 `jsonl`(`audit/<시장>/*.jsonl`, 이전 방식). 그 밖의 값은 `ValueError`. 아래 "기록 저장소" |
| `TRADING_MEMORY_LOG` | summary의 `log_path` | 읽을 메모리 로그(국내만 — 미국 결정 베이스라인은 아직 없음) |
| `TRADING_CORS` | localhost 5173·3000 | 허용 오리진, 쉼표 구분 |
| `TRADING_ADMIN_TOKEN` | 없음 | 설정하면 재가동·모드 변경에 `X-Admin-Token` 헤더 필수. 없으면 개발 모드로 누구나 가능하고 기록의 `role`이 `admin(dev, no token)`으로 남는다 |
| `TRADING_STATIC_DIR` | 없음 | 빌드한 대시보드(`frontend/dist`)를 이 앱이 `/`에서 서빙(Docker 이미지). 클라이언트 경로(`/decisions` …)는 `index.html`로, `/api/…`는 그대로 404 |
| `TRADING_ENV_FILE` | `<repo>/.env` | `/api/data/status`가 키 **유무**를 볼 `.env`(환경 변수가 먼저). 값은 어떤 응답에도 나오지 않는다 |

**백엔드는 기록 파일을 읽지도 감시하지도 않는다**(db-store-api, 2026-11-16): `_Store`는 `agent_trading_core.audit`의 조회 함수만 부르고, 캐시는 기록 종류별로 `AuditLog.stamp()`(sqlite: 테이블별 `max(id)` — append-only라 커지기만 함 · jsonl: 그 장부 파일의 크기·mtime)가 바뀌면 버린다. 다른 프로세스(스케줄러 · 실행기 · CLI)가 쓴 기록은 다음 요청에 보인다. 결정은 통째로 캐시하지 않고 요청마다 범위로 읽는다(아래 "조회 범위"). 베이스라인 요약 · 메모리 로그는 기록이 아니라 파일이라 mtime 그대로. 이전의 touch 스탬프(sqlite 쓰기마다 빈 `*.jsonl`을 건드림)는 없앴다 — sqlite 저장소에서 `audit/` 아래에 JSONL 파일이 생기지 않는다.

## 기록 저장소 (2026-11-14, db-store)

**정본은 하나**: 기본 SQLite `TRADING_DATA_DIR/trading.db`(표준 `sqlite3`, 의존성 없음). `TRADING_STORE=jsonl`이면 이전처럼 JSONL 파일이 정본이고 DB는 쓰지 않는다 — 둘을 동시에 쓰는 경로는 없다. `audit.AuditLog`의 공개 함수(`record_*` · `screens/decisions/orders/operations/runs` · `market_audit` · `latest_orders` · `trading_state` · `decision_source`)는 그대로이고 내부 저장만 바뀐다(`agent_trading_core/store/`). `AuditLog.directory`(`audit/<시장>/`)는 sqlite에서도 남는다: 실행 잠금(`execution_lock`, `daily_loop_*`), 모델 원문(`agent_raw/`)이 거기 있다.

| 테이블 | 종류(이전 파일) | 풀어 둔 조회 열 · 인덱스 |
|---|---|---|
| `screen_runs` | `ScreenRun`(`screens.jsonl`) | `run_id`, `as_of` · (market, as_of), (run_id) |
| `screen_candidates` | 선별 후보 한 종목(`ScreenRun.candidates`) | `screen_id` → `screen_runs(id)` FK, `ticker`, `selected`, `rank`, `score` · (screen_id), (market, ticker). 선별과 한 트랜잭션, 읽기는 `screen_runs.payload`가 정본 |
| `decisions` | `DecisionRecord` | `ticker`, `trade_date`, `rating`, `source`, `screen_run_id` · (market, trade_date), (market, ticker, trade_date), (screen_run_id) |
| `orders` | `OrderRecord` | `order_id`, `ticker`, `trade_date`, `side`, `status`, `fill_date` · (market, trade_date), (market, status), (market, ticker, trade_date), (order_id) |
| `operations` | `OperationRecord` | `kind`, `target`, `operator` · (market, recorded_at), (market, kind) |
| `run_records` | `RunRecord`(`runs.jsonl`) | `stage`, `trade_date`, `status` · (market, stage, trade_date) |
| `schema_version` | 1행(`version` 2 — 2: 코드가 쓴 기록은 `source_hash` NULL) | 코드가 읽는 버전과 다르면 연결 거부 → DB를 `data/backup/`으로 옮기고 처음부터 적재 |

- **공통 열**: `id INTEGER PRIMARY KEY`(삽입 순서 = 재생 순서), `market`(시장 폴더 대신), `recorded_at`(기록 자신의 UTC 시각: screens·decisions `recorded_at`, orders `placed_at`, operations `at`, runs `ended_at`), `payload`(기록 JSON 원문 그대로 — 읽기·내보내기는 이 값), `source_hash` UNIQUE(`screen_candidates` 제외 — JSONL에서 적재한 줄의 키, 코드가 새로 쓴 기록은 NULL). 피크 재설정은 운영 기록(`resume`)을 재생해 얻으므로 `peak_resets` 테이블은 두지 않았다.
- **append-only를 DB가 강제**: 모든 테이블(`schema_version` 포함)에 `BEFORE UPDATE` · `BEFORE DELETE` 트리거 `RAISE(ABORT, 'append-only')`. `INSERT OR REPLACE`(충돌 행을 지우고 넣음)도 막는다: 우리 연결은 `recursive_triggers`로 삭제 트리거가 걸리고, 그 밖의 클라이언트(sqlite3 CLI 등)에도 `BEFORE INSERT` 트리거가 있는 `id`로의 삽입은 거부(`*_no_replace`), 적재된 줄의 `source_hash`로의 삽입은 무시(`*_same_hash`, `RAISE(IGNORE)` — `ON CONFLICT DO NOTHING`과 같음)한다. 새 행 삽입만 된다. 상태 변경은 새 행(주문 open → filled는 같은 `order_id`의 새 행, `latest_orders`가 마지막 행을 고름). **상태 테이블은 없다** — 모드·정지·장부·모의 판정은 지금처럼 기록을 재생한다.
- **쓰기**: 기록 1건 = 트랜잭션 1건(`BEGIN IMMEDIATE`, 선별은 후보까지 한 번에). WAL, `busy_timeout` 30초 — 스케줄러 · API · CLI가 같은 DB에 동시에 써도 줄을 서서 들어간다. 실행 잠금(`execution_lock`)은 그대로 유지(읽고 판단하고 쓰는 구간 전체를 묶는 용도).
- **멱등은 적재에만**: 코드가 쓰는 기록은 바이트까지 같아도 매번 새 행이다(JSONL에 새 줄이 붙던 것과 같음 — 시각이 초 단위라 같은 초의 정지 → 재가동 → 정지가 같은 바이트일 수 있다). 재실행 중복은 지금처럼 기록의 키(선별 run id · 선별+종목별 결정 · `order_id` · `RunRecord`)가 막는다. 적재는 `source_hash`로 멱등(아래).
- **DB 위치**: 감사 루트(`data/audit`)의 부모 폴더 `trading.db` = `TRADING_DATA_DIR/trading.db`. 시장마다 따로가 아니라 하나.
- **적재 전 보호**: sqlite인데 DB 파일이 없고 감사 루트(루트 파일과 시장 폴더 전부 — 연 장부만이 아니다: 첫 장부를 읽는 순간 DB가 생겨 다음 장부의 보호가 꺼지므로)에 내용 있는 JSONL이 있으면 `AuditLog`가 `RuntimeError`("…migrate_jsonl_to_db.py로 먼저 적재") — 빈 장부로 시작해 주문이 겹치는 것을 막는다.
- **Postgres 이식**: 타입은 TEXT · INTEGER · REAL · BOOLEAN, 시각은 ISO-8601 텍스트. 다른 점은 트리거 문법(plpgsql 함수)과 `INTEGER PRIMARY KEY` → `BIGINT GENERATED ALWAYS AS IDENTITY`뿐(`store/schema.sql` 머리 주석).

**적재 · 백업 명령**

```text
.venv/Scripts/python scripts/migrate_jsonl_to_db.py --verify     # data/audit/** → data/trading.db, 재생 비교까지
.venv/Scripts/python scripts/migrate_jsonl_to_db.py --verify-only
.venv/Scripts/python scripts/export_db_to_jsonl.py               # → data/backup/jsonl-<UTC>/<시장>/<종류>.jsonl
.venv/Scripts/python scripts/export_db_to_jsonl.py --out D:/backup/audit
```

- 적재 순서: 종류마다 루트(시장 축 이전, 국내) → `audit/KRX/` → `audit/US/`, 파일 안은 줄 순서 — JSONL 저장소가 읽는 순서와 같아 시장별 재생 순서가 보존된다. 줄 원문이 그대로 `payload`. 같은 줄이 여러 번 있으면 k번째는 `sha256(줄 + "#k")`로 행이 따로 남고, 다시 적재해도 새 행 0(멱등). JSON이 아니거나 기록 검증에 실패하거나 다른 시장 기록이 섞인 줄이 하나라도 있으면 **아무것도 적재하지 않고** 줄 위치를 출력, 종료 코드 4. JSONL 파일은 건드리지 않는다(백업으로 남김). 적재 전체가 트랜잭션 하나이고, 첫 적재는 `trading.db.loading`에 다 넣은 뒤 `trading.db`로 이름을 바꾼다 — DB 파일이 있으면 적재가 끝난 것이라 위 보호가 그때까지 유지되고, 중간에 멈추면 DB가 생기지 않는다(다음 실행이 남은 `.loading`을 지우고 처음부터). **적재 전에 스케줄러 · 백엔드를 멈춘다**(적재 중 쓰인 기록은 옛 줄보다 id가 앞선다). `--verify-only`는 DB가 없으면 종료 코드 2.
- `--verify`: 시장마다 기록 · `latest_orders` · `trading_state` · `positions_view` · `paper_progress`를 두 저장소로 재생해 JSON으로 덤프하고 바이트 비교(다르면 종료 코드 5).
- 내보내기: 테이블 → `<out>/<시장>/<종류>.jsonl`, 행마다 payload 한 줄(id 순). 적재한 줄과 바이트까지 같다(국내는 루트 줄 + `KRX/` 줄이 한 파일로). `<out>`은 `TRADING_STORE=jsonl`의 감사 루트로 그대로 읽힌다. 이미 있는 폴더에는 쓰지 않는다(종료 코드 2). `data/trading.db*` · `data/backup/`은 gitignore.
- 저장소를 jsonl로 되돌리려면 export 결과를 감사 루트로 쓴다(sqlite 동안의 기록은 JSONL 파일에 없다). jsonl로 운영한 뒤 sqlite로 돌아오면 `trading.db`(`-wal` · `-shm` 포함)를 `data/backup/`으로 옮기고 `--verify`로 **처음부터** 적재한다(파일에 sqlite 기간 + jsonl 기간이 다 있다). 옛 DB 위에 다시 적재하면 export한 줄이 코드가 쓴 행과 겹치므로 적재가 그 줄들을 거부한다(종료 4, 아무것도 안 넣음).

### 조회 범위 (db-store-api, 2026-11-16 — `AuditLog` 읽기 도우미, 쓰기 경로 · 스키마 불변)

| 함수 | 거르는 열 | 백엔드에서 쓰는 곳 |
|---|---|---|
| `decisions_where(trade_date, ticker, source, rating)` | `trade_date`는 값 하나 또는 `(lo, hi)` 포함 범위(한쪽 None = 열린 끝) | `/api/decisions` · 예외 큐(그날 REVIEW) · 홈 흐름(최신 결정일) · `/api/tickers/{t}` · 모의 판정(시작일 이후) · 승인 존재 확인 · 보유로 처리의 중복 확인 |
| `decision_facts()` | — | 결정일 목록 · 종목 목록 · 저장 source별 수 · 총수(본문을 읽지 않음): `/api/decisions/dates` · `/api/health` · `/api/tickers` · `/api/performance`의 사람 처리 수 |
| `latest_orders_where(status, trade_date, ticker, order_id)` | 주문마다 **마지막 행**을 고른 뒤 거름(`latest_orders`와 같은 결과) | `/api/orders` · 예외 큐 · 결정 카드의 주문 |
| `order_facts()` | — | 주문 id 수 · 마지막 결정일 |
| `operations_where(kind, target, last)` · `runs_where(stage, trade_date, last)` | `last`: 기록 순서 마지막 n개 | `/api/runs` · 루프 상태 |
| `stamp()` | — | 위 캐시 무효화 |

jsonl 저장소는 전량을 읽은 뒤 같은 조건으로 거른다(결과 동일 — 두 모드 테스트 `tests/test_db_store_api.py`). sqlite는 인덱스 열 `WHERE`(`market` + 위 열). 장부 재생(`positions_view` · `book_events` · 모의 판정)은 지금처럼 그 시장 주문 전량을 쓴다(캐시).

**측정**(합성 1년 장부 — 국내 결정 2.8만 · 주문 행 14,940, 미국 같은 크기, SQLite 58.6MB, 가격 112종목 × 1만 행; 같은 데이터 사본을 develop 7db30d1 코드와 이 브랜치 코드로, TestClient, 다른 프로세스의 주문 append 직후 첫 호출 / 그 뒤 5회 중앙값, ms):

| 호출 | develop 첫 | develop 반복 | 이 브랜치 첫 | 이 브랜치 반복 |
|---|---:|---:|---:|---:|
| `/api/health` | 549 | 13 | 30 | 11 |
| `/api/decisions/dates` | 766 | 10 | 15 | 17 |
| `/api/decisions?date=` (112건) | 832 | 47 | 57 | 60 |
| `/api/decisions?ticker=` (250건) | 734 | 126 | 55 | 55 |
| `/api/exceptions` | 806 | 18 | 117 | 83 |
| `/api/orders?date=` | 635 | 11 | 35 | 36 |
| `/api/tickers` | 736 | 55 | 14 | 14 |
| `/api/tickers/{t}` | 1,108 | 201 | 70 | 78 |
| `/api/trading/mode` | 639 | 6 | 15 | 13 |
| `/api/positions` | 16,493 | 20,844 | 200 | — |
| `/api/markets` (두 시장) | 32,661 | 32,834 | 705 | — |
| `/api/paper/progress` | 19,943 | 18,190 | 497 | — |

장부 재생 세 줄은 저장소가 아니라 가격 조회가 원인이었다(`Prices.close_asof`가 호출마다 파일 stat 2회 + 전 행 마스크 — 재생 1회에 2.8만 번): 파일 확인을 종목당 1초에 한 번으로(`Prices.RECHECK_SECONDS`, ponytail), 종가는 정렬된 열에서 이진 탐색. 마지막 세 줄의 이 브랜치 값은 그 수정 뒤 반복 1회. 예외 큐 첫 호출 117ms는 주문이 append된 직후 최신 주문 캐시를 다시 만드는 비용(2만 9천 행)이다.

## 기록 종류

시장마다 장부 하나: sqlite는 `market` 열, JSONL은 폴더 `audit/KRX/`, `audit/US/`(아래 파일 이름은 JSONL 저장소의 것, 테이블은 위 표). 기록 모델(`DecisionRecord`·`OrderRecord`·`OperationRecord`·`RunRecord`)에 `market` 필드(`RunRecord`는 필수, 나머지는 기본 `"KRX"`)가 있고, `OperationRecord`에는 `scope`(정지·재가동의 요청 범위 `all`/`KRX`/`US`, 그 외 `null`)가 있다.

**호환 읽기(이전 기록):** 시장 축 이전의 `audit/*.jsonl`(루트)은 **국내 기록으로 읽고 옮기지 않는다**. 국내 장부는 루트 파일을 먼저, 그 뒤 `audit/KRX/`를 이어 읽고(`audit.market_audit`), 실행기·API가 쓰는 새 국내 기록은 `audit/KRX/`에만 append된다. 예외: `scripts/baseline_krx.py`(선별·결정)는 아직 루트 `audit/`에 쓰고, `scripts/build_mockup.py`는 루트만 읽는다(core 소유 스크립트, 후속). 읽기는 둘 다 이어 읽으므로 국내 장부는 섞이지 않는다. 루트 파일은 한 바이트도 바뀌지 않는다(옮기거나 다시 쓰면 append-only 감사 기록을 고치는 셈이라 택하지 않음). 미국 장부는 `audit/US/`만 읽는다.

**시장 검증:** `AuditLog(directory, market=…)`는 다른 시장의 기록을 쓰기·읽기 모두에서 거부한다(`ValueError` "…: US 기록이 KRX 장부에 들어올 수 없습니다"). 실행기는 이를 기록 파손으로 보고 종료 코드 4.

| 파일 (`audit/<시장>/`) | 모델 | 쓰는 쪽 |
|---|---|---|
| `screens.jsonl` | `ScreenRun` | 선별 |
| `decisions.jsonl` | `DecisionRecord` (+ `source`: `agent` 기본 / `rule` / `baseline`) | 에이전트(`agent`) · 모의 실행기의 규칙 청산(`rule`, 주문보다 먼저 기록) · 규칙 베이스라인(`baseline`). 베이스라인 기록은 지금까지 모두 `agent`로 저장됐고(실행기가 `agent`만 주문으로 내므로 `baseline_krx.py`도 아직 `agent`로 쓴다) 옮기지 않는다 — 읽는 쪽은 `audit.decision_source(d)`(`settings.decider == "rule_momentum_baseline"`이면 `baseline`), 라벨 `DECISION_SOURCE_KO["baseline"]` = "규칙 베이스라인". 사람 결정(`human`, 단계 4): 예외 처리 [보유로 처리]가 쓴다 — `rating="Hold"`, `action=null`, `thesis="사람 처리: 보유"`, `evidence[other]`(운영자: 사유, `ref`=order_id), 주문 없음, 채점 제외. 수동 등급은 Hold만(매수·매도 경로 없음). API는 저장값 `source`(agent/rule/human)와 함께 `source_kind`(`decision_source(d)`: agent/rule/baseline/human)·`source_kind_ko`를 싣고, `decisions_by_source` · `?source`는 `source_kind` 기준<br>LLM 결정(2026-11-14, `agent.py`, `--decider llm`): `source="agent"`, `settings` = `{decider: "claude-opus-5-5", effort, prompt_version, kind: candidate\|holding, seconds, raw}`(`raw`: `audit/<시장>/agent_raw/…json` 원문 경로), `usage` = `{input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens}`(모델이 안 만든 결정은 `{}`), 매도 검토는 `sell_reason`(`target_reached` · `thesis_broken` · `better_alternative` · `overweight` · `catalyst_gone` · `would_not_rebuy` · `hard_rule`, Hold면 `null` — 그 밖의 결정도 `null`). 호출 상한 · 키 없음 · 스냅샷 없음으로 규칙이 대신 정한 결정은 `settings.decider="rule_momentum_baseline"` + `fallback: "rule"` · `fallback_reason`. REVIEW는 `rating: "REVIEW"`, `thesis`에 사유(`refusal(…)` · `max_tokens(…)` · `schema: …` · `api: …`)와 원문 경로. 세 필드 모두 새로 생겨 이전 줄에는 없고(기본값으로 읽힘), API는 그대로 실어 보낸다 |
| `orders.jsonl` | `OrderRecord` (order_id·ticker·trade_date·side·quantity·filled_*·fill_date·status·exception_kind·reason·rules_applied) + 단계 4(decision-cards): `plan`·`exception_action`·`exception_resolved`·`reruns`·`retries`·`retry_on` | 모의 실행 `scripts/paper_execute.py`(`execution.py`) · 긴급 정지(미체결 취소) · 예외 처리(`POST /api/exceptions/{order_id}/action`). 같은 order_id의 마지막 줄이 현재 상태. 규칙 청산은 `paper-<date>-<ticker>-exit`, `rules_applied=[stop_loss/trailing/expired/force]`. 새 필드는 모두 기본값이 있어 이전 줄은 그대로 읽힌다(`plan` null · 처리 없음 · 0회) — 아래 "원안 vs 최종" · "예외 처리" |
| `operations.jsonl` | `OperationRecord` (kind halt/resume/mode_change/rule_change/exception_action/capital_set·at·operator·role·reason·target·before·after·market·scope) | 이 API의 쓰기 3종 · 실행기 자신의 정지·재가동(`operator=system`, `role=auto`). 정지·재가동의 `target`: `manual`(사람 — 이전 기록의 `running`도 manual로 읽음) · `daily_loss`(일일 손실) · `dd_stop`(−20% 드로다운). `effective_from`(system 일일 손실 자동 해제의 효력 거래일 — `at`은 실제로 쓴 시각 그대로). 실거래 전환 `mode_change`의 `before.paper_gate`(판정 스냅샷). 예외 처리 `exception_action`: `target`=order_id, `after{action, attempt}`(재실행 · 재시도만 회차), `market`=그 시장 |
| `runs.jsonl` | `RunRecord` (market·stage `decide`/`fill`·trade_date·started_at·ended_at·status `ok`/`skipped`/`failed`·reason·error·counts·notes) | 매일 루프 `scripts/daily_loop.py` — 단계 하나에 한 줄. `skipped`의 `reason`: `holiday`(벤치마크 봉 없음) 또는 실행기 거부 문구(정지 · 모드). `error`: 예외 종류와 메시지(자격 증명 없음). `counts`: refreshed · provisional · screened · decisions · decisions_new · fill_open_checked · fill_open_mismatch · `orders_<status>`. `notes`: 실패가 아닌 발견(decide가 전날 체결가를 그날 확정 시가 기준과 비교해 다르면 주문 id와 두 값). `ok`와 `holiday`는 그날 그 단계의 끝 — 다시 돌지 않는다. 아직 읽는 API 없음 |

## 읽기 엔드포인트

| 메서드·경로 | 화면 | 반환 |
|---|---|---|
| `GET /api/health?market` | 상태 바 · 다음 실행 칩 · 실패 띠 | ok, 코어 버전, `market`·`market_ko`(고정 "KRX" 없음)·`markets[]`, 그 시장의 기록 개수(screens·decisions·orders(주문 id 수)·memory_entries), `decisions_by_source{agent, rule, human}`(저장값 기준), 서버 시각. **가격 출처** `price_source`(kis / yahoo / null — 그 시장 지수 파일, 아래 "가격 출처")·`price_as_of`(지수 마지막 종가일)·`price_provisional`(그날 KIS 행이 잠정). **루프** `loop{status(ok / failed / never), status_ko, last{단계: RunRow\|null}(스케줄된 단계마다 마지막 기록), failed[RunRow](마지막 기록이 failed인 단계), next{at_kst, stage, stage_ko, label_ko}("11-17 09:05 KST · 체결" — `scheduler.plan`), retry_at_kst}`(그날 단계가 실패해 스케줄러가 다음 예정보다 먼저 다시 깨는 시각 — `scheduler.next_wake`, 아니면 null). 다음 실행 · 재시도는 스케줄러 함수를 그대로 부른다(백엔드 · 화면에 같은 계산이 따로 없음). **갱신** `refresh{at, source, symbols, last_final_day, failed_count, failed_symbols[≤10], failed_error, last_success_at}\|null`(`data/cache/_refresh_log.jsonl`의 그 시장 마지막 줄, `last_success_at`은 실패 0인 마지막 갱신) |
| `GET /api/runs?market&stage&limit` | 루프 기록 | 일일 루프 단계 기록(`RunRecord`, `run_records` 테이블) 최신순: `market, stage, stage_ko, trade_date, started_at, ended_at, status(ok/skipped/failed), status_ko, reason, error`(자격 증명 없음)`, counts, notes`. `limit` 기본 50(1~500 밖 **422**), `stage` decide · fill(그 외 422), 시장이 틀리면 400 |
| `GET /api/data/status` | 7 데이터 · 캐시 상태 | `cache{kis, yahoo: {files, kb}}`(`data/cache/*-KIS-data.csv` · `*-YFin-data.csv`), `refresh{KRX, US}`(위 `refresh`), `price_source{시장: {price_source, price_as_of, price_provisional}}`, `price_fallback{시장: {stage, trade_date, ended_at, notes[]}|null}`(그 시장 **마지막** 일일 루프 실행 기록의 가격 폴백 줄 — `notes` 중 "가격 Yahoo 폴백" · "가격 폴백 확인 실패"로 시작하는 것, core loop-prices; 없으면 null, 화면은 경고 한 줄), `vendors[{id(kis/yfinance/dart/anthropic), label_ko, use_ko, key_required, keys{환경 변수 이름: bool}, state(reachable/unreachable/no_key/unknown), state_ko, last_refresh_at}]`, `note`. **키는 있는지만**(환경 변수 또는 `TRADING_ENV_FILE`의 값이 비어 있지 않음) — 값은 어떤 응답에도 없다(테스트가 응답 · 헬스 · 시장 카드 · 설정 본문을 값으로 스캔해 0건). 벤더 도달은 **네트워크를 부르지 않고** 그 벤더의 마지막 갱신 기록으로 판단(심볼 수 > 실패 수면 도달 가능, 기록 없으면 "기록 없음", 필요한 키가 하나라도 없으면 "키 없음") |
| `GET /api/capital?market` | 7 실거래 자본 · 유니버스 | 아래 [자본](#자본-live-capital-2026-11-16) — 모의 · 실거래 자본, 출처 기록, 입출금 목록, 소액 1종목 모드 여부 |
| `GET /api/capital/preview?market&account=paper\|live&amount&universe=loop\|index\|custom&tickers` | 7 자본 미리보기 | 아래 [자본](#자본-live-capital-2026-11-16) — 이 자본에서 걸리는 규칙 · 금액, 살 수 있는 종목 수, 1주가 주문당 상한을 넘는 종목 |
| `GET /api/model?market` | 7 모델 설정 | `config.LLM` 읽기만, 결정자 · 시험은 `scripts/daily_loop`의 함수를 그대로 부른다: `decider`(rule/llm — `default_decider(그 시장 장부)`: `LLM.enabled`면 llm, 시험 중(`trial_on()` = `LLM.trial` 또는 `TRADING_LLM_TRIAL=1`)이면 모델 결정 거래일이 `trial_sessions`보다 적을 때 llm, 아니면 rule)·`decider_ko`·`decider_id`(llm이면 모델 id, 아니면 `rule_momentum_baseline`)·`model`·`effort`·`max_tokens`·`timeout`·`max_calls_per_run`·`enabled`·`trial_mode`·`trial`(시험 중이면 `{sessions, days(그 시장 모델 결정일), remaining}`, 아니면 null)·`config`(LLM 전체 — DART 재무 섹션 플래그 `fundamentals` 포함, 기본 false, core dart-fundamentals)·`keys{ANTHROPIC_API_KEY: bool}`(있는지만, 값 없음)·`output_language`·`note_ko`. 설정 화면의 하드코딩을 대신한다 |
| `GET /api/labels` | 공통 (시장 무관) | `rating_ko`, `rule_ko`, `rule_desc_ko`, `sell_reason_ko`, `mode_ko`, `order_status_ko`, `exception_kind_ko`, `exit_reason_ko`, `market_ko`, `halt_scope_ko`, `halt_target_ko`(정지 종류 manual · daily_loss · dd_stop → 한국어), `book_event_ko`(가드·잠금 이력 종류 → 가드 · 잠금 · Protections · 개입 · 피크 재설정), `markets{KRX, US: {market, label_ko, currency, currency_symbol, benchmark, benchmark_name_ko, timezone, session(현지), session_kst(오늘 기준, 서머타임 반영), name_style(name / name_ticker)}}`, `color_convention{up: red, down: blue, note}`(두 시장 공통) |
| `GET /api/markets` | 1 시장 요약 카드 | 시장마다 한 줄(합산 없음): 위 시장 라벨 + `as_of, equity, initial_cash, day_return, drawdown, positions`(보유 종목 수)`, state`(아래 운용 상태)`, flow`(홈 흐름 카드 4장: `screen{run_id, as_of, universe, universe_size, selected_count}`\|null 최신 선별 · `decisions{date, total, by_rating, by_rating_ko, rule}` 최신 결정일의 에이전트 결정 등급별 수(5단계 → REVIEW 순, 있는 것만 · `total`도 에이전트만)와 같은 날 규칙 청산 수 `rule` · `exceptions{date, total, by_kind, by_kind_ko}` `/api/exceptions` 기본값과 같은 날의 예외 큐 종류별 수 · `holdings{count, near_stop, protections_active}` — 서버가 센다, 화면은 합산하지 않음)`, next_run_kst`(ISO KST — 스케줄러의 다음 단계: 국내 개장 5분 뒤 fill · 마감 5분 뒤 decide, 미국 decide만, `scheduler.plan`)`, next_run_stage`(decide/fill)`, next_run_label_ko`("11-17 09:05 KST · 체결")`, price_source · price_as_of · price_provisional`(위 health와 같음)`, error`. 한 시장 장부가 파손되면 그 카드만 `error` 문구와 숫자·state·flow `null`이고 다른 카드는 정상이다. `market` 쿼리는 받지 않는다(두 시장 전용). 다음 실행은 거래소 세션만 본다(`scheduler.plan` → `market_calendar.is_session` — 휴장일 · 주말 건너뜀, 미국 조기 폐장일 decide는 13:05 ET; 그 해 달력 파일이 없으면 평일, 2026-11-16 holiday-calendar). `session_kst`는 그날 달력 시간(`market_calendar.session_open` · `session_close` — 미국 조기 폐장 · 수능일). **달력 파일이 깨지면**(JSON 오류 · 시장/연도 불일치 · 키 없음) 그 시장 카드만 `next_run_kst` · `next_run_stage` · `session_kst` null, `next_run_label_ko` "일정 계산 오류", `schedule_error` 문구(평일로 조용히 대신하지 않는다), 장부 숫자와 다른 시장 카드는 정상. 정상이면 `schedule_error` null |
| `GET /api/trading/mode?market` | 상태 바·설정 | 그 시장의 `market`·`market_ko`·`mode`(auto/manual/paper)·`mode_ko`·`running`·`halted_at`·`halted_by`·`halt_reason`·`keep_exits`·`halted_by_ko`(시스템 정지면 "시스템 자동 정지", 아니면 운영자 이름)·`halt_target`(`manual`/`daily_loss`/`dd_stop`, 실행 중이면 null)·`halt_target_ko`("운영자 긴급 정지" / "시스템 · 일일 손실 정지" / "시스템 · 드로다운 정지")·`dd_stop`(−20% 정지가 서 있음 — 사람이 그 위에 정지를 더 걸었어도 true, 재가동에서 피크 재설정을 고를 수 있음)·`label_ko`(실행 중이면 **두 축**을 함께: "국내 모의투자 · 자동 실행 중" / "국내 실거래 · 자동 실행 중" / "국내 실거래 · 수동 승인 중"(live-capital, 2026-11-16 — 이전 "국내 자동 매매 실행 중" · "국내 모의투자 모드"), 정지면 "미국 정지됨 · 시각 UTC · 정지 주체" — 시스템 정지면 주체 자리에 `halt_target_ko`)·`admin_token_required`·**두 축**(파생, 저장 형식 불변): `account_mode`(paper/live — mode가 paper면 paper, 아니면 live)·`account_mode_ko`(모의투자/실거래)·`execution_mode`(auto/manual — mode가 manual이면 manual, 아니면 auto)·`execution_mode_ko`(자동 실행/수동 승인)·`auto_resume_at`(system 일일 손실 정지가 풀리는 시각: 정지일 다음 **거래일** 00:00, 그 시장 시간대 ISO — 주말·휴장일은 건너뜀, 아니면 null)·`auto_resumed`(그 시각이 지나 실행 기록 전이라도 실행 중으로 읽힘)·`auto_resume_open_kst`(그날 정규장 개장 시각, KST ISO — 해제는 현지 00:00이지만 주문은 개장부터)·`auto_resume_label_ko`("07-28(화) 09:00 KST", 상단 배지 · 정지 띠 · 재가동 모달 문구, 일일 손실 정지가 아니면 null)·`halt_scope`(서 있는 마지막 정지를 보낸 범위 all/KRX/US, 실행 중이면 null)·`halt_scope_ko`(전체 시장 / 국내만 / 미국만). 한계: 다음 거래일은 지수 가격 캐시에서 찾는데 정지가 서 있는 동안 캐시는 보통 정지일에서 끝나므로 **다음 평일**이 된다(평일 휴장 미반영 — 휴장일 00:00부터 실행 중으로 읽힘. 다음 실행 칩은 휴장 달력을 반영하지만 이 자동 해제일은 아직 평일 기준(`execution.next_trading_day`, 후속). 주문 영향은 없다: 실행기는 휴장일에 돌지 않고, 해제 줄의 `effective_from`은 실행 때의 달력으로 정해짐) — 유효 상태는 `execution.effective_state`, 서 있는 정지가 전부 system `daily_loss`일 때만 |
| `GET /api/screens?from&to&universe&market` | 2-A | 선별 실행 요약 목록: `run_id, as_of, universe, universe_size, selected_count, rules, rules_ko, rule_count, stage_counts, recorded_at`. `from`·`to`(as_of 포함 범위)·`universe`(정확히 같은 이름)로 거른다 |
| `GET /api/screens/{run_id}?market` | 2-B | `ScreenRun` 전체 + 요약 필드. 후보마다 `name, failed_rules(_ko), filters[{rule, value, threshold, passed, note}], score, rank, selected` |
| `GET /api/decisions/dates?market` | 3 | 결정일 목록(내림차순) |
| `GET /api/decisions?date&rating&review_only&overrides_only&ticker&source&market` (`source=agent`면 에이전트 판단만 — 등급 필터 "매도"에 규칙 청산을 빼려면 함께 쓴다, 그 외 값은 422) | 1·3 | `DecisionRecord` + `source`(저장값 agent/rule/human)·`source_ko`·`source_kind`(배지: agent/rule/baseline/human — `?source`도 이 값, 그 외 422)·`source_kind_ko`("에이전트 결정"/"규칙 청산"/"규칙 베이스라인"/"사람 처리")·`name, rating_ko, tradeable, outcome{…}`(rule · human이면 null), `orders[]`(같은 ticker·trade_date의 주문 중 자기 것: rule 결정은 `-exit` 주문만, agent 결정은 그 외, human 결정은 없음 — 주문마다 `plan`), `has_intervention`(자기 주문 중 `plan.interventions`가 있는 것이 있음 — `overrides_only`가 이것으로 거른다, 하드룰 개입 탭) · (2026-11-16, sell-reasons) `review`: 매도 검토 결정이면 `{action Hold/Trim/Exit, verdict keep/trim/exit, verdict_ko, reasons[], reasons_ko[], trim_to_pct, met, total}`(`main._review_of` — 기록 문서를 `audit.read_thesis`로 되읽음, 새 LLM 호출 없음, 종목 상세 논지 점검과 같은 판정 매핑), 그 밖(메모 · 규칙 청산 · REVIEW · 사람 처리)은 null. 규칙 청산의 사유는 이 필드가 아니라 자기 `-exit` 주문의 `rules_applied`(청산 사유 배지) |
| `GET /api/orders?date&status&ticker&market` | 1 오늘 자동 실행 · 3 | 주문마다 현재 상태 한 줄: `OrderRecord`(+ `market`) + `name, side_ko, status_ko, amount`(그 시장 통화) + **체결 시각** `fill_at_local`·`fill_at_kst`(그 세션 개장 시각, 현지 · KST ISO — zoneinfo가 서머타임 반영, 뉴욕 09:30 = 여름 22:30 · 겨울 23:30 KST)·`fill_planned`(filled·partial은 `fill_date`의 개장 · false, open은 결정일 다음 거래일 개장 · true — 지수 캐시 밖이면 다음 평일, 휴장 미반영. blocked·rejected·cancelled·exception은 셋 다 null/false). 화면은 Intl로 서식만(국내는 KST만). status는 filled/partial/open/blocked/rejected/exception/cancelled. date 형식·status 값이 틀리면 **400** |
| `GET /api/exceptions?date&all_dates&market` | 1 예외 띠 · 3 예외 큐 · 사이드바 배지 | `[{kind(review/cap_exceeded/order_rejected/vendor_unavailable/data_delayed — luld_halt·pdt_blocked는 값·라벨만), kind_ko, ticker, name, trade_date, summary, detail, ref}]`. date 없으면 최신 결정일만. 주문의 kind는 `exception_kind`(없으면 규칙 id·사유에서 추론). REVIEW 결정의 예외 주문은 결정 항목과 같아 한 번만 나온다. 단계 4: 항목마다 `market`, `order_id`(REVIEW는 `paper-<date>-<ticker>`, 실행기가 아직 안 돌았으면 null), `actions[{action, label_ko, used, limit, available}]`(REVIEW: rerun · hold · dismiss / 주문 예외: retry_next · dismiss_order — `limit`은 재실행 · 재시도만 2), `pending_action`·`pending_action_ko`(예약된 재실행 · 재시도 — 큐에 남음), `reruns`, `retries`. 처리가 끝난 예외(`exception_resolved`)는 큐에서 빠진다 · (sell-reasons) 항목마다 `review`: 매도 검토의 주문 예외면 그 검토(같은 종목 · 결정일의 에이전트 결정), REVIEW 항목 · 규칙 청산(`-exit`) 주문은 null |
| `GET /api/reviews?market&from&to&ticker&action` | 3 매도 검토 · 결정 카드 배지 | (2026-11-16, sell-reasons) 에이전트 매도 검토 결정만(`_review_of`가 읽히는 것 — 규칙 청산 · 메모 제외), 결정 화면과 같은 순서(최신 결정일 먼저, 같은 날은 종목 내림차순): `trade_date, recorded_at, ticker, name, market, rating(_ko)` + review 필드 + `orders[{order_id, status(_ko), side, quantity, filled_quantity, fill_date, held_before(plan 원안 = 보유 전체), partial_quantity(plan 최종 < 원안인 부분 매도의 체결 수량, 아니면 0)}]`(그 결정의 주문, `-exit` 제외). `action`은 Hold/Trim/Exit 외 422, `from` · `to` 형식 오류 · from > to 422. 시장 분리 · 두 저장 모드 |
| `GET /api/tickers?market` | 4 선택 | `ticker, name, latest_selected, latest_rank` |
| `GET /api/tickers/{ticker}?market` | 4 | `name, market, screening[], decisions[], outcomes[], orders[], timeline[]` + (2026-11-16, ticker-detail — **네트워크 없음, 파일 · 기록만**) `sector`(`universe.sector_of`, 섹터 파일에 없으면 null) · `exchange`(국내는 접미사 .KS 유가증권 · .KQ 코스닥, 미국은 KIS 시세 갱신이 남긴 `data/cache/_kis_us_exchange.json` — 없으면 null) · `holding`(모의 장부 `positions_view`의 그 종목 행 + `as_of` · `partial_sales[{date, quantity, price, order_id}]` — 진입일 이후 부분 매도 체결(`plan.final`이 매도이고 원안보다 작은 것), 미보유면 null) · `thesis_checks[]`(기록된 투자 메모 · 매도 검토 문서를 `audit.read_thesis`로 되읽은 것, 결정일 · 기록 시각 오름차순 — **새 LLM 호출 없음**: 메모 `{kind: memo, thesis, invalidation[], price, target, downside, horizon, exit_plan, checks[{condition, met: null}], verdict: memo}`, 검토 `{kind: review, action Hold/Trim/Exit, trim_to_pct, reasons[] · reasons_ko[], checks[{condition, met, evidence}], reasoning, met, total}`, 판정 `verdict` = 검토의 action: Hold → keep "논지 유지" · Trim → trim "축소" · Exit → exit "청산"; 규칙 결정 · REVIEW는 문서가 메모 형식이 아니라 빠진다) · `earnings{date, date_to, source, fetched}`(미국만, `data/cache/_earnings_us.json` — `scripts/refresh_earnings.py`만 쓴다; **참고용, 결정 · 실행 · LLM 입력에 안 쓴다**) · `earnings_note_ko`(국내 "국내 실적 발표일 미제공", 캐시 없음, 출처에 예정일 없음, 조회일보다 앞선 날짜는 "지난 실적일", 갱신 실패면 실패일 · 오류 + 이전 값) · (sell-reasons) `earnings`에 `error` · `failed`(마지막 갱신 실패 — 404 등. `refresh_earnings.py`가 yfinance의 오류 숨김을 그 호출 동안 꺼서 "조회 실패"와 "예정일 없음"을 따로 기록, 화면은 "조회 실패" 태그) |
| `GET /api/performance?market&source&from&to` | 6 · 1 최근 정산 | 시장별 규칙 베이스라인 요약(`audit.baseline_summary_path`: 국내 `data/baseline_summary.json`, 미국 `data/audit/US/baseline_summary.json` — `scripts/baseline_krx.py --market US`)과 요약의 `log_path` 메모리 로그. 그 시장 요약이 없으면 **404**(`TRADING_MEMORY_LOG`는 국내 전용 덮어쓰기). `by_rating{…}, per_date[], reflections[{date, ticker, name, rating(_ko), raw_return, alpha, resolved, reflection}], holding, caveat`, `human_handled`(그 시장의 사람 처리 결정 수 — 채점 집계에는 들어가지 않는다: 채점 표는 베이스라인 메모리 로그이고 사람 처리는 거기 쓰이지 않음, 화면은 "사람 처리 (채점 제외)"로 따로 표시). **6-B 채점 막대**(2026-11-16, performance-charts): `market`, `rating_bars[{rating, rating_ko, count, mean_alpha, mean_raw, hit_rate}]` — 5등급 전부(Buy · Overweight · Hold · Underweight · Sell 순, 정산 0건이면 `count` 0 · 나머지 null), `backtest.summarize`와 같은 대상(정산됨 · 알파 있음 · REVIEW 제외)과 같은 식(적중 = 알파 × 방향 > 0, Hold는 null), `mean_raw`는 같은 보유 기간 실현 수익 평균. `settlement{failed, by_reason[{id(no_price/price_ended), label_ko, count, tickers[≤20]}], waiting}` — 대기(pending) 결정 중 청산일이 지났는데 결과가 없는 것 — 정산 기간은 채점 경로(`memory.settlement.fetch_returns`)와 같다: 진입 = 결정일 다음 첫 거래일 종가, 청산 = 그 뒤 `holding` 거래일째(결정일 뒤 holding + 1번째 거래일, 벤치마크 달력). 사유: 가격 파일 없음 / 시세가 청산일 전에 끊김(상장 폐지 · 거래 정지 등). 청산일이 아직 안 왔으면 가격 파일이 없어도, 청산일까지 시세가 있으면(다음 정산 실행이 가져감) `waiting`(실패 아님). 로그 값은 `backtest._alpha`와 같이 공백을 떼고 읽는다. `by_source{decisions{agent, rule, …}(사람 제외 저장 결정 수), human, scored_source("baseline"), scored(채점된 수), note_ko}` — 채점은 베이스라인 메모리 로그에서만 나온다(모의 루프 결정은 수만, 정산 경로 없음). 미국도 같은 경로(`?market=US`) |
| `GET /api/performance/loop?market&source&from&to` (`/api/performance`의 `loop`도 같다) | 6 모의 루프 채점 (LLM vs 규칙) | 일일 루프가 기록한 결정(선별 run id `daily_<시장>_`, `daily_loop.RUN_IDS`)의 5일 정산 · 채점. **정의는 `scripts/llm_trial_report.py`와 같다**(테스트가 같은 장부로 두 숫자를 대조): `memory.settlement.fetch_returns(ticker, 결정일, 5, benchmark=시장 벤치마크)` — 결정 다음 첫 종가 진입, 종목 자신의 5거래일 뒤 종가 청산(휴장일은 봉이 없어 건너뛴다), 벤치마크 대비 알파, 오늘 봉은 쓰지 않음, 가격은 **KIS 캐시만**(KIS 파일 없는 종목은 Yahoo로 대신하지 않고 대기). `fetch_returns`가 모듈의 `get_closes`를 읽으므로 호출 동안만 KIS 읽기로 바꿔 끼운다(잠금 한 개 — 스크립트와 같은 방식, core가 가격 소스 인자를 넣으면 둘 다 뺀다). `source`: `agent`(=`settings.decider`가 `LLM["model"]`인 결정) · `rule`(=**모델과 같은 (종목, 결정일)에 그날 선별로 규칙이 내린 판단** — `daily_loop.rule_record` 재계산, 두 열이 같은 날 · 같은 종목을 채점한다; 규칙이 직접 기록한 결정 · 모델 대신 규칙이 정한 `fallback: rule` 결정은 `rule_recorded{decisions, first, last, stand_ins}`로 수만) · `all`(기본), 그 밖이면 422. `from` · `to`: 결정일 범위(형식 오류 · from > to는 422). 응답 `columns{agent, rule}`마다 `decisions, ratings, review, stand_ins, first, last, settled, pending, pending_by_reason[{id: window(5거래일 안 지남)/no_price(종목 KIS 파일 없음)/no_bench(벤치마크 KIS 파일 없음)/price_ended(벤치마크는 5거래일이 지났는데 종목 봉이 먼저 끊김 — 상폐 · 정지), count}], settle_failed[], buy_settled, mean_raw, mean_alpha`(**매수 쪽 Buy · Overweight만** — 스크립트와 같음), `alpha_by_rating{등급: {n, mean_alpha}}`(스크립트와 같음 — REVIEW · Hold도 정산은 된다), `directional{n, mean_return, mean_alpha, hit_rate}`(이 화면의 것: 방향 있는 등급만, 매도 쪽 Underweight · Sell은 부호를 뒤집어 평균, 적중 = 방향 × 알파 > 0 — `backtest.summarize`와 같은 식, **REVIEW 미채점**), `hold{n, mean_alpha}`(참고). `agreement{compared, agree, rate, no_screen}`: 모델 결정마다 그 결정의 선별로 규칙 결정을 다시 계산(`daily_loop.rule_record`)해 등급이 같은지 — 스크립트의 `rule_compared` · `rule_agree`와 같다(`no_screen` = 선별 기록이 없어 비교 못 한 모델 결정, 규칙 열에도 없다). 재계산은 (선별, 종목, 날짜)별로 선별 기록 stamp까지 캐시. `trial{sessions, days, remaining}` | null(`/api/model`의 `trial`과 같은 함수). `human_excluded`(사람 처리 — D7, 채점 없음), `rule_exits_excluded`(규칙 청산 `source=rule` — 선별 결정이 아님, 수만). 베이스라인 채점(`rating_bars` 등 — 메모리 로그)과 섞지 않는다. 캐시: 시장마다 메모리 한 칸, (종목, 결정일)별 정산을 그 종목 · 벤치마크 가격 파일 stamp와 오늘 날짜로 보관 — 새 결정은 그 결정만, 가격 파일이 바뀐 종목은 그 종목만 다시 정산(테이블 · 파일 캐시 없음). 베이스라인 요약이 없어도 `/api/performance/loop`는 답한다(`/api/performance`는 요약 없으면 그대로 404 — 루프 채점은 그 뒤에 계산하고, 실패하면 `loop: {error}`로 남겨 베이스라인 채점은 그대로 답한다) |
| `GET /api/performance/series?market&from&to` | 6-A 모의 장부 추이 | 모의 장부를 시작일(`paper_start`)부터 가격 달력 마지막 날까지 하루씩 재생(`execution.book_series`): `rows[{date, equity, nav_pct, drawdown_pct, exposure_pct, cash_pct, baseline_pct}]`. `nav_pct` = 시간 가중 NAV(`unit_values` — 입출금은 성과가 아님) / 시작 자본 − 1, `drawdown_pct` = NAV / 시작 자본부터의 누적 최고 − 1(`paper_progress`와 같음, 피크 재설정 없음), `exposure_pct` = 보유 평가액 / 평가액, `cash_pct` = 1 − 노출, 첫 체결 전은 시작 자본 그대로. `baseline_pct` = 참고 변형(`rules_on_regime`, 2차 · 3차 실측 `*_equity.csv`)의 평가액 / 모의 시작일 직전 행 − 1, CSV가 닿지 않는 날은 null. 그 밖에 `start, as_of, from, to, initial_cash, currency, reference{variant, variant_ko, measurement_ko, covered_until, error}`(실측 결과 없으면 `error` 문구, 선은 전부 null), `cache`(hit/miss), `price_source · price_as_of · price_provisional`, `note_ko`. `from` · `to`는 보여 줄 범위만 자른다(% 기준은 시작일 그대로), 형식이 틀리거나 `from > to`면 **422**. 빈 장부는 `start` null · `rows` []. **캐시**: 시장마다 메모리 한 칸 — 키 = 주문 · 운영 기록 `stamp()` + 읽는 가격 파일(벤치마크 + 주문 종목)의 파일 · mtime(`Prices.stamp`) + 요약 · 변형 CSV mtime. 기록이 하나 붙거나 가격 파일이 다시 쓰이면 다음 요청이 다시 계산(상태 테이블 · 파일 캐시 없음). 실측(2026-11-16, 실제 장부 ~108거래일): 첫 호출 41~48ms, 캐시 적중 7ms |
| `GET /api/paper?market` | 6 포트폴리오 실측 탭 · 국내 2차 ↔ 미국 3차 비교 | `scripts/paper_backtest.py [--market US]`의 `summary.json`(국내 `results/paper/`, 미국 `results/paper_us/`) + `market, currency`: `benchmark{metrics, windows}`, `variants{rules_off, rules_on, rules_on_regime: {label_ko, metrics(total_return·cagr·max_drawdown·dd_peak/trough/recovery·worst_day·volatility…), windows, exits_by_reason(_ko), trades, win_rate, mean_exposure, event_counts, events_tail[40], trades_sample}}`, `criteria[]`(PAPER_TRADING), `caveats[]`. 없으면 404("…scripts/paper_backtest.py --market US를 먼저 실행") |
| `GET /api/paper/progress?market` | 8 모의투자 검증 · 사이드바 배지 · 실거래 전환 모달 | 그 시장의 모의투자 통과 판정(서버가 6개 기준을 전부 판정 — 화면은 다시 계산하지 않는다). 아래 "모의투자 통과 판정" 절. 기록이 없으면 404가 아니라 `started: false`(전 기준 `in_progress`, `eligible: false`). market이 틀리면 400 |
| `GET /api/settings/rules?market` | 7 | `screen_rules[]`(layer screen), `screen_params`, `risk_rules[]`(RiskConfig 19개, layer hard_rule, 두 시장 공통), `paper_criteria[]`, `auto_rules[]`(`config.auto_execution(market)`: 공통 auto_daily_loss_limit·kill_switch_reset·default_mode + 그 시장 금액 auto_daily_order_cap·auto_order_max — 금액 행은 `currency`, 그 외 `null`), `auto_by_market{KRX, US}`, `market`(그 시장 상수)·`market_code`·`markets`(두 시장 `config.MARKETS`), `mode`(그 시장 상태), `change_history[]`(**두 시장** 운영 기록 합침, 최신순 — 같은 초면 기록 순서 — + `kind_ko`·`market_ko`), `history_errors[]`(다른 시장 운영 기록을 못 읽으면 그 시장 행을 빼고 사유를 적음 — 고른 시장 자신의 파손은 500) |
| `GET /api/positions?as_of&market` | 1 KPI·리스크·Protections · 5 | 그 시장의 모의투자 장부(체결 기록을 캐시 종가로 재생, 저장 상태 없음): `market, currency, as_of, initial_cash`(KRX 1억 · US $100,000)`, day_return`(전일 평가 대비)`, cash, holdings_value, equity, peak, drawdown, gross_exposure, realized_pnl, realized_today, unrealized_pnl, regime_on, regime_gap`(지수 종가 ÷ `limits.regime_ma_days`일 단순평균 − 1, 창보다 데이터가 짧으면 null)`, peak_reset{at, to}|null`(마지막 −20% 정지 뒤 관리자 피크 재설정, 없으면 null — `peak`·`drawdown`이 이 값부터 계산됨)`, positions[{ticker, name, quantity, avg_price, entry_date, last_price, last_date, market_value, unrealized(_pct), weight, stop_price`(체결 평단 × (1 + stop_loss_pct))`, stop_gap`(종가 ÷ 손절선 − 1)`, near_stop`(stop_gap ≤ `limits.near_stop_pct` 2%)`, trailing_active, trailing_high, trailing_stop_price, holding_days, expiry_date, exit_signal{reason, note}|null}], protections[{id, label_ko, active, detail, until, tickers[], lookback_days, lookback_hits}]`(카드의 "lookback 내 트리거 n건": as_of까지 그 가드 창의 거래일 안 — StoplossGuard 손절 청산 수(10일), MaxDrawdown −20% 정지 기록 수(dd_stop_lock_days 20일), Cooldown 청산 수(5일), LowProfit 발동 수(30일))`, sector_exposure[{sector, pct, classified}]`(보유 비중 `weight`를 `universe.sector_of` — 섹터 파일 `data/universe/sectors.csv`, 네트워크 없음 — 로 묶은 것: 큰 순 3개 섹터, 그다음 섹터 파일에 없는 종목 합 "미분류" 한 줄(`classified: false`, 한도 없음). 보유 없으면 `[]`)`, price_source · price_as_of · price_provisional, price_fallback[]`(그 시장 출처와 다른 파일로 값을 매긴 보유 종목 — KIS 파일이 없어 Yahoo)`, protections_active`(발동 수, 상단바 칩)`, near_stop_count, max_position_weight, max_position_ticker`(가장 큰 보유, 없으면 0 · null)`, today_session, today_buy_amount, today_sell_amount, today_buy_cap_used`(아래)`, exits_by_reason(_ko)`(stop_loss·trailing·expired·force·exit_signal(등급 하향)별 건수)`, closed[{…, reason}], limits{…, auto_daily_order_cap, near_stop_pct, regime_ma_days, max_sector_pct}`(금액은 그 시장 통화, `max_sector_pct`는 실행기 사이징의 섹터 상한 0.20)`, note, mode`. **오늘 주문 금액**: `as_of`에 결정된 주문이 체결될 세션(`today_session`, 데이터에 아직 없으면 null)에 체결되는 모든 주문(결정일 무관 — 휴장일 결정은 앞 거래일과 세션을 공유)의 매수·매도 금액. 체결분은 체결가 × 수량, 미체결은 결정일 종가 × 수량. 일일 상한(`auto_daily_order_cap`)은 **매수만** 세므로 `today_buy_cap_used` = 매수 ÷ 상한(실행기의 상한 판정과 같은 함수 `execution.session_amounts`). `exit_signal`은 실행기가 그날 종가에 규칙 청산 주문으로 바꾸는 판정과 같은 함수(`execution.exit_signals`). as_of 기본값은 데이터의 마지막 거래일, 형식이 틀리면 **400**. 이전에는 **501**이었다(본문 `{implemented:false}`) · (sell-reasons) 보유 행마다 `last_review`: 그 종목의 as_of 이전 마지막 에이전트 매도 검토(`{trade_date} + review 필드`) 또는 null — `exit_signal`(룰 판정)과 따로 |
| `GET /api/events?market&limit` | 5 가드·잠금 이력 | `{market, total, counts{종류: 수}, events[{date, kind, kind_ko, ticker, name, text}]}` 최신순(`limit` 기본 50, 1~500 밖이면 **422**). 주문·운영 기록에서 모은다(`execution.book_events`, 저장 이력 없음): `guard` = system 정지(일일 손실 · −20%)와 드로다운이 처음 반감(−10%)에 들어간 날, `lock` = 잠금이 막은 신규 매수(전 종목 잠금 dd_stop · daily_loss_limit · regime · StoplossGuard는 결정일·잠금 묶음마다 한 줄, 종목 잠금 Cooldown · LowProfit은 주문마다), `protections` = 청산이 새로 건 잠금(청산마다 Cooldown, 발동 때 StoplossGuard · LowProfit), `intervention` = 상한·반감 스케일이 비중을 자른 매수(`max_position_pct`·`sector_cap`·`max_gross_pct`·`dd_halve`), `peak_reset` = 피크를 재설정한 관리자 재가동. 사람 정지는 넣지 않는다 |
| `GET /api/trading/resume-preview?market&reset_peak` | 재가동 모달 | 읽기 전용. `{market, variant(running/manual/daily_loss/dd_stop), variant_ko, can_resume, reset_peak, equity, as_of, peak_before, base, restops, lines[{id(peak/dd_halve/dd_stop), label_ko, level, amount, vs_equity}]}`. 변형: 실행 중 · 일일 손실(서 있는 정지가 전부 system 일일 손실 — `can_resume` false, 다음 개장 자동 해제를 기다림) · −20% 정지가 서 있음(사람이 그 위에 정지했어도) · 그 밖은 사람 정지. −20%일 때만 `lines`: `base` = `reset_peak`(기본 true)면 지금 평가액, false면 옛 피크, 금액 = `base × (1 + level)`, `vs_equity` = 평가액 ÷ 금액 − 1, `restops` = 옛 피크 기준으로 이미 −20% 이하(재가동 직후 다시 정지). 재설정 금액은 재가동이 쓰는 것과 같은 함수(`_peak_reset`). `reset_peak`가 bool이 아니면 **422**, 시장이 틀리면 400 |

## 쓰기 엔드포인트

| 메서드·경로 | 권한 | body | 동작 |
|---|---|---|---|
| `POST /api/trading/halt` | 운영자 (토큰 불필요 — 사람이 항상 멈출 수 있어야 함) | `{operator, reason, keep_exits=true, scope="all"}` (operator·reason 비면 422, scope가 all·KRX·US 외면 **400**) | **범위**: `all`(기본)이면 실행 중인 모든 시장에, 시장 이름이면 그 시장에만 정지 한 줄씩 — 같은 `at`·같은 사유, 기록의 `scope`에 요청 범위. 이미 정지된 시장은 `skipped`(`keep_exits` 포함 — true면 그 시장 청산은 계속 돈다)로 건너뛴다. 단, 청산이 도는 정지(`keep_exits=true`)에 `keep_exits=false`가 오면 건너뛰지 않고 정지 한 줄을 더 써서 청산까지 멈춘다(상태는 마지막 정지 줄을 따름). 건너뛸 시장만 남으면 **409**. **한 시장 기록이 파손돼도 다른 시장 정지는 막히지 않는다**: `operations.jsonl`을 못 읽는 시장에도 정지 줄을 append하고(그 시장 실행기는 어차피 그 파일을 못 읽어 exit 4로 멈춘다) 결과에 `error`를 싣는다. 시장마다: 신규 주문 중단, 정지 기록(취소할 order_id 목록 포함)을 쓴 뒤 미체결(open) 주문마다 `cancelled` 줄을 orders.jsonl에 append, 보유 포지션 유지. `keep_exits=true`면 미체결 **매도**는 남기고, 정지 중에도 실행기가 청산만 계속 돈다(미체결 매도 체결 + 그날 규칙 청산; 신규 매수·에이전트 결정 주문은 없음). `keep_exits=false`면 실행기가 거부(종료 코드 3). orders.jsonl이 파손돼 읽히지 않아도 정지는 기록되고 `open_orders_cancelled: null` + 사유 note. 반환 `{scope, scope_ko, results[{market, state, error, effects{new_orders, open_orders_cancelled, positions, exits_active, note}}], skipped[{market, reason, keep_exits}]}` |
| `POST /api/trading/resume` | 관리자 | `{operator, reason, scope="all", reset_peak=true}` (`reset_peak`가 bool이 아니면 422, scope가 틀리면 400) | 재가동(기록 `target=manual`). **−20% 정지가 서 있는 시장**에서 `reset_peak=true`(기본)면 그 시장의 마지막 종가 평가액을 새 드로다운 피크로 기록한다(`after{peak_reset_to, peak_reset_at(종가일), peak_before}`). `false`면 피크를 그대로 두며, 드로다운이 여전히 −20% 이하면 다음 실행에서 다시 정지한다. 다른 정지(사람·일일 손실)에서는 `reset_peak`가 아무것도 하지 않는다. 피크 재설정 값은 `resume-preview`와 같은 함수다. **일일 손실 정지(서 있는 정지가 전부 system `daily_loss`)인 시장은 재가동하지 않는다**(단계 4, D2 — 다음 개장 자동 해제를 기다림): 사람이 그 위에 정지를 더 걸었어도 해제 시각 전이면 같다(해제 시각 뒤에는 사람 정지가 남아 관리자 재가동으로 푼다). 범위 안의 그 시장은 `skipped`(`reason`에 해제 시각)로 남고, 범위 안에서 재가동된 시장이 하나도 없으면 **409** `detail{message, auto_resume_at, waiting[{market, auto_resume_at, auto_resume_label_ko, reason}]}`. 화면의 재가동 모달도 그 변형에서 버튼을 내지 않는다(`can_resume` false). `all`이면 정지된 모든 시장, 시장 이름이면 그 시장만(다른 시장은 그대로 정지). 실행 중인 시장은 `skipped`. 시장마다 정지 전에 나간 미체결이 남아 있으면(정지 때 orders.jsonl을 못 읽은 경우) 취소 기록(`keep_exits`면 매도는 유지). 범위 안 시장이 전부 실행 중이면 409. 토큰 설정 시 헤더 없거나 틀리면 **403**. 반환 `{scope, scope_ko, resumed[], skipped[{market, reason}], peak_resets{시장: {to, at, before}}, states{시장: 상태}}` |
| `POST /api/capital?market` | 관리자 | `{account: paper\|live, amount, operator, reason}` — 금액은 그 시장 통화, 0보다 크고 단위(국내 1원 · 미국 $0.01)에 맞고 상한(국내 1,000억원 · 미국 $1억 — 입력 오타 방지) 이하, 아니면 422. operator · reason 비면 422, market 틀리면 400 | 아래 [자본](#자본-live-capital-2026-11-16). 토큰 먼저(403). 운영 기록 `capital_set` 하나 |
| `POST /api/trading/mode` | 관리자 | `{operator, reason, market, mode \| account_mode, confirmed=false}` — `mode`(auto/manual/paper, 저장 형식 그대로)와 `account_mode`(paper/live, 계좌 축) 중 **정확히 하나**(둘 다 · 둘 다 없음 422). market 없으면 422, 틀리면 400 | **그 시장의** 모드 전환. 토큰을 먼저 본다(403). 같은 모드면 409. `account_mode: live`는 paper에서 auto로(실행 축은 저장 형식상 auto), `account_mode: paper`는 paper로. **모의 → 실거래 게이트**(paper에서 paper가 아닌 모드로 — `mode: auto`·`manual`로 요청해도 같다): `confirmed: true`가 없으면 **422**, 서버가 그 자리에서 `/api/paper/progress`를 다시 판정해 `eligible`이 아니면 **409**("국내 모의투자 통과 기준 미충족 — 실거래로 전환할 수 없습니다: 기준(상태 · 값); …", 기록은 쓰지 않음). 통과하면 운영 기록 `before{mode, account_mode: paper, paper_gate{as_of, start, weeks, eligible, summary_ko, criteria{id: {value, status}}}}`, `after{mode, account_mode: live}`. 다른 시장은 그대로. 실거래 → 모의(또는 실행 축만 바꾸기)는 게이트 없음. paper에서 나갈 때 미체결 모의 주문을 취소 기록(`after.open_orders_cancelled`). 반환 `{state, note}` — 저장 형식은 그대로라 실행기는 paper가 아닌 모드에서 주문하지 않는다(`ExecutionRefused`, 실주문 경로 없음): `note`가 그 사실을 적는다(paper로 돌릴 때는 null) |
| `POST /api/decisions/{ticker}/{date}/approve?market` | 운영자 | `{approver, note}` | **수동 승인 모드 전용.** 자동·모의 모드에서는 **409**("승인 단계가 없습니다"). 수동 모드에서는 실행 레이어 전까지 **501** |
| `POST /api/exceptions/{order_id}/action?market` | 관리자 (토큰 설정 시 없거나 틀리면 **403** — 운영자 역할 분리는 후속) | `{action, operator, reason}` (operator · reason 비면 422) | 예외 처리(D7). `action`: REVIEW 예외는 `rerun` · `hold` · `dismiss`, 주문 예외(상한 초과 · 거부 · 벤더 · 지연)는 `retry_next` · `dismiss_order` — 종류에 맞지 않으면 **422**. **모든 액션이 `OperationRecord(kind=exception_action, target=order_id)`를 먼저 쓴다.** `rerun`: 예약만(API는 LLM을 부르지 않는다), `reruns`+1, 큐에 "재실행 대기"로 남음 — 다음 `daily_loop decide`가 답한다(아래 "재실행 처리", `result_ko` "… 다음 일일 루프 decide에서 그날 결정자로 다시 판단하고 이 예외를 닫습니다 …"). 루프가 닫은 원 예외의 주문(`/api/decisions`의 `orders[]`, `/api/orders`)에는 `rerun_answer{ticker, trade_date, rating, rating_ko, attempt, same_day}` — 답한 결정(루프의 처리 표시 `after.decision`, 없으면 `settings.rerun_of`가 그 주문인 결정). 카드는 "재실행 n회째 답 · T 등급 →" 칩으로 그날 결정 화면(`/decisions?date=T`)에 연결한다. 그 밖의 주문은 `null`. `hold`: `DecisionRecord(source=human, rating=Hold, action=null, thesis="사람 처리: 보유")`를 쓰고(이미 있으면 다시 쓰지 않음) 주문 없이 닫는다(`exception_resolved`), 채점 제외. `dismiss` · `dismiss_order`: 주문 없이 닫는다(`exception_resolved`) — `dismiss_order`의 에이전트 결정은 채점에 그대로 남는다. `retry_next`: `retries`+1, 다음 실행기 실행(그 주문의 `placed_on` 다음 세션 이후)이 그날 잠금 · 사이징(가드 스케일 · 종목 비중 캡 · 섹터 캡) · 자동 실행 상한을 다시 거쳐 **처음 수량 이하로**(`min(처음 수량, 그날 목표 수량)` — 목표 수량은 그 주문의 **결정일 매수 등급 목록**을 분모로 한 균등 비중 × 그날 가드 스케일 · 캡 × 그날 평가액, 처음 수량이 0인 예외(종가 없음)는 목표 수량 그대로) 같은 order_id에 새 줄을 쓴다(`retry_on`, 다음 세션 시가 체결) — 다시 막히면 예외로 남는다. 짝지을 에이전트 주문이 없는 재시도(결정이 지워짐 등)는 처리 표시를 지운 줄로 예외 큐에 돌아온다. **규칙 청산(`-exit`) 주문은 `dismiss_order`만**(retry_next 422 — 실행기가 다음 종가에 신호를 다시 판정해 새 청산을 낸다). 재실행 · 재시도는 예외마다 **2회**, 3회째 **409**. 이미 닫힌 예외 409, 예외가 아닌 주문 409, 없는 주문 404, 시장이 틀리면 400. **멱등**: 마지막 처리와 action · operator · reason이 같으면 아무것도 쓰지 않고 그 결과를 돌려준다(`replayed: true`). 반환 `{order_id, market, action, action_ko, replayed, attempt, limit, result_ko, order, decision?}` |

정지·재가동·모드 변경과 모의 실행은 **시장마다** 같은 잠금(`audit/<시장>/execution.lock`)을 잡는다. 범위 정지·재가동은 범위 안 모든 시장의 잠금을 `config.MARKETS` 순서로 잡아 교착이 없고, 실행기는 자기 시장 잠금만 잡는다. 실행기가 상태(정지·모드)를 확인한 뒤 주문을 쓰는 사이에 정지·모드 변경이 끼어들 수 없다.

기본 운용 모드는 **모의투자(paper)** 다. 기획서 §8-1대로 같은 자동 루프를 모의로 먼저 돌리고, 관리자가 모드 변경으로 자동(auto)에 올린다. 코드상 기본값은 `config.AUTO_EXECUTION["default_mode"]`. 모드는 시장마다 따로다.

자동 실행 금액 한도는 시장 통화로 시장마다(`config.AUTO_EXECUTION["by_market"]`, 읽기는 `config.auto_execution(market)`): 국내 일일 1억원 · 주문당 3천만원, 미국 일일 $70,000 · 주문당 $20,000(Q3). 비교는 같은 통화 안에서만 한다.

**주문당 상한은 축소 주문**(D1, 단계 4): 매수 금액(기준가 × 수량)이 `auto_order_max`를 넘으면 예외로 세우지 않고 상한 안의 **최대 정수 주식 수**(`floor(상한 ÷ 기준가)`)로 줄여 그대로 낸다. 사유 · 개입 칩 문구 `auto_order_max: 9,845만 → 3,000만 (56주 → 17주)`(미국 `$99,900 → $20,000 (999주 → 200주)`), `rules_applied`에는 id `auto_order_max` 그대로. 줄인 수량이 0주면(한 주 가격이 상한보다 큼) `blocked`, `rules_applied=[…, auto_order_max, cap_zero]`. 일일 상한 `auto_daily_order_cap`(매수만)은 그대로 **예외**(`cap_exceeded`). 매도는 상한 대상이 아니다.

**원안 vs 최종**(`OrderRecord.plan`, 매수만 — 매도 · 이전 줄은 null): `{proposed, final, interventions[{rule, before, after, note}]}`. `proposed`·`final`은 `{side(Buy/Sell/Hold), weight_pct, qty, amount}` — 원안은 에이전트의 균등 비중(1/매수 수) 그대로, 최종은 하드 룰 · 축소 뒤(차단이면 Hold · 0주). `interventions`는 사유 문구에서: 상한 축소 · 비중 축소는 `before`/`after`, 잠금(쿨다운 · 동시 보유 · 정지 등)은 `before`·`after` null이고 `note`만. 화면은 2열 표(다른 칸 강조) · 개입 칩 · 잠금 문구로 그린다. 다음 세션 재시도 줄의 원안은 처음 수량이다.

404: 없는 run_id·ticker·결정. 숫자 형식: 수익률·알파는 소수(0.012 = 1.2%), 가격·금액은 그 시장 통화 단위 float(국내 원, 미국 달러). 주문 사유 문구의 금액도 그 통화("1,234원" / "$1,234.56").

### 재실행 처리 (2026-11-16, exception-rerun — `scripts/daily_loop.py` `answer_reruns`)

- **대기 판정**(`audit.pending_reruns`): 주문의 마지막 줄이 REVIEW 예외(`exception_kind=review`, status exception/rejected)이고 `exception_action=rerun`, `exception_resolved` 아님. `GET /api/exceptions`가 그 항목에 `pending_action=rerun`("재실행 대기")을 싣는 규칙(`backend/main.py` `_queue_state`)과 같다 — 백엔드는 이 함수를 쓰지 않고, 두 쪽 결과가 같음은 `tests/test_exception_rerun.py`가 API로 단언한다. hold · dismiss로 닫힌 예약은 대상이 아니다.
- **언제**: 그날 `decide`의 결정을 다 쓴 뒤, 실행기(`execute_date`) 전. 결정자는 그날 `--decider`(rule | llm).
- **결과**: 원 결정일 D의 예외를 처리일 T의 결정으로 다시 판단한다 — 새 `DecisionRecord`(trade_date T, `screen_run_id` = 그 종목을 올린 D의 선별, `settings.rerun_of` = 원 order_id · `attempt` = 회차, `source=agent`). T 결정이라 실행기가 그날 결정처럼 주문을 낸다(원 order_id `paper-D-…`에는 예외 줄이 이미 있어 실행기는 그 id로 다시 주문하지 않는다). 그날 결정이 이미 같은 종목을 판단했으면 **그 결정이 답**이고 두 번째 결정은 쓰지 않는다(같은 종목 예약이 둘이면 첫 예약의 답이 둘째의 답) — 단 그날 llm 실행이 그 종목을 규칙으로 대체한 결정(`settings.fallback`)은 답이 아니다(아래 미룸). 이어서 처리 표시 `OperationRecord(kind=exception_action, operator=daily_loop, role=auto, target=원 order_id, after{action: rerun, attempt, pending: false, decision{trade_date, rating, decider, same_day}})`, 마지막으로 원 주문에 닫는 줄(`exception_resolved`, 사유에 "재실행 n회째 → T 등급")을 붙인다 — 큐에서 빠진다. 고치는 것은 없다(새 줄 · 새 행만).
- **다시 REVIEW면**: T의 REVIEW 예외 줄(`paper-T-…`)을 실행기보다 먼저 쓴다 — `reruns`를 이어받아 API의 2회 상한이 계속 센다(실행기가 쓰면 0부터). 큐에는 T 항목으로 돌아온다.
- **미룸**(처리 표시 없음, 큐에 그대로, `RunRecord.notes`에 "재실행 미룸 …"): llm 결정자의 호출 상한(`LLM.max_calls_per_run` — 그날 결정이 쓴 호출 포함) 초과, 키 없음, 프롬프트 재료 실패. **규칙 결정으로 바꾸지 않는다**(사람이 에이전트를 다시 부른 것). 그날 llm 실행이 그 종목을 규칙으로 대체했으면(`fallback` — 지금 물으면 그날 결정이 둘이 된다), 결정일 당일(D = T)의 예약, 원 결정 · 선별이 없는 예약도 미룬다. 처리 표시는 `operator=daily_loop` · `role=auto` · `after.pending=false`로 알아본다(API의 operator는 자유 문자열이라 이름만으로는 사람 예약과 섞일 수 있다). 모델 응답이 다시 읽히지 않으면 REVIEW(위).
- **멱등**: 같은 날 `decide`는 `RunRecord`로 한 번만. 기록이 없어도 답은 (`rerun_of`, `attempt`)로 찾아 다시 묻지 않고, 처리 표시가 있는데 닫는 줄이 없으면(그 사이 중단) 닫는 줄만 쓴다. `RunRecord.counts`: `reruns_pending` · `reruns_done` · `reruns_deferred`(예약이 없으면 키 없음 — 이전과 같은 기록).

## 자본 (live-capital, 2026-11-16)

docs/10 D5. 자본은 **상태 테이블 없이** 운영 기록 `OperationRecord(kind="capital_set")`을 재생해 읽는다(`execution.capital_state` · `with_capital`). 기록: `target` = `paper` | `live`, `before{amount}`(그때 값 — 실거래 미설정이면 null), `after{account, amount, change, flow?}`, `effective_from` = 요청 시각의 **그 시장 현지 날짜**, `operator` · `role` · `reason`. 스키마 · 트리거 변경 없음(`kind` Literal에 값 하나 추가).

- **기록이 없으면 그대로**: `with_capital`이 설정 객체를 그대로 돌려준다 — 시작 자본은 `PAPER_BOOKS`(국내 1억원 · 미국 $100,000), 재생 결과는 이전과 바이트 단위로 같다(입출금이 없으면 계산 경로도 이전 그대로).
- **모의 자본 — 장부에 주문이 없으면 시작 자본 교체**(`change: start`): 처음부터 그 금액으로 센다. 이전 입출금도 지운다(시작 자본 교체가 마지막이면 그 뒤 입출금만 센다).
- **모의 자본 — 주문이 있으면 입출금**(`change: flow`, `flow` = 새 금액 − 지금 금액): `effective_from` 이후 **첫 거래일 종가**에 현금으로 들어간다(가격 데이터가 아직 그날까지 없으면 `GET /api/capital`의 `pending: true`, 그날 종가가 들어오면 장부에 반영). 출금이 그때 장부 현금(마지막 종가 현금 + 아직 반영 안 된 입출금 − **체결 대기 매수**(open이거나 마지막 종가 뒤 체결 — 입출금 종가보다 먼저 체결된다, 결정일 종가 × (1 + 수수료 + 슬리피지)))보다 크거나, 출금 뒤 평가액이 0 이하이면 **409**(기록 안 씀 — 평가액 0이면 단위 가치를 이어 셀 수 없다. 재생은 그런 기록이 있어도 단위를 바꾸지 않아 0으로 나누지 않는다).
- **입출금 규칙 — 시간 가중**: 입출금은 수익도 손실도 아니다. 그날의 일간 수익률은 `(종가 평가액 − 그날 입출금) / 전일 평가액 − 1`. 장부는 시작 자본 단위의 **단위 가치**(NAV)를 따로 센다: 입출금일마다 `단위 ×= 평가액 / (평가액 − 입출금)`, NAV = 평가액 × 시작 자본 / 단위. 드로다운 · 피크(`_peak` · `book_events`의 신규 비중 절반 · 정지), 모의 판정의 최대 드로다운 · 일일 손실 위반 · 기간 수익률은 모두 NAV로 센다 — 입금이 드로다운을 감추지 않고, 출금이 낙폭으로 보이지 않는다. 피크는 그날 돈으로 환산해 `peak`로 낸다(평가액 / 피크 − 1 = NAV 드로다운). 관리자 피크 재설정 값은 그 날의 단위로 환산.
  - 손계산(테스트 `test_a_deposit_while_running_is_neither_a_gain_nor_a_drawdown_by_hand`): 시작 10,000, D1 A 50주 @100, 종가 100 · 120 · 90 · 108 · 99, D3 종가에 +5,000. 평가액 10,000 · 11,000 · 14,500 · 15,400 · 14,950. 일간 수익률 0 · +10% · 9,500/11,000−1(−13.6%) · 15,400/14,500−1(+6.2%) · 14,950/15,400−1(−2.9%). NAV 10,000 · 11,000 · 9,500 · 10,090 · 9,795 → 기간 수익률 −2.05%(평가액으로 셌다면 +49.5%), 드로다운 9,795/11,000−1 = −10.96%(평가액 14,950/15,400−1 = −2.9%로 감춰지지 않음 — 신규 비중 절반 발동).
- **실거래 자본**(`change: live`): 저장 · 표시 · 미리보기만. 실주문 경로는 없다. `GET /api/paper/progress`의 `go_live.capital`이 이 값(`capital_source: live`), 없으면 모의 자본(`capital_source: paper`). 계좌 축 전환(`POST /api/trading/mode`)과 모의 판정 409는 그대로.
- **멱등**: 그 계좌의 마지막 설정과 금액 · 운영자 · 사유가 모두 같으면 아무것도 쓰지 않고 `replayed: true`. 금액만 같으면 409("이미 …입니다").
- **실행기**: `execute_date`가 잠금 안에서 `with_capital`로 그날의 자본을 읽는다 — 사이징(종목당 비중 · 섹터 상한은 그 자본 기준), 일일 손실 한도(전일 평가 대비 %), 드로다운. 주문당 · 일일 금액 상한(`AUTO_EXECUTION`)은 설정값 그대로라 자본이 더 작으면 자본이 먼저 걸린다.
- **소액 1종목 모드**(그날 자본 < `SMALL_ACCOUNT` — 국내 100만원 · 미국 $1,000): 보유 · 미체결 매수가 없을 때 그날 첫 매수 등급 1종목만 산다. 그 종목은 종목당 · 섹터 상한을 100%로 풀어 자본 전체로(1주라도 사게), 나머지는 `blocked` · `rules_applied: ["small_account"]` · 사유 "소액 1종목 모드: 자본 X < Y — …". 예외 재시도(`retry_next`)도 같다 — 다른 종목을 보유 · 매수 대기 중이거나 그날 매수 종목이 따로 있으면 막힌다. **하드 룰 충돌**: 종목당 10% · 섹터 20% 상한을 이 모드에서만 넘는다(docs/10 D5 결정대로 — 10%로는 100만원 미만에서 대부분 1주도 못 산다). 일일 손실 · 드로다운 · 손절 · 주문당 상한은 그대로.

`GET /api/capital?market` 반환: `market`·`market_ko`·`currency`, `paper{amount, start, source: record|default, set{at, operator, role, reason}|null, small_account}`, `live{amount|null, source: record|unset, set, small_account|null}`, `cash_flows[{date, amount, pending, at, operator, role, reason}]`, `next_paper_change`(start/flow — 지금 바꾸면 무엇이 되는지)·`next_paper_change_ko`, `small_threshold`, `max_amount`, `unit`, `account_mode`, `admin_token_required`, `note_ko`.

`POST /api/capital?market` 반환: `{replayed, change, change_ko, flow, effective_from, capital(위 GET), result_ko}`.

`GET /api/capital/preview` — 서버 계산(가격 캐시 · `universe/sectors.json` · 지수 구성 CSV만 읽음, 네트워크 없음, 기록 안 씀). `amount` 없으면 그 계좌의 지금 자본(실거래 미설정이면 422), 금액 검증은 POST와 같다(422). 지수 가격 캐시가 없으면 503. `universe`: `loop`(일일 루프의 고정 목록 `SETUPS[시장].universe`) · `index`(그날 지수 구성 — `kospi200_history.csv` · `sp500_history.csv`) · `custom`(`tickers` 쉼표 · 공백 구분, 600개까지, 형식 아니면 422). 반환: `amount`·`small_account`·`as_of`(지수 달력 마지막 날), `universe{label, size, priced, missing[≤20], missing_count, sector_ready, name_ready}`, `buyable`(1주 종가 ≤ min(종목당 상한, 주문당 상한))·`none_buyable`·`cheapest`, `over_order_cap[≤20]`(1주 종가 > `auto_order_max`, 비싼 순)·`over_order_cap_count`, `rules[{id, label_ko, setting_ko, amount, value_ko, note_ko, warn}]`(종목당 · 섹터당 상한, 살 수 있는 종목, 최대 보유, 주문당 · 일일 상한 — 설정값과 자본 중 작은 쪽, 일일 손실 한도, 드로다운 −10% · −20%, 손절 1건 손실), `note_ko`. 유니버스 선택은 미리보기만 — 루프의 선별 유니버스는 여전히 스크립트 고정 목록.

설정 화면의 규칙 레지스트리는 국내 · 미국 열로 나뉜다: `GET /api/settings/rules`가 `screen_params_by_market`(미국은 `screening.us_preset` — 가격 하한만 다름)과 `regime_index{시장: {benchmark, name_ko}}`를 더 낸다. `GET /api/markets` · `/api/positions` · `/api/paper/progress`는 `capital`(시작 자본 + 그날까지 입출금)과 `cash_flows`(positions · progress), `small_account`(positions)를 더 낸다.

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

- 예외 처리 권한은 관리자 토큰 하나 — 운영자 역할 분리는 후속.
- 규칙 변경(`rule_change`)은 `OperationRecord`에 자리만 있고 쓰기 API는 없다. 설정 화면의 [수정]은 그때 열린다.
- 에이전트 결정(`--decider llm`)의 `DecisionRecord.thesis`는 `render_investment_memo()` · `render_sell_review()` 출력(첫 줄 `**Rating**: …`). 구조화 메모가 화면에 필요하면 원문(`settings.raw`)에서 읽거나 `memo` 키를 추가한다(api 후속).
- 매도 검토(`SellReview`)는 `GET /api/reviews?date` 로 별도 서빙 예정. 사유 라벨은 `/api/labels`의 `sell_reason_ko`.
- 거래일 달력은 시장 지수 캐시 한 종목이다(국내 `^KS11`, 미국 `^GSPC` — 6-19 준틴스처럼 한 시장만 쉬는 날은 그 시장만 체결이 밀린다). 벤더 공백일은 휴장으로 취급돼 체결이 하루 밀린다(미해결 — `^KS11` 이상치 검증과 같이). 결정일이 달력 끝보다 뒤면 실행을 거부한다.
- 모의 실행은 API가 아니라 CLI(`scripts/paper_execute.py [--market KRX|US] --date D | --all`, 기본 KRX, 쓰기는 `audit/<시장>/`에만, 매도 비용은 시장 설정(국내 거래세 · 미국 SEC fee·TAF), 마지막 줄 "US · 주문 …", 종료 코드 0 완료 · 2 인자 오류 · 3 거부 · 4 기록 파손)로 돈다. `--all`은 결정일만이 아니라 가격 데이터 끝까지 **모든 거래일**을 돌고(규칙 청산은 매일 종가 판정), 끝에 미설명 주문 수를 출력한다. 쓰기 API는 기획서 §0-5의 세 가지로 유지.
- 일일 손실 자동 정지: 그날 장부 손익(전일 평가 대비)이 `AUTO_EXECUTION.auto_daily_loss_limit`(−2%) 이하면 실행기가 잠금 안에서 정지 기록(`operator=system, role=auto, target=daily_loss, after{keep_exits:true, date, day_return, equity, open_orders_cancelled}`)을 쓰고 미체결 매수를 취소한 뒤 그날은 청산만. 날짜당 한 번(관리자가 재가동한 뒤 같은 날을 다시 돌려도 다시 정지하지 않는다).
- **일일 손실 정지는 그날 세션까지**(2026-11-13 사용자 결정): 정지일보다 뒤인 **다음 거래일**의 첫 실행이 잠금 안에서 `resume`(`operator=system, role=auto, target=daily_loss, reason="일일 손실 정지 자동 해제(정지일 YYYY-MM-DD)", after{running:true, date, halted_on}`)을 먼저 쓰고 정상 진행한다. 한 번만(해제 뒤엔 실행 중이라 다시 쓰지 않음), 그 시장만, 휴장일 날짜 실행에서는 해제하지 않는다. 서 있는 정지가 **전부** system의 `daily_loss`일 때만 해제한다: 사람이 그 위에 정지를 걸었거나 −20% 정지가 함께 서 있으면 관리자 재가동을 기다린다. 관리자가 먼저 재가동해도 된다. 자동 정지 횟수(`PAPER_TRADING.max_daily_loss_breaches`)는 정지 기록 수로 그대로 센다. **해제 시점**(paper-gate, 2026-11-13): 해제 줄의 `at`은 실제로 쓴 시각이고 `effective_from`에 다음 거래일을 적는다. 실행기가 아직 돌지 않았어도 API의 유효 상태는 다음 거래일 00:00(그 시장 시간대)부터 실행 중이다(`auto_resume_at`·`auto_resumed`).
- **−20% 드로다운 정지**: 드로다운이 `RiskConfig.dd_stop`(−20%) 이하인 거래일 종가에 실행기가 전량 규칙 청산(`force`, 각 종목의 손절이 먼저면 그 사유)을 낸 뒤 `halt`(`operator=system, role=auto, target=dd_stop, after{keep_exits:true, date, equity, peak, open_orders_cancelled}`)를 쓰고 미체결 매수를 취소한다. 청산 매도는 정지 중에도 체결된다. 같은 날 일일 손실 정지가 먼저 쓰였어도 함께 쓰고, 이 정지가 서 있는 동안 다시 쓰지 않으며, 관리자 재가동(피크 재설정)이 그 날짜 이후에 이미 있으면 그 날짜를 다시 돌려도 쓰지 않는다. 실행기는 해제하지 않는다(관리자만).
- **드로다운 피크**: 마지막 피크 재설정(`peak_reset_at` ≤ 그날) 이후의 종가 평가액 최고치(재설정 값 포함), 재설정이 없으면 초기 자본부터. 신규 비중 절반(−10%) · 정지(−20%) · 사이징 스케일(`drawdown_scale`) · `/api/positions`의 `peak`·`drawdown`·MaxDrawdown 가드가 모두 이 피크 하나를 쓴다.
- **체결일 시가 없음**(실행기 `_fill`): 그 종목 캐시의 마지막 날이 지수 달력의 **체결일 직전 거래일**이면(한 세션만 늦음) `exception_kind=data_delayed`(시세가 아직 안 들어옴 — 사유 "체결일 D 시세가 캐시에 아직 없음 (마지막 D') — 데이터 지연"), 그보다 여러 거래일 앞에서 끊겼거나(장기 거래정지 · 상장폐지 직전), 캐시가 체결일 뒤까지 있는데 그날만 없거나, 파일이 없으면 `vendor_unavailable`(거래정지 또는 데이터 공백). 둘 다 주문하지 않는다. 미국 `luld_halt`(LULD 거래 정지) · `pdt_blocked`(PDT 제한)는 **미구현 — 실시간 데이터 레이어 필요**: 값과 라벨(`EXCEPTION_KIND_KO`)만 있고 실행기는 내지 않는다. 기존 기록(`exception_kind` 없음 · 이전 4종)은 그대로 읽힌다.
- 규칙 청산(손절·트레일링·만료·DD −20% 전량): 실행기가 그날 종가로 판정해 `source=rule` 결정(평가 근거 `evidence[portfolio]`)을 먼저 기록하고 `-exit` 매도 주문을 낸다. 같은 날 같은 종목의 에이전트 매도는 `already_selling`으로 차단. 매도는 자동 실행 상한 대상이 아니다. 백테스트와 다른 점: 둘 다 −20% 뒤 피크를 재설정하고 정상 비중으로 돌아간다(`RiskConfig.dd_stop_recovery="reset_peak"`, dd-stop-align 2026-11-13). 백테스트(`simulate.run_simulation`)는 `dd_stop_lock_days`(20거래일) 뒤 스스로, 실행기는 관리자 재가동 때(고정 잠금 없음). 섹터 상한(`max_sector_pct` 20%)은 실행기도 백테스트와 같은 `risk.size_targets(…, sector_of=universe.sector_of)`로 건다(db-store-api): 그날 매수 등급 목록 순서대로 선착순, 섹터 파일에 없는 종목은 상한 없음, 잘린 종목의 카드에만 `sector_cap` 개입(다른 종목 카드에 번지지 않음).
- **매도 수량**(2026-11-16, partial-sell — `execution._sell_size`): `Sell`(메모의 Sell · 매도 검토의 Exit)은 보유 전량. `Underweight`는 둘로 나뉜다. ① 매도 검토의 **Trim**(`agent.to_record`가 등급 Underweight · `position_sizing` "trim to X% of equity"로 기록 — `audit.trim_target`이 읽는다): 결정일 종가 평가액 기준 목표 비중까지 줄인다 — 매도 수량 = ⌊보유 − X% × 평가액 ÷ 종가⌋(남는 쪽이 목표보다 한 주 미만 많을 수 있음, 목표 아래로는 안 내려감). 목표가 현재 비중 이상이거나 줄일 수량이 1주 미만이면 주문 없음(`blocked`, `rules_applied=["trim_at_target"]`, 사유에 두 비중). 0%면 전량. 결정일 종가가 없으면 `vendor_unavailable` 예외. ② 메모의 **Underweight**(목표 비중 없음): 보유의 `AUTO_EXECUTION.underweight_sell_fraction`(0.5)을 내림 정수 주로, 1주 보유면 그 1주. 근거: Underweight는 "비중 축소"이지 청산이 아니다 — 전량 매도는 축소 판단을 청산으로 바꿔 수익률 · 회전율을 왜곡한다. 목표 비중이 없으니 단순한 규칙 하나(절반)로 두고 값은 설정 한 곳에. 사유 문구 "Trim 비중 18.2% → 10.0% (120주 → 66주) · 평가 120만 → 66만" / "Underweight 보유의 50% 매도 (999주 → 500주)". 두 경우 모두 `OrderRecord.plan`을 쓴다: `proposed` = 주문 전 보유 전체(수량 · 종가 금액 · 비중), `final` = 파는 몫(주문 없음이면 `Hold`), 개입(`interventions`)은 없음 — 하드룰이 아니므로 "하드룰 개입" 탭에 들지 않는다. 결정 카드는 `plan`으로 "부분 매도 499주 · 남은 500주"를 보인다. 전량 매도 · 규칙 청산 주문은 전과 같다(`plan` 없음, 바이트 동일). 남은 수량은 진입일 · 평단을 그대로 이어서 손절선(체결 평단 D3) · 트레일링 고점 · 보유일 · 만료가 바뀌지 않는다. 부분 매도 청산(`ClosedTrade.full=false`)은 Protections(Cooldown · StoplossGuard · LowProfit)에 넣지 않는다 — 잠금은 전량 청산에만. `/api/positions`의 보유 행 `partial_sold`(부분 매도로 줄인 수량), 청산 기록 `full`. 매도는 일일 주문 상한(D2, 매수만)에 세지 않는다.
- **가격 출처**(db-store-api): 실행기 · API의 `execution.Prices`는 기본 `source="kis"` — 종목마다 KIS 캐시(`<심볼>-KIS-data.csv`, `scripts/refresh_cache.py`, 잠정 행 포함 · `_provisional.json`)를 먼저, 없으면 Yahoo 파일(`-YFin-data.csv`). `Prices.info(t)`가 어느 파일인지 · 마지막 종가일 · 잠정 여부를 준다. **백테스트 경로는 그대로**(`simulate` · `paper_backtest.py`는 `Prices`를 쓰지 않고 `config.MARKETS[…]["data_source_*"]`=yahoo). 실데이터 국내 `--all` 재생(스크래치 사본, 결정 · 선별만 남기고 주문 · 규칙 결정 · 운영 기록을 다시 씀): 주문 58건 · 상태(차단 36 · 체결 22) · 차단 규칙별 수 · 규칙 청산 10건은 develop(Yahoo) · 이 브랜치(KIS) · 이 브랜치 Yahoo 강제 셋이 같고, **체결가가 다른 주문 16건**(그중 수량 다른 2건 — 체결가로 정한 수량이 KIS 원가격과 Yahoo 수정가 차이로 1~2주 다름). 섹터 상한은 이 재생에서 07-10 012330.KS(이미 보유 — `already_held`가 먼저 막음) 한 곳에서만 걸려 차단 수 변화 0.
