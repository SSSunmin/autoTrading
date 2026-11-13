"""Korean display names for KRX tickers, and Korean labels for rule ids and ratings.

yfinance serves English company names. A Korean reader expects 삼성전자, not
"Samsung Electronics Co., Ltd.", so the UI and the audit records use this table.

ponytail: a hand-kept dict for the baseline universe. For the full KOSPI200 load
the KRX listing (KIND 상장법인목록, or pykrx) into ``NAMES_KO`` at start-up.
"""

NAMES_KO: dict[str, str] = {
    "005930.KS": "삼성전자", "000660.KS": "SK하이닉스", "373220.KS": "LG에너지솔루션",
    "207940.KS": "삼성바이오로직스", "005380.KS": "현대차", "000270.KS": "기아",
    "068270.KS": "셀트리온", "105560.KS": "KB금융", "055550.KS": "신한지주",
    "035420.KS": "NAVER", "012330.KS": "현대모비스", "028260.KS": "삼성물산",
    "006400.KS": "삼성SDI", "051910.KS": "LG화학", "003670.KS": "포스코퓨처엠",
    "035720.KS": "카카오", "032830.KS": "삼성생명", "066570.KS": "LG전자",
    "015760.KS": "한국전력", "034730.KS": "SK",
    "^KS11": "KOSPI", "^KQ11": "KOSDAQ",
    # US book (docs/07): Korean HTS spellings
    "AAPL": "애플", "MSFT": "마이크로소프트", "NVDA": "엔비디아", "AVGO": "브로드컴", "ORCL": "오라클",
    "CRM": "세일즈포스", "GOOGL": "알파벳 A", "META": "메타 플랫폼스", "NFLX": "넷플릭스", "DIS": "월트 디즈니",
    "AMZN": "아마존닷컴", "TSLA": "테슬라", "HD": "홈디포", "MCD": "맥도날드", "WMT": "월마트",
    "COST": "코스트코 홀세일", "PG": "프록터 앤드 갬블", "KO": "코카콜라", "LLY": "일라이 릴리", "JNJ": "존슨앤드존슨",
    "UNH": "유나이티드헬스 그룹", "ABBV": "애브비", "BRK-B": "버크셔 해서웨이 B", "JPM": "JP모건 체이스", "V": "비자",
    "MA": "마스터카드", "BAC": "뱅크 오브 아메리카", "GE": "GE 에어로스페이스", "CAT": "캐터필러", "RTX": "RTX",
    "UNP": "유니온 퍼시픽", "XOM": "엑슨모빌", "CVX": "셰브론", "COP": "코노코필립스", "NEE": "넥스트에라 에너지",
    "SO": "서던", "PLD": "프로로지스", "AMT": "아메리칸 타워", "LIN": "린데", "SHW": "셔윈 윌리엄스",
    "^GSPC": "S&P 500",
}

RULE_KO: dict[str, str] = {
    "price_min": "가격 하한", "listed_days_min": "상장일수", "dollar_volume_top_n": "거래대금 상위",
    "volatility_band": "변동성 밴드", "range_stability_min": "60일 고저폭", "sector_quota": "섹터 쿼터",
    "performance_filter": "성과 필터", "momentum_top_k": "모멘텀 상위", "data": "데이터",
}

RULE_DESC_KO: dict[str, str] = {
    "price_min": "종가 ≥ 임계(원)", "listed_days_min": "상장 후 일수 ≥ 임계",
    "dollar_volume_top_n": "20일 평균 거래대금 순위 ≤ N", "volatility_band": "20일 변동성(연율)이 밴드 안",
    "range_stability_min": "60일 (고−저)/저 ≥ 임계", "sector_quota": "섹터 내 모멘텀 순위 ≤ 상한",
    "performance_filter": "최근 정산 알파 평균 ≥ 임계 (기록 없으면 통과)",
    "momentum_top_k": "MSCI식 리스크조정 모멘텀 z-score 순위 ≤ K",
}

# Rating values stay English in data (the parser reads them); the UI shows these.
RATING_KO: dict[str, str] = {
    "Buy": "매수", "Overweight": "비중확대", "Hold": "보유", "Underweight": "비중축소", "Sell": "매도",
    "REVIEW": "검토 필요",
}

