# data/external — 출처

모두 2026-11-13에 받았다. 내용을 고치지 않은 원본 그대로다. 변환은 `scripts/build_universe.py`(S&P 500)와 `agent_trading_core/krx_names.py`(`load_listed_names`)가 맡는다.

| 파일 | 출처 URL | 판(revision) | 바이트 | sha256 앞 16자 |
|---|---|---|---|---|
| `sp500_wikipedia_oldid1192789360_2023-12-31.html` | https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=1192789360 | 1192789360 (2023-12-31, 2024-01-02 전 마지막 편집) | 1317236 | d9265244b6bda6d1 |
| `sp500_historical_components_2026-11-13.html` | https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500 | 1383679373 | 1138707 | 0ad91400531ed95f |
| `sp500_wikipedia_2026-11-13.html` | https://en.wikipedia.org/wiki/List_of_S%26P_500_companies | 1384572480 (2026-11-08) | 571692 | a9211798fb5fd710 |
| `kind_corplist_2026-11-13.xls` | https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13 | — (받은 날 기준 상장법인 목록, cp949 HTML 표) | 1248266 | e76a9392ea76b66d |

- S&P 500 시작 구성 = 1192789360판의 `constituents` 표. 변경 = Historical components 문서의 `changes` 표(2026-08-11에 본문 문서에서 이 문서로 옮겨졌다). 현재판(1384572480)은 재생 결과 대조용으로만 쓴다.
- KIND 목록은 한글명 표시에만 쓴다(현재 상장 종목만 있다. 상장폐지 종목 이름은 없다).
- KRX 정보데이터시스템(data.krx.co.kr)의 KOSPI200 구성종목 · KOSPI 일별 시세는 로그인이 필요해 받지 못했다. 사용자 결정(2026-11-13)으로 수동 CSV는 보류 상태다.
