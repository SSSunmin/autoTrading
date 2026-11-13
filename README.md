# agent-trading-core

LLM 에이전트가 판단하는 자동매매 시스템을 만들 때 필요한 **판단 안전장치 + 판단 채점** 레이어.
[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) v0.7.1에서 그래프·CLI·LLM 프로바이더 의존성을 끊고 추출했다 (Apache-2.0, `NOTICE` 참고).

이 패키지는 **종목을 고르지도, 주문을 내지도 않는다.** 그 두 가지는 `docs/01-오픈소스-규칙-조사.md`의 설계안대로 별도 레이어(스크리너·하드 룰·실행)로 만들고, 이 패키지는 그 사이에서 에이전트의 판단이 틀리지 않게 하고 결과를 채점하는 역할만 맡는다.

## 모듈

| 모듈 | 역할 | 막아주는 사고 |
|---|---|---|
| `date_window` | 모델이 요청한 날짜를 실행 날짜 이하로 클램프, 미공개 데이터 보류 | 백테스트 룩어헤드 |
| `yahoo` | 종목별 캐시된 OHLCV를 실행 날짜까지만 서빙, 오래된 프레임 거부, 정산용 종가, 회사 프로필 | 1년 전 가격으로 지표 계산 |
| `snapshot` | LLM 없이 계산한 OHLCV·지표·최근 종가 표. "숫자는 여기서만 인용" | 가격·지지선 환각 |
| `identity` | 티커 → 회사명·업종·거래소를 결정론적으로 조회해 프롬프트에 주입 | 차트 모양 보고 다른 회사 분석 |
| `schemas` | `ResearchPlan` · `TraderProposal` · `PortfolioDecision` · **`InvestmentMemo`**(사업요약·논지·불/베어·밸류에이션·목표가/하락가·촉매·논지깨짐조건·등급·확신도·매도계획. Buy는 상승/하락 2:1 미만이면 거부) | 촉매·하락 시나리오 없이 사는 결정, "15%"를 가격으로 읽는 파싱 사고 |
| `rating` | 5단계 등급 파서. 읽을 수 없는 결정은 **REVIEW**, 절대 Hold로 뭉개지 않음 | 못 읽은 판단으로 주문 |
| `structured` | 구조화 출력 호출 + 실패 시 자유 텍스트 폴백 | 프로바이더별 차이로 파이프라인 중단 |
| `portfolio` | 현금·보유·평단을 에이전트에게 알려주는 블록. "미제공"과 "빈 계좌"를 구분 | 가득 찬 종목에 또 Buy |
| `memory` | 결정 로그 → 보유기간 경과 후 실현수익·벤치마크 대비 알파 정산 → LLM 반성 1단락 → 다음 판단에 주입 | 같은 실수 반복 |
| `backtest` | 임의의 `decide(ticker, date)`를 티커×날짜 격자로 돌려 등급별 알파·적중률 집계 | 감으로 "잘 되는 것 같다" |
| `audit` | 종목 선별 근거(규칙별 값·임계·통과)와 결정 근거(증거·하드룰 개입)를 JSONL로 기록, 깔때기 단계 수 | 대시보드에서 "왜 이 종목?"에 답 못함 |
| `screening` | **신규.** 규칙 8개를 순서대로 적용(가격·상장일·거래대금·변동성·고저폭·섹터 쿼터·성과 필터·모멘텀 top-k), MSCI식 리스크조정 모멘텀 z-score 랭킹, 모든 판정을 `FilterResult`로 | 근거 없는 종목 선택 |
| `schemas.SellReview` | **신규.** 보유 종목 매도 검토: 논지 깨짐 조건 체크 + 매도 사유 7종. 사유 없는 Exit·체크 충족 시 thesis_broken 누락·목표 없는 Trim은 거부 | "가격 떨어져서 판다" |
| `risk` | **신규.** 하드 룰: 손절 → 트레일링 → 기간 만료, 포트폴리오 DD −10% 반감 / −20% 정지, 일일 손실 한도, Protections 4종(StoplossGuard·MaxDrawdown·Cooldown·LowProfit), 200일선 국면 필터, 종목·섹터·총 노출 사이징. 전부 순수 함수, 발동 사유를 한글 문구로 반환 | 한 종목·한 날에 책이 무너짐 |
| `simulate` | **신규.** 책 전체를 날마다 돌리는 시뮬레이션(종가 결정·다음 시가 체결·KRX 비용). 지표는 최대 드로다운·회복·최악의 날. 하드 룰 끔/켬/국면 필터 비교 | "잘 되는 것 같다"를 드로다운 없이 말함 |
| `audit.OrderRecord` · `OperationRecord` · `trading_state` | **신규(기획서 v0.3).** 주문·체결 기록, 운영 기록(긴급 정지·재가동·모드 변경·규칙 변경), 기록을 재생해 만든 현재 운용 상태 | 상태와 감사 이력이 어긋남, 누가 왜 멈췄는지 모름 |