SELL_REASON_KO: dict[str, str] = {
    "target_reached": "목표가 도달", "thesis_broken": "논지 깨짐", "better_alternative": "더 좋은 대안",
    "overweight": "비중 초과", "catalyst_gone": "촉매 소멸", "would_not_rebuy": "재매수 테스트 실패",
    "hard_rule": "하드룰 발동",
}


# Execution-layer labels (docs/02 v0.3): operating mode, order status, exception kinds, auto rules.
MODE_KO: dict[str, str] = {"auto": "자동 매매", "manual": "수동 승인", "paper": "모의투자"}

SIDE_KO: dict[str, str] = {"Buy": "매수", "Sell": "매도"}

ORDER_STATUS_KO: dict[str, str] = {
    "filled": "체결", "partial": "부분 체결", "open": "미체결", "blocked": "차단", "rejected": "거부", "exception": "예외", "cancelled": "취소",
}

EXCEPTION_KIND_KO: dict[str, str] = {
    "review": "등급 읽기 실패 (REVIEW)", "cap_exceeded": "자동 실행 한도 초과",
    "order_rejected": "주문 거부", "vendor_unavailable": "데이터 벤더 장애",
}

AUTO_RULE_KO: dict[str, str] = {
    "auto_daily_order_cap": "일일 주문 금액 상한", "auto_daily_loss_limit": "일일 손실 한도 (넘으면 자동 정지)",
    "auto_order_max": "주문당 금액 상한", "kill_switch_reset": "재가동 권한", "default_mode": "기본 운용 모드",
}

RISK_RULE_KO: dict[str, str] = {
    "max_position_pct": "종목당 비중 상한", "max_sector_pct": "섹터당 비중 상한", "max_gross_pct": "총 노출 상한",
    "stop_loss_pct": "손절 (진입가 대비)", "trailing_activate_pct": "트레일링 시작 수익", "trailing_pct": "트레일링 폭 (고점 대비)",
    "max_holding_days": "최대 보유 거래일", "daily_loss_limit": "일일 손실 한도 (다음날 신규 매수 중단)",
    "dd_halve": "드로다운 → 신규 비중 절반", "dd_stop": "드로다운 → 전량 청산·정지", "dd_stop_lock_days": "정지 후 잠금 거래일",
    "stoploss_guard_n": "StoplossGuard 손절 건수", "stoploss_guard_lookback_days": "StoplossGuard 관측 거래일",
    "stoploss_guard_lock_days": "StoplossGuard 잠금 거래일", "cooldown_days": "Cooldown 재진입 금지 거래일",
    "low_profit_n": "LowProfit 거래 건수", "low_profit_lookback_days": "LowProfit 관측 거래일", "low_profit_lock_days": "LowProfit 잠금 거래일",
    "regime_ma_days": "국면 필터 이동평균 (KOSPI)",
}

PAPER_CRITERIA_KO: dict[str, str] = {
    "min_weeks": "최소 모의 운용 기간(주)", "max_drawdown": "최대 드로다운 한도", "max_daily_loss_breaches": "일일 손실 한도 위반 허용 횟수",
    "max_stop_outs": "−20% 정지 발동 허용 횟수", "max_review_rate": "REVIEW 결정 비율 상한", "max_unexplained_orders": "결정 기록 없는 주문 허용 건수",
}

EXIT_REASON_KO: dict[str, str] = {
    "stop_loss": "손절", "trailing": "트레일링", "expired": "기간 만료", "exit_signal": "등급 하향", "force": "정지 청산",
}

OPERATION_KO: dict[str, str] = {
    "halt": "긴급 정지", "resume": "재가동", "mode_change": "운용 모드 변경", "rule_change": "규칙 변경",
}


def korean_name(ticker: str) -> str | None:
    return NAMES_KO.get(ticker.upper())


DECISION_SOURCE_KO: dict[str, str] = {"agent": "에이전트 결정", "rule": "규칙 청산"}

HALTED_BY_KO: dict[str, str] = {"system": "시스템 자동 정지"}

MARKET_KO: dict[str, str] = {"KRX": "국내", "US": "미국"}

HALT_SCOPE_KO: dict[str, str] = {"all": "전체 시장", "KRX": "국내만", "US": "미국만"}

HALT_TARGET_KO: dict[str, str] = {"manual": "운영자 긴급 정지", "daily_loss": "시스템 · 일일 손실 정지",
                                  "dd_stop": "시스템 · 드로다운 정지"}
