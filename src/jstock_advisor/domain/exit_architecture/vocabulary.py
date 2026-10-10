"""Exit Architecture の語彙(Issue #878 PR-1 = C0。#846 の rev2〜rev3.3 が設計)。

売却・保有継続の判断を層(L0〜L6)に分けて評価し、Arbiter が最終 action を決めるための
共通の語彙。**型と語彙だけ**であり、現行のエンジン(sell_signal / profit_taking /
holding_decision)からは参照されない(配線なし・保存なし・flag なし)。

名称・状態の個数は設計上の「案」であり、確定ではない(値・閾値は事前登録 -> shadow ->
replay / backtest -> USER 承認で決める)。語彙の集合は契約テストで固定しており、変更は
意図した変更として、テストの更新と一緒に行う。
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class ExitAction(StrEnum):
    """最終 action。HOLD は正式な最適解(何もしない = 不明、ではない)。"""

    HOLD = "HOLD"
    PARTIAL = "PARTIAL"
    FULL = "FULL"


class ExitClass(StrEnum):
    """なぜ売る(売らない)のか。action とは別の軸。HOLD のときは NONE。"""

    RISK_EXIT = "RISK_EXIT"  # 投資前提の崩壊
    VALUE_EXIT = "VALUE_EXIT"  # 割安の解消・将来期待リターンが小さい
    PROFIT_PROTECTION = "PROFIT_PROTECTION"  # 利益の毀損
    CAPITAL_ROTATION = "CAPITAL_ROTATION"  # 資本の入替
    NONE = "NONE"  # HOLD_OPTIMAL


class Strength(IntEnum):
    """class ごとの候補の強さ(内部)。WATCH は action にならない予備段階。"""

    NONE = 0
    WATCH = 1
    PARTIAL = 2
    FULL = 3


class ReviewFlag(StrEnum):
    """売却の推奨ではなく、人の確認を要することを表す(action とは別の軸)。

    数量を伴わない現行の「売却を検討」「至急確認」を、PARTIAL / FULL へ自動変換
    しないための軸(UJ-15)。
    """

    NONE = "NONE"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    URGENT_REVIEW = "URGENT_REVIEW"


class ThesisState(StrEnum):
    """L1(投資前提)の状態。評価できない場合は Determination の UNDETERMINED で表す。"""

    INTACT = "INTACT"
    WEAKENING = "WEAKENING"
    BROKEN = "BROKEN"


class RegimeState(StrEnum):
    """L3(価格の動き)の状態。名称・個数は仮(非決定)。"""

    HEALTHY = "HEALTHY"
    PEAK_WARNING = "PEAK_WARNING"
    DOWNTREND_CONFIRMED = "DOWNTREND_CONFIRMED"
    BREAKDOWN = "BREAKDOWN"


class ReliabilityClass(StrEnum):
    """L0(データ信頼性)。売る理由にはせず、強い action を許すかの上限だけを決める。"""

    RELIABLE = "RELIABLE"
    DEGRADED = "DEGRADED"
    UNUSABLE = "UNUSABLE"


class RootFactor(StrEnum):
    """根拠の根(root)。同じ root の根拠は何件あっても 1 と数える(R-A)。"""

    PRICE_PATH = "PRICE_PATH"  # 価格系列の経路
    VALUATION_LEVEL = "VALUATION_LEVEL"  # 現在価格と適正価格の関係
    EARNINGS = "EARNINGS"
    CASHFLOW = "CASHFLOW"
    BALANCE_SHEET = "BALANCE_SHEET"
    RETURN_POLICY = "RETURN_POLICY"  # 株主還元の方針(配当・優待)
    GOVERNANCE_EVENT = "GOVERNANCE_EVENT"
    EVENT_RISK = "EVENT_RISK"  # 決算等の近接。売買の時機であり、売る理由ではない
    PORTFOLIO = "PORTFOLIO"  # 集中。量の問題であり、売る理由ではない
    OPPORTUNITY_COST = "OPPORTUNITY_COST"  # L4。保有と代替の期待リターンの差(FE-3 の根拠)
    DATA = "DATA"  # L0。売る理由にしない
    USER_DIRECTIVE = "USER_DIRECTIVE"  # ユーザー設定の目標。通知であり、売却根拠ではない(UJ-6)


class FullEvidenceKind(StrEnum):
    """FULL の独立根拠の種類(FE-1〜FE-3)。

    valuation 枯渇(上値余地・適正価格との関係)は L2 の重要な材料だが、これらの
    いずれでもない。**枯渇のみでは FULL の十分条件にならない**(UJ-4)。
    """

    THESIS_DETERIORATION = "THESIS_DETERIORATION"  # FE-1: 投資前提の悪化(L1)
    EXPECTED_RETURN_DETERIORATION = "EXPECTED_RETURN_DETERIORATION"  # FE-2: 将来期待総リターン
    ROTATION_OPPORTUNITY = "ROTATION_OPPORTUNITY"  # FE-3: 資本入替の機会(L4)


class TriggerKind(StrEnum):
    """現行 E2(profit_taking)の理由の種別。語彙のみ(E2 への配線は #878 の PR-3)。

    ユーザー目標と価格 × 上値余地は、現行コードでは同じ origin を持ち、理由の文字列しか
    違いがない。reason の文字列を解析せずに区別するための構造化された種別(UJ-6)。
    """

    PRICE_UPSIDE_MATRIX = "PRICE_UPSIDE_MATRIX"  # 含み益 × 上値余地(PX-1 / PX-2)
    FAIR_VALUE_STRONG = "FAIR_VALUE_STRONG"
    FAIR_VALUE_PARTIAL_GATE = "FAIR_VALUE_PARTIAL_GATE"
    PROFIT_PROTECTION_STRONG = "PROFIT_PROTECTION_STRONG"  # PX-3
    PARTIAL_CONDITIONS = "PARTIAL_CONDITIONS"  # 条件の件数(PX-4 / PX-5)
    FULL_MODERATE_CONDITIONS = "FULL_MODERATE_CONDITIONS"  # 条件の件数(PX-6)
    FULL_STRONG_CRITICAL = "FULL_STRONG_CRITICAL"  # 投資前提の崩壊・不祥事・確定減配 + CF 悪化
    USER_TARGET_PRICE = "USER_TARGET_PRICE"
    USER_TARGET_RATE = "USER_TARGET_RATE"


class ExitLayer(StrEnum):
    """判断の層(Decision の主たる層)。"""

    L0_DATA_RELIABILITY = "L0_DATA_RELIABILITY"
    L1_THESIS = "L1_THESIS"
    L2_EXPECTED_RETURN = "L2_EXPECTED_RETURN"
    L3_PRICE_REGIME = "L3_PRICE_REGIME"
    L4_OPPORTUNITY_COST = "L4_OPPORTUNITY_COST"


class UndeterminedReason(StrEnum):
    """UNDETERMINED(値を作れない)の理由コード。値を捏造しないための記録。"""

    COMPONENT_NOT_IMPLEMENTED = "COMPONENT_NOT_IMPLEMENTED"  # 例: #601 / #602 が未実装
    INPUT_MISSING = "INPUT_MISSING"
    COVERAGE_INSUFFICIENT = "COVERAGE_INSUFFICIENT"
    NOT_EVALUATED = "NOT_EVALUATED"
    RELIABILITY_UNUSABLE = "RELIABILITY_UNUSABLE"
    GUARD_NOT_MET = "GUARD_NOT_MET"  # valuation 信頼度ガード等を満たさない


class SuppressionReason(StrEnum):
    """選ばれなかった候補が、なぜ選ばれなかったか(suppressed trace)。"""

    RELIABILITY_CAP = "RELIABILITY_CAP"
    PROFIT_PROTECTION_PARTIAL_CAP = "PROFIT_PROTECTION_PARTIAL_CAP"
    NO_FULL_EVIDENCE = "NO_FULL_EVIDENCE"
    EARNINGS_WINDOW = "EARNINGS_WINDOW"
    MITIGATION = "MITIGATION"
    TIMING_LAYER = "TIMING_LAYER"  # 上昇トレンドによるタイミング層の降格(緩和要因とは別の層)
    UNDETERMINED_INPUT = "UNDETERMINED_INPUT"
    DUPLICATE_EVIDENCE = "DUPLICATE_EVIDENCE"
    SUPERSEDED_BY_STRONGER = "SUPERSEDED_BY_STRONGER"