## 설치

```bash
cd D:\trading-core
uv venv --python 3.12 .venv
uv pip install --python .venv -e ".[dev]"
.venv\Scripts\python -m pytest -q
```

## 사용 흐름

```python
from agent_trading_core.config import default_config
from agent_trading_core.identity import instrument_context
from agent_trading_core.snapshot import build_verified_market_snapshot
from agent_trading_core.schemas import PortfolioDecision, render_pm_decision
from agent_trading_core.structured import bind_structured, invoke_structured
from agent_trading_core.rating import parse_rating, is_review
from agent_trading_core.memory import TradingMemoryLog, Reflector, settlement
from agent_trading_core.audit import AuditLog, DecisionRecord, EvidenceItem

cfg = default_config()                      # 경로·보유기간·벤치마크 맵. .KS→KOSPI, .KQ→KOSDAQ
memory = TradingMemoryLog(cfg)
audit = AuditLog("D:/trading-core/data/audit")

ticker, date = "005930.KS", "2026-11-12"

# 1. 에이전트에게 줄 접지 자료 (LLM 호출 없음)
context  = instrument_context(ticker, trade_date=date)        # 회사 정체 고정
snapshot = build_verified_market_snapshot(ticker, date, cache_dir=cfg["data_cache_dir"])
lessons  = memory.get_past_context(ticker, as_of=date)         # 과거 결정의 교훈 (시점 필터)

# 2. 에이전트 호출 — llm은 LangChain ChatModel 등 .invoke / .with_structured_output 을 가진 객체
structured = bind_structured(llm, PortfolioDecision, "PM")
decision = invoke_structured(structured, prompt_with(context, snapshot, lessons), "PM")
text   = render_pm_decision(decision) if decision else llm.invoke(prompt).content
rating = decision.rating.value if decision else parse_rating(text)   # 못 읽으면 "REVIEW"

# 3. 기록: 메모리 로그(정산용) + 감사 로그(대시보드용)
memory.store_decision(ticker, date, text, rating=rating)
audit.record_decision(DecisionRecord(ticker=ticker, trade_date=date, rating=rating, thesis=text,
                                     evidence=[EvidenceItem(source="market", summary="...", as_of=date)]))

# 4. REVIEW는 거래 금지. 하드 룰(손절·한도)은 여기서 코드로 적용한다.
if is_review(rating):
    ...  # 사람 확인 큐로

# 5. 다음 실행 때(또는 스케줄러에서): 보유기간이 지난 결정을 정산하고 교훈을 쓴다
settlement.settle_all_pending(memory, Reflector(llm), cfg)
```

백테스트는 그래프가 아니라 함수 하나를 받는다:

```python
from agent_trading_core.backtest import Decision, iter_grid, run_backtest, summarize

def decide(ticker, date) -> Decision:
    ...                                   # 위 1~2단계. 규칙 기반이어도 됨
    return Decision(text, rating)

result = run_backtest(["005930.KS", "000660.KS"], iter_grid("2026-06-01", "2026-10-01", 7),
                      decide, cfg, reflector=Reflector(llm))
print(summarize(result).render())        # 등급별 n, 방향 적중률, 평균 알파
```

## 업스트림과 달라진 점

- 전역 `get_config()`/contextvar 제거. `cache_dir`, `config` dict를 인자로 넘긴다.
- `yahoo.py` 하나로 common/ohlcv/market/fundamentals 중 필요한 함수만 합쳤다.
- `backtest.run_backtest`는 `TradingAgentsGraph` 대신 `decide(ticker, date) -> Decision` 콜러블을 받는다.
- `identity.language_instruction(language)`는 인자를 받는다.
- `audit.py`는 신규. 업스트림에 없는 선별·결정 감사 기록.
- LangChain·LangGraph 의존성 없음. `structured`/`reflection`은 `.invoke()`·`.with_structured_output()` 덕 타이핑만 쓴다.

## 검증 (2026-11-13)

