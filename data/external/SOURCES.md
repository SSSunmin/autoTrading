# data/external — 출처

모두 2026-11-13에 받았다. 내용을 고치지 않은 원본 그대로다. 변환은 `scripts/build_universe.py`(S&P 500), `scripts/ingest_krx_constituents.py`(KOSPI200 · KOSPI 지수), `agent_trading_core/krx_names.py`(`load_listed_names` · `load_history_names`)가 맡는다.

| 파일 | 출처 URL | 판(revision) | 바이트 | sha256 앞 16자 |
|---|---|---|---|---|
| `sp500_wikipedia_oldid1192789360_2023-12-31.html` | https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=1192789360 | 1192789360 (2023-12-31, 2024-01-02 전 마지막 편집) | 1317236 | d9265244b6bda6d1 |
| `sp500_historical_components_2026-11-13.html` | https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500 | 1383679373 | 1138707 | 0ad91400531ed95f |
| `sp500_wikipedia_2026-11-13.html` | https://en.wikipedia.org/wiki/List_of_S%26P_500_companies | 1384572480 (2026-11-08) | 571692 | a9211798fb5fd710 |
| `kind_corplist_2026-11-13.xls` | https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13 | — (받은 날 기준 상장법인 목록, cp949 HTML 표) | 1248266 | e76a9392ea76b66d |
| `kospi200_20240102.csv` | KRX 정보데이터시스템 [11006] 지수구성종목, KOSPI 200, 기준일 2024-01-02 (사용자 로그인 수동 다운로드) | — (cp949 CSV, 200행) | 11218 | 4f7477f7cdd46ff2 |
| `kospi200_20240614.csv` | 〃 기준일 2024-06-14 | — (200행) | 11190 | de94484944c9cb92 |
| `kospi200_20241213.csv` | 〃 기준일 2024-12-13 | — (200행) | 11115 | bd29b7b351f44bd0 |
| `kospi200_20250613.csv` | 〃 기준일 2025-06-13 | — (200행) | 11384 | 4f4693221c2fe20e |
| `kospi200_20251212.csv` | 〃 기준일 2025-12-12 | — (200행) | 11192 | 4f0cf9df280c99f7 |
| `kospi200_20260612.csv` | 〃 기준일 2026-06-12 | — (200행) | 11342 | f3b4d41ac4b595fa |
| `kospi_daily_2024-2025.csv` | KRX 정보데이터시스템 [11003] 개별지수 시세 추이, KOSPI, 2024-01-02 ~ 2025-12-30 (사용자 수동 다운로드) | — (cp949 CSV, 486행, 내림차순) | 52928 | 94dc0b42b3c3752a |
| `kospi_daily_2026.csv` | 〃 2026-01-02 ~ 2026-11-12 | — (cp949 CSV, 211행, 내림차순) | 23329 | a896f75fc7db149d |

- S&P 500 시작 구성 = 1192789360판의 `constituents` 표. 변경 = Historical components 문서의 `changes` 표(2026-08-11에 본문 문서에서 이 문서로 옮겨졌다). 현재판(1384572480)은 재생 결과 대조용으로만 쓴다.
- KIND 목록은 한글명 표시에만 쓴다(현재 상장 종목만 있다. 상장폐지 종목 이름은 없다).
- KRX 정보데이터시스템(data.krx.co.kr)의 KOSPI200 구성종목 · KOSPI 일별 시세는 로그인이 필요해 사용자가 2026-11-13에 수동으로 받았다(사용자 결정: 수동 CSV 1회). [11006]은 기준일 하나씩 6장, [11003]은 한 번에 2년까지만 받을 수 있어 2장으로 나눴다(두 파일은 겹치는 날이 없다).
- 만든 파일(원본은 고치지 않음): `data/universe/kospi200_history.csv`, 이 폴더의 `kospi_official_daily.csv`(두 지수 파일을 일자 오름차순으로 합친 utf-8, 열 Date · Open · High · Low · Close · Volume(천 주) · TradingValue(백만 원) · MarketCap(백만 원)), `ks11_check.csv`(yfinance `^KS11` 종가와 KRX 종가 날짜별 대조, diff_pct = yfinance/KRX − 1, %).
