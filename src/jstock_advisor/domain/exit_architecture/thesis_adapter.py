"""L1(投資前提)の verdict adapter(Issue #882 PR-1 = N6。#846 の Phase B。dormant)。

既存の保有判断スコア(HoldingDecisionResult)を、**新しい採点機構を作らずに**、L1 の verdict
(ThesisVerdict)へ写す純粋関数。どこからも import されない(配線なし・保存なし・flag なし)。

性質(契約テストで固定する)
  ・読取専用の純粋関数: HoldingDecisionResult(メモリ上の型)を引数で受け取るだけ。
    HoldingDecisionService.evaluate() は外部読取・AuditLog への書込・baseline の作成を行うため
    **呼ばない**(このモジュールは services / infrastructure / providers を import しない)
  ・最終スコアだけで投資前提の悪化を判断しない: thesis_state は evidence(非価格の root)から決める。
    判定区分の帯は policy で『必要条件(W-2)』にもできるが、既定は置かない
  ・BROKEN = ハードゲートの発動 / UNDETERMINED = coverage・confidence が INSUFFICIENT_EVIDENCE で
    ハードゲートも発動していない(不明は INTACT ではない)
  ・fact_key は『root × 経済的事実 × 向き』の安定した識別子(文字列の解析はしない。等値比較のみ)。
    保有判断スコアの 3 部品・ハードゲート・E1 のルールが同じ事実を指すときは同じ fact_key / event_id
  ・水準を採点する項目(企業品質・投資ストーリー)の不足は『推定(SUSPECTED)』の補助 evidence。
    FE-1 の独立根拠に数えない。独立根拠(TRIGGERED)になるのは、E1 のルールが成立した事象
    (new_reason_codes)とハードゲートの理由だけ
  ・primary_source_confirmed は、E1 のルールが自分で『公式発表 / 登録簿』と判定する 4 本だけ True。
    ハードゲートの理由コードは、#888 / #889(キーワード経路の確認の質)の方針が出るまで全て
    False(不明を True にしない)
  ・総合利回り(投資ストーリーの軸)は L2 の構成要素で、evidence を作らない(UJ-1。L2 で 1 回だけ消費)

値は『非決定(案)』: 閾値は policy で受け取り、**既定値を置かない**
(事前登録 -> replay -> USER 承認)。

保存済みの結果からは復元できないもの(rev3.1 の G-2 / G-3): signal ごとの NOT_EVALUATED / SUSPECTED、
一次情報の確認の段階(例: 開示キーワードの検出が重大事象の確認語を伴うか)。そのため
major_scandal / accounting_problem / listing_maintenance_risk 由来の evidence は
primary_source_confirmed = False とする(キーワード一致のみでも E1 の rule は primary = True に
なるため、保存済みの結果だけでは確認の段階を区別できない)。

本 module に置かないもの
  arbiter・FULL の判定(N2)・Evidence.layer(#878 PR-2 の P-1)・additive な保存 field(#882 PR-2)・
  保有判断スコアの採点式 / weight / threshold(変更しない)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from jstock_advisor.domain.entities.enums import (
    EvidenceCoverageStatus,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
)
from jstock_advisor.domain.entities.holding_decision import HoldingDecisionResult
from jstock_advisor.domain.exit_architecture.determination import Determination
from jstock_advisor.domain.exit_architecture.evidence import (
    Evidence,
    EvidenceStatus,
    dedupe_by_fact_key,
    distinct_roots,
)
from jstock_advisor.domain.exit_architecture.verdicts import ThesisVerdict
from jstock_advisor.domain.exit_architecture.vocabulary import (
    ReliabilityClass,
    RootFactor,
    ThesisState,
    UndeterminedReason,
)


@dataclass(frozen=True)
class FactSpec:
    """1 つの入力(ルール・理由コード・評価軸)が指す、root × 事実の対応。

    root が None のものは、独立した経済的事実を持たない補助(Evidence を作らず、
    ThesisAdaptation.supporting_only に名前を残す)。
    """

    root: RootFactor | None
    fact_key: str
    event_id: str | None
    primary_source_confirmed: bool
    status: EvidenceStatus = EvidenceStatus.TRIGGERED

    def __post_init__(self) -> None:
        if not self.fact_key.strip():
            raise ValueError("fact_key は空にできない")


_DIVIDEND_EVENT = "RETURN_POLICY:dividend"
_BENEFIT_EVENT = "RETURN_POLICY:benefit"

#: E1(sell_signal)のルール 17 本 -> root × 事実。TRIGGERED のとき(new_reason_codes に出る)だけ使う。
SIGNAL_FACTS: dict[str, FactSpec] = {
    # 一次情報(公式発表)で確認されたときだけ TRIGGERED(sell_signal の実読)
    "dividend_cut": FactSpec(
        RootFactor.RETURN_POLICY, "RETURN_POLICY:dividend:cut", _DIVIDEND_EVENT, True
    ),
    "dividend_omission": FactSpec(
        RootFactor.RETURN_POLICY, "RETURN_POLICY:dividend:omission", _DIVIDEND_EVENT, True
    ),
    # override 依存(現行の Production では常に NOT_EVALUATED = 成立しない)
    "unfavorable_dividend_policy_change": FactSpec(
        RootFactor.RETURN_POLICY, "RETURN_POLICY:dividend:policy_change", _DIVIDEND_EVENT, False
    ),
    # manual_registry。primary = True(sell_signal の実読)
    "shareholder_benefit_abolished": FactSpec(
        RootFactor.RETURN_POLICY, "RETURN_POLICY:benefit:abolished", _BENEFIT_EVENT, True
    ),
    "shareholder_benefit_major_downgrade": FactSpec(
        RootFactor.RETURN_POLICY, "RETURN_POLICY:benefit:major_downgrade", _BENEFIT_EVENT, True
    ),
    # override 依存
    # 投資ストーリーの benefit_condition の入力ではない(別の事実)ので event_id を共有しない
    "long_term_holding_condition_unfavorable_change": FactSpec(
        RootFactor.RETURN_POLICY,
        "RETURN_POLICY:benefit:long_term_condition_change",
        None,
        False,
    ),
    # 価格・財務データ由来の判定(一次情報の確認フラグを立てない = primary False)
    "continuous_operating_income_decline": FactSpec(
        RootFactor.EARNINGS, "EARNINGS:operating_income:decline_streak", None, False
    ),
    "continuous_operating_cashflow_decline": FactSpec(
        RootFactor.CASHFLOW, "CASHFLOW:operating_cf:decline_streak", None, False
    ),
    # override 依存
    "large_earnings_guidance_downgrade": FactSpec(
        RootFactor.EARNINGS, "EARNINGS:guidance:downgrade", None, False
    ),
    "interest_bearing_debt_surge": FactSpec(
        RootFactor.BALANCE_SHEET, "BALANCE_SHEET:debt:surge", None, False
    ),
    "financial_health_severe_deterioration": FactSpec(
        RootFactor.BALANCE_SHEET,
        "BALANCE_SHEET:financial_health:severe_deterioration",
        None,
        False,
    ),
    # 二次情報(yfinance)由来では SUSPECTED 止まりで new_reason_codes に出ない。TRIGGERED にする
    # コードは現行に無く、一次情報の確認を保証できないため False(保守側)
    "balance_sheet_insolvency": FactSpec(
        RootFactor.BALANCE_SHEET, "BALANCE_SHEET:insolvency", None, False
    ),
    # 開示キーワードの検出。キーワード一致のみ(RISK_KEYWORD_DETECTED)でも E1 の rule は
    # status = TRIGGERED かつ primary = True になる。保存済みの結果から確認の段階を区別できない
    # ため False とする(F-2。MANAGER の回答)
    "major_scandal": FactSpec(RootFactor.GOVERNANCE_EVENT, "GOVERNANCE_EVENT:scandal", None, False),
    "accounting_problem": FactSpec(
        RootFactor.GOVERNANCE_EVENT, "GOVERNANCE_EVENT:accounting", None, False
    ),
    "listing_maintenance_risk": FactSpec(
        RootFactor.GOVERNANCE_EVENT, "GOVERNANCE_EVENT:listing", None, False
    ),
    # root を持たない補助(人が宣言する前提 / 規制資本は L1 の root が無く常に NOT_EVALUATED)
    "investment_premise_broken": FactSpec(None, "SUPPORTING:investment_premise", None, False),
    "regulatory_capital_breach": FactSpec(None, "SUPPORTING:regulatory_capital", None, False),
}

#: ハードゲートの理由コード 7 種 -> root × 事実
HARD_GATE_FACTS: dict[str, FactSpec] = {
    # 入力は is_debt_excess(= 自己資本比率 < 0。#11 の SUSPECTED と同じ事実)と
    # balance_sheet_insolvency の TRIGGERED(一次情報の provider が無く、現行では立たない)。
    # 確認の質の方針が出るまで False
    "DEBT_EXCESS": FactSpec(RootFactor.BALANCE_SHEET, "BALANCE_SHEET:insolvency", None, False),
    # snapshot の判定フラグ(一次情報の確認を要する構造ではない)
    "GOING_CONCERN_DOUBT": FactSpec(
        RootFactor.GOVERNANCE_EVENT, "GOVERNANCE_EVENT:going_concern", None, False
    ),
    # 入力は major_scandal / listing_maintenance_risk / accounting_problem の rule。
    # キーワード一致のみでも発動しうるため False(F-2)
    "BANKRUPTCY_FILING": FactSpec(
        RootFactor.GOVERNANCE_EVENT, "GOVERNANCE_EVENT:scandal", None, False
    ),
    "DELISTING_OR_KANRI": FactSpec(
        RootFactor.GOVERNANCE_EVENT, "GOVERNANCE_EVENT:listing", None, False
    ),
    "ACCOUNTING_FRAUD": FactSpec(
        RootFactor.GOVERNANCE_EVENT, "GOVERNANCE_EVENT:accounting", None, False
    ),
    # 入力は dividend_omission の TRIGGERED(公式発表。現行では立たない)と、financial_crisis
    # カテゴリの控除点 > 0(財務の signal。hard_gate_excluded でないもの = #9・#10 だけが寄与しうる。
    # 名前に CASHFLOW とあるが営業 CF の signal〔#7〕は含まれない)。確認の質の方針が出るまで False。
    # Evidence は配当の無配(RETURN_POLICY)だけを写す。財務側は #10 が成立すれば別に TRIGGERED で出る
    "DIVIDEND_OMISSION_AND_CASHFLOW_CRISIS": FactSpec(
        RootFactor.RETURN_POLICY, "RETURN_POLICY:dividend:omission", _DIVIDEND_EVENT, False
    ),
    # 点数ベース(人が承認した baseline + 投資ストーリーの点数 < 閾値)。独立した経済的事実の根拠を
    # 持たない。thesis_state = BROKEN の理由には使うが、root の根拠にしない(F-3)
    "INVESTMENT_THESIS_COLLAPSE": FactSpec(None, "SUPPORTING:thesis_collapse", None, False),
}

#: 企業品質 10 項目・投資ストーリー 6 軸 -> root × 事実。水準の不足は SUSPECTED の補助 evidence
_SUSPECTED = EvidenceStatus.SUSPECTED
ITEM_FACTS: dict[str, FactSpec] = {
    # 企業品質(item_code)
    "financial_health_equity_ratio": FactSpec(
        RootFactor.BALANCE_SHEET, "BALANCE_SHEET:equity_ratio:low", None, False, _SUSPECTED
    ),
    # is_debt_excess = 自己資本比率 < 0 で、#11(balance_sheet_insolvency)の SUSPECTED と同じ事実
    "financial_health_debt_excess": FactSpec(
        RootFactor.BALANCE_SHEET, "BALANCE_SHEET:insolvency", None, False, _SUSPECTED
    ),
    "cash_generation_cf_income_ratio": FactSpec(
        RootFactor.CASHFLOW, "CASHFLOW:operating_cf_vs_income:weak", None, False, _SUSPECTED
    ),
    # 『直近から連続して正値の期数』= 水準の持続。#7 の『連続悪化』とは測る事実が違う
    "cash_generation_cf_streak": FactSpec(
        RootFactor.CASHFLOW, "CASHFLOW:operating_cf:positive_streak_short", None, False, _SUSPECTED
    ),
    "profitability_roe": FactSpec(RootFactor.EARNINGS, "EARNINGS:roe:low", None, False, _SUSPECTED),
    "profitability_eps_stability": FactSpec(
        RootFactor.EARNINGS, "EARNINGS:eps:unstable", None, False, _SUSPECTED
    ),
    # 『変動の大きさ・黒字四半期の比率』。#6 の『連続悪化』と同じ系列だが測る事実が違う
    "stability_operating_income": FactSpec(
        RootFactor.EARNINGS, "EARNINGS:operating_income:instability", None, False, _SUSPECTED
    ),
    "stability_deficit": FactSpec(
        RootFactor.EARNINGS, "EARNINGS:deficit:periods", None, False, _SUSPECTED
    ),
    "governance_going_concern": FactSpec(
        RootFactor.GOVERNANCE_EVENT, "GOVERNANCE_EVENT:going_concern", None, False, _SUSPECTED
    ),
    # 入力は bool(material_event_keywords_found)(『重大事象の確認語が 1 つでもあるか』)。
    # listing_maintenance_risk の rule とは独立で、測る事実が広い(#889 と同じ rule 非依存の性質)
    "governance_listing_risk": FactSpec(
        RootFactor.GOVERNANCE_EVENT,
        "GOVERNANCE_EVENT:material_event_words",
        None,
        False,
        _SUSPECTED,
    ),
    # 投資ストーリー(item_code)。推定の減配 / 無配は補助 evidence(FE-1 の独立根拠にしない)
    "dividend_policy": FactSpec(
        RootFactor.RETURN_POLICY,
        "RETURN_POLICY:dividend:policy_deterioration",
        _DIVIDEND_EVENT,
        False,
        _SUSPECTED,
    ),
    "benefit_condition": FactSpec(
        RootFactor.RETURN_POLICY,
        "RETURN_POLICY:benefit:condition_deterioration",
        _BENEFIT_EVENT,
        False,
        _SUSPECTED,
    ),
    # E1 のルール(営業利益・営業 CF の連続減少 / 財務の著しい悪化)から導出された値
    "profit_cf_premise": FactSpec(
        RootFactor.EARNINGS, "EARNINGS:profit_cf_premise:broken", None, False, _SUSPECTED
    ),
    "financial_premise": FactSpec(
        RootFactor.BALANCE_SHEET, "BALANCE_SHEET:financial_premise:broken", None, False, _SUSPECTED
    ),
    # root を持たない: 総合利回りは L2 の構成要素(UJ-1)/ custom_conditions は利用者の定義
    "total_yield": FactSpec(None, "SUPPORTING:total_yield_l2", None, False),
    "custom_conditions": FactSpec(None, "SUPPORTING:custom_conditions", None, False),
}

#: 現行の Production で『成立しない(到達しない)』入力。コードの読みによる(実発生は未確認)。
#: 表の見直しの合図: provider や配線が変わって到達しうるようになったら、この集合と表を見直す。
#: 到達しない入力にも対応を持つ(将来の provider の変更で成立したときに、写されないものを作らない)
NOT_REACHABLE_IN_PRODUCTION: dict[str, frozenset[str]] = {
    # override を渡す箇所が src に無く、常に NOT_EVALUATED(5 本)/ 公式発表の provider が無い(2 本)/
    # 一次情報の provider が無く SUSPECTED 止まり / 金融業向けで常に NOT_EVALUATED
    "signal": frozenset(
        {
            "unfavorable_dividend_policy_change",
            "large_earnings_guidance_downgrade",
            "interest_bearing_debt_surge",
            "long_term_holding_condition_unfavorable_change",
            "investment_premise_broken",
            "dividend_cut",
            "dividend_omission",
            "balance_sheet_insolvency",
            "regulatory_capital_breach",
        }
    ),
    # 前提の signal / フラグが成立しない。到達しうるのは BANKRUPTCY_FILING・DELISTING_OR_KANRI・
    # ACCOUNTING_FRAUD(開示キーワード経路 = #888 / #889)と INVESTMENT_THESIS_COLLAPSE のみ
    "hard_gate": frozenset(
        {"DEBT_EXCESS", "GOING_CONCERN_DOUBT", "DIVIDEND_OMISSION_AND_CASHFLOW_CRISIS"}
    ),
    # 現行の provider は going_concern を常に False で返し、満点になる(不足にならない)
    "item": frozenset({"governance_going_concern"}),
}

#: 保有判断スコアの信頼度 -> L0 の信頼クラス(案。テストで固定)
RELIABILITY_BY_CONFIDENCE: dict[HoldingDecisionConfidenceLevel, ReliabilityClass] = {
    HoldingDecisionConfidenceLevel.HIGH: ReliabilityClass.RELIABLE,
    HoldingDecisionConfidenceLevel.MEDIUM: ReliabilityClass.RELIABLE,
    HoldingDecisionConfidenceLevel.LOW: ReliabilityClass.DEGRADED,
    HoldingDecisionConfidenceLevel.INSUFFICIENT_EVIDENCE: ReliabilityClass.UNUSABLE,
}


@dataclass(frozen=True)
class ThesisMappingPolicy:
    """thesis_state の写し方。**既定値は無い**(値は事前登録 -> replay -> USER 承認で確定する)。

    min_distinct_roots_for_weakening     WEAKENING に必要な、独立(TRIGGERED)な非価格の root の数
    item_shortfall_max_ratio             評価軸の点が weight に対してこの割合以下なら『不足』
                                         (補助 evidence を作る)。0 以上 1 以下
    weakening_required_categories        None = W-1(判定区分の帯は裏付けに留め、必要条件にしない)。
                                         集合 = W-2(WEAKENING には、判定区分がこの集合に
                                         含まれることも要る)
    """

    min_distinct_roots_for_weakening: int
    item_shortfall_max_ratio: float
    weakening_required_categories: frozenset[HoldingDecisionCategory] | None

    def __post_init__(self) -> None:
        if isinstance(self.min_distinct_roots_for_weakening, bool) or not isinstance(
            self.min_distinct_roots_for_weakening, int
        ):
            raise TypeError("min_distinct_roots_for_weakening は int")
        if self.min_distinct_roots_for_weakening < 1:
            raise ValueError("min_distinct_roots_for_weakening は 1 以上")
        if not math.isfinite(self.item_shortfall_max_ratio) or not (
            0 <= self.item_shortfall_max_ratio <= 1
        ):
            raise ValueError("item_shortfall_max_ratio は 0 以上 1 以下の有限の数値")
        if (
            self.weakening_required_categories is not None
            and not self.weakening_required_categories
        ):
            raise ValueError("W-2 の集合は空にできない(帯を必要条件にしないなら None)")


@dataclass(frozen=True)
class ThesisAdaptation:
    """adapter の結果。verdict と、Evidence にならなかった入力の記録(黙って捨てない)。

    supporting_only  root を持たない補助として扱った入力の名前
    unmapped         対応表に無かった名前(新しいルール・理由コード。契約テストで対応表の網羅を固定)
    """

    verdict: ThesisVerdict
    supporting_only: tuple[str, ...]
    unmapped: tuple[str, ...]


def _evidence_from(source: str, spec: FactSpec) -> Evidence | None:
    if spec.root is None:
        return None
    return Evidence(
        root_factor=spec.root,
        source=source,
        fact_key=spec.fact_key,
        status=spec.status,
        primary_source_confirmed=spec.primary_source_confirmed,
        event_id=spec.event_id,
    )


def _shortfall_items(
    result: HoldingDecisionResult, policy: ThesisMappingPolicy
) -> tuple[tuple[str, str], ...]:
    """評価済み(EVALUATED)で、点が weight に対して不足している項目(source, item_code)。"""
    found: list[tuple[str, str]] = []
    for part, items in (
        ("company_quality", result.company_quality.items),
        ("investment_thesis", result.investment_thesis.items),
    ):
        for item in items:
            if item.status is not EvidenceCoverageStatus.EVALUATED or item.weight <= 0:
                continue
            if item.points_earned / item.weight <= policy.item_shortfall_max_ratio:
                found.append((f"{part}:{item.item_code}", item.item_code))
    return tuple(found)


def adapt_holding_decision_to_thesis(
    result: HoldingDecisionResult, policy: ThesisMappingPolicy
) -> ThesisAdaptation:
    """保有判断スコアの結果を L1 の ThesisVerdict へ写す(純粋関数)。"""
    raw: list[Evidence] = []
    supporting: list[str] = []
    unmapped: list[str] = []

    def add(source: str, name: str, table: dict[str, FactSpec]) -> None:
        spec = table.get(name)
        if spec is None:
            unmapped.append(name)
            return
        evidence = _evidence_from(source, spec)
        if evidence is None:
            supporting.append(name)
        else:
            raw.append(evidence)

    for code in result.new_reason_codes:
        add(f"risk_deduction:{code}", code, SIGNAL_FACTS)
    for code in result.hard_gate.reason_codes:
        add(f"hard_gate:{code}", code, HARD_GATE_FACTS)
    for source, item_code in _shortfall_items(result, policy):
        add(source, item_code, ITEM_FACTS)

    evidence = dedupe_by_fact_key(raw)
    reliability = RELIABILITY_BY_CONFIDENCE[result.confidence]
    state = _thesis_state(result, policy, evidence)
    verdict = ThesisVerdict(
        thesis_state=state,
        reliability=reliability,
        evidence=evidence,
        hard_gate_triggered=result.hard_gate.triggered,
        hard_gate_reasons=tuple(result.hard_gate.reason_codes),
    )
    return ThesisAdaptation(
        verdict=verdict,
        supporting_only=tuple(dict.fromkeys(supporting)),
        unmapped=tuple(dict.fromkeys(unmapped)),
    )


def _thesis_state(
    result: HoldingDecisionResult,
    policy: ThesisMappingPolicy,
    evidence: tuple[Evidence, ...],
) -> Determination[ThesisState]:
    if result.hard_gate.triggered:
        return Determination.of(ThesisState.BROKEN)
    if result.confidence is HoldingDecisionConfidenceLevel.INSUFFICIENT_EVIDENCE:
        return Determination.undetermined(
            UndeterminedReason.COVERAGE_INSUFFICIENT, "coverage・confidence が不足している"
        )
    if (
        policy.weakening_required_categories is not None
        and result.category not in policy.weakening_required_categories
    ):
        return Determination.of(ThesisState.INTACT)
    if len(distinct_roots(evidence)) >= policy.min_distinct_roots_for_weakening:
        return Determination.of(ThesisState.WEAKENING)
    return Determination.of(ThesisState.INTACT)