- `pytest`: 126 passed. 업스트림 순수 모듈 테스트 7개 파일 이식 + 백테스트·감사·투자메모·매도검토·선별·API(긴급 정지·재가동·모드·예외 큐·관리자 토큰) 테스트 신규.
- 실측 1차(`docs/03`): KOSPI 대형주 20종목 × 8주, 규칙 전용 베이스라인 160셀 전부 결정·정산, 실패 0. 결과는 통계적 결론 없음(등급당 n=12), 파이프라인 검증 완료.
- 실측 2차(`docs/05`): KOSPI 대형주 40종목 × 2024-01~2026-11 포트폴리오. 최대 드로다운 KOSPI −44.9% / 하드 룰 끔 −42.7% / 켬 −19.3% / 켬+국면 −15.6%. 손절은 갭을 못 막고 종목 비중 상한이 손실을 가둠.
- 실데이터 스모크: `005930.KS` 2026-11-12 스냅샷(종가 208,000, RSI 43.1), 정체 "Samsung Electronics Co., Ltd. / Technology / KSC", 2026-10-20 결정의 5거래일 수익 +8.3%, KOSPI 대비 알파 +3.5%, 정산일 2026-10-28.

## 서비스 실행 (백엔드 + 프론트)

```bash
# 1) 데이터 생성 (최초 1회, 네트워크) — 선별·결정·정산 기록
.venv\Scripts\python -I scripts\baseline_krx.py

# 2) 백엔드 API (FastAPI, http://127.0.0.1:8000/docs)
uv pip install --python .venv -e ".[api]"
.venv\Scripts\python -m uvicorn backend.main:app --reload --port 8000

# 3) 프론트 (Vite + React 19 + @bv-ds/ui, http://localhost:5173, /api는 8000으로 프록시)
cd frontend && npm install && npm run dev
```

- `backend/main.py`: 선별·결정·주문·정산 기록을 그대로 서빙하고 한글 라벨을 붙인다. 쓰기는 기획서 v0.3의 세 가지만: 긴급 정지(운영자) · 재가동 · 운용 모드 변경(관리자, `TRADING_ADMIN_TOKEN` 설정 시 `X-Admin-Token` 필수). 운영 기록은 `data/audit/operations.jsonl`에 append-only로 남고 현재 상태는 그 재생값이다. 기본 모드는 모의투자. 계약은 `docs/04-백엔드-API.md`.
- `frontend/`: 기획서 v0.3 7화면. 상단에 운용 모드 배지와 긴급 정지 버튼(확인 모달), 결정·실행 메뉴에 예외 건수 배지. `src/api`(클라이언트·타입) / `src/hooks` / `src/components`(등급 배지·종목 셀·실행 띠·결정 카드·긴급 정지) / `src/pages` / `src/app.css`(레이아웃, 토큰만 사용). 디자인 시스템 규칙은 `@bv-ds` agent-guide를 따른다. 디자이너 시안(`mockup/canvas/`)이 확정되면 `pages/` 내부를 교체한다.
- 종목명·근거 문구는 한글이다 (`agent_trading_core/krx_names.py`, 선별 note, 베이스라인 결정 텍스트).

## 스크립트와 시안

- `scripts/paper_backtest.py`: 2차 실측. 하드 룰 끔/켬/국면 필터 3개 변형 → `data/results/paper/summary.json`.
- `scripts/baseline_krx.py`: 규칙 전용 베이스라인 백테스트. 선별 → 등급 → 메모리 로그 → 정산 → 요약, 감사 로그 기록.
- `scripts/build_mockup.py`: 감사 로그·메모리 로그·요약을 `mockup/dashboard.template.html`에 주입해 `mockup/dashboard.html` 생성.
- `mockup/`: 초기 HTML 시안(아티팩트). 디자인 시안은 디자이너 세션이 별도로 작업 중이며, 실제 화면은 `frontend/`다.

## 다음 단계

- `docs/01-오픈소스-규칙-조사.md`: 종목 선별·매수매도 규칙을 어디서 빌릴지 (freqtrade, Lean, qlib, ai-hedge-fund 코드 근거)
- `docs/02-대시보드-화면-기획서.md`: 선별 근거·결정 근거를 보여주는 대시보드 화면 기획
- 국내 데이터: 재무는 DART, 뉴스·커뮤니티는 국내 소스로 교체. 가격·정산은 yfinance `.KS/.KQ` 그대로 사용 가능.
