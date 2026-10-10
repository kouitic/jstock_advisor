"""開示の『確認』判定(Issue #889 PR-1。dormant な純粋関数)の契約テスト。

## この file の構成

    1 現行の挙動の固定(characterization)   ← 最初の commit。現行の関数を一切変更しない
    2 新しい判定(domain/signals/disclosure_confirmation.py)の契約  ← 以降の commit

## 1 の目的

PR-2(旧方式の切替)が『何を変えるのか』を、差分として測れるようにする土台。
現行の `detect_disclosure_risk_keywords` / `classify_disclosure_risk_keywords_with_confirmation`
の挙動を、**期待値をこのテストの中で独立に導出して**固定する(現行の関数の出力を期待値にしない)。
固定する現行の挙動は、是正の対象である以下を含む(= 『正しい』という意味ではなく『現在こうである』)。

    ・結びつけがない(別の開示にある確認の言葉でも確認になる)
    ・否定を見ない(『該当しません』でも確認になる)
    ・『継続企業』を確認の言葉に含む(危険な言葉『継続企業の前提に関する重要事象』の中に
      含まれるため、危険な言葉だけで自己充足して確認になる)
    ・accounting_problem は常に RISK_KEYWORD_DETECTED(2 段階を持たない)

文言はすべて合成であり、実在の開示は使わない。
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import itertools
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import DisclosureRiskConfirmationLevel
from jstock_advisor.domain.screening.rules import (
    MATERIAL_EVENT_KEYWORDS,
    detect_disclosure_risk_keywords,
    detect_material_event_keywords,
)
from jstock_advisor.domain.signals import disclosure_confirmation as dc
from jstock_advisor.domain.signals import sell_signal
from jstock_advisor.interfaces.types import Disclosure

_NOW = dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)
_REPO = Path(__file__).resolve().parents[2]

DETECTED = DisclosureRiskConfirmationLevel.RISK_KEYWORD_DETECTED
CONFIRMED = DisclosureRiskConfirmationLevel.MATERIAL_EVENT_CONFIRMED


def _d(title: str, summary: str | None = None) -> Disclosure:
    return Disclosure(
        stock_code="9999",
        published_at=_NOW,
        title=title,
        category=None,
        summary=summary,
        url=None,
        source=_SOURCE,
    )


# 現行の仕様を、独立に書き下した期待値(コードから導出しない)
_RISK_TO_RULE = {
    "特別調査委員会": "major_scandal",
    "第三者委員会": "major_scandal",
    "内部統制上の重要な不備": "accounting_problem",
    "不適切な会計処理": "accounting_problem",
    "上場廃止基準": "listing_maintenance_risk",
    "監理銘柄": "listing_maintenance_risk",
    "整理銘柄": "listing_maintenance_risk",
    "継続企業の前提に関する重要事象": "listing_maintenance_risk",
}
_OLD_CONFIRMATION_WORDS = (
    "決算訂正",
    "決算発表延期",
    "監査意見",
    "業績予想の大幅修正",
    "上場維持",
    "重大な財務損失",
    "経営陣の責任",
    "不正の事実",
    "継続企業",
)
_RULES = ("major_scandal", "accounting_problem", "listing_maintenance_risk")
_TWO_STAGE = ("major_scandal", "listing_maintenance_risk")


def _config_keywords() -> list[str]:
    with (_REPO / "config" / "sell_rules.yaml").open(encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    keywords = loaded["disclosure_risk_keywords"]
    assert isinstance(keywords, list)
    return [str(k) for k in keywords]


def _old_classify(disclosures: list[Disclosure]):
    found = detect_disclosure_risk_keywords(disclosures, _config_keywords())
    material = detect_material_event_keywords(disclosures)
    return sell_signal.classify_disclosure_risk_keywords_with_confirmation(found, material)


def _expected_old(risk: str, material_present: bool):
    """独立に導出した現行の期待値: 検出された rule だけが値を持ち、確認の言葉が
    どこかにあれば 2 段階 rule は CONFIRMED、会計は常に DETECTED。"""
    expected = {rule: None for rule in _RULES}
    rule = _RISK_TO_RULE[risk]
    if rule in _TWO_STAGE and material_present:
        expected[rule] = CONFIRMED
    else:
        expected[rule] = DETECTED
    return expected


# --- 1 現行の挙動の固定 ------------------------------------------------------


def test_old_vocabulary_is_pinned() -> None:
    """語彙の表そのものが、このテストの書き下しと一致する(語彙が動いたら気づく)。"""
    assert dict(sell_signal._KEYWORD_RULE_MAP) == _RISK_TO_RULE
    assert tuple(MATERIAL_EVENT_KEYWORDS) == _OLD_CONFIRMATION_WORDS
    assert set(sell_signal._TWO_STAGE_CONFIRMATION_RULES) == set(_TWO_STAGE)
    assert sorted(_config_keywords()) == sorted(_RISK_TO_RULE)


@pytest.mark.parametrize("risk", sorted(_RISK_TO_RULE))
@pytest.mark.parametrize("where", ["title", "summary"])
def test_old_detection_finds_each_risk_keyword_in_title_or_summary(risk: str, where: str) -> None:
    d = _d(risk, "本文") if where == "title" else _d("標題", f"本文 {risk} 本文")
    assert detect_disclosure_risk_keywords([d], _config_keywords()) == [risk]


def test_old_detection_is_sorted_and_deduplicated() -> None:
    ds = [_d("第三者委員会"), _d("第三者委員会", "監理銘柄"), _d("不適切な会計処理")]
    assert detect_disclosure_risk_keywords(ds, _config_keywords()) == sorted(
        {"第三者委員会", "監理銘柄", "不適切な会計処理"}
    )


@pytest.mark.parametrize("risk", sorted(_RISK_TO_RULE))
def test_old_risk_keyword_alone_matches_expected(risk: str) -> None:
    """危険な言葉だけの開示。『継続企業の前提に関する重要事象』は、その文字列の中に確認の言葉
    『継続企業』を含むため、**危険な言葉だけで自己充足して確認になる**(現行の欠陥。PR-2 で是正)。"""
    got = _old_classify([_d(risk)])
    self_satisfied = "継続企業" in risk
    assert got == _expected_old(risk, material_present=self_satisfied)
    assert (got["listing_maintenance_risk"] is CONFIRMED) == (
        risk == "継続企業の前提に関する重要事象"
    )


@pytest.mark.parametrize("word", _OLD_CONFIRMATION_WORDS)
@pytest.mark.parametrize("risk", sorted(_RISK_TO_RULE))
def test_old_grid_same_disclosure(risk: str, word: str) -> None:
    got = _old_classify([_d(risk, f"{word}について")])
    assert got == _expected_old(risk, material_present=True)


@pytest.mark.parametrize("word", _OLD_CONFIRMATION_WORDS)
@pytest.mark.parametrize("risk", sorted(_RISK_TO_RULE))
def test_old_grid_separate_disclosures_also_confirm(risk: str, word: str) -> None:
    """現行は結びつけがない(別の開示の確認の言葉でも確認になる)。PR-2 で A1 に是正。"""
    got = _old_classify([_d(risk), _d(f"{word}について")])
    assert got == _expected_old(risk, material_present=True)


@pytest.mark.parametrize("word", _OLD_CONFIRMATION_WORDS)
def test_old_confirmation_word_alone_detects_nothing(word: str) -> None:
    """確認の言葉だけでは検出にならない(危険な言葉が無ければ全 rule が None)。"""
    assert _old_classify([_d(f"{word}について")]) == {rule: None for rule in _RULES}


@pytest.mark.parametrize(
    "summary",
    [
        "上場廃止基準に該当しません。決算訂正はありません",
        "監査意見は適正意見です。上場廃止基準には該当しない",
        "不正の事実は認められませんでした。第三者委員会を設置しません",
    ],
)
def test_old_ignores_negation(summary: str) -> None:
    """現行は否定を見ない(否定された確認の言葉でも確認になる)。PR-2 で B2 に是正。"""
    got = _old_classify([_d("上場廃止基準 第三者委員会", summary)])
    assert got["listing_maintenance_risk"] is CONFIRMED
    assert got["major_scandal"] is CONFIRMED


def test_old_accounting_is_never_confirmed() -> None:
    """会計は 2 段階を持たない: 確認の言葉が全部あっても常に RISK_KEYWORD_DETECTED。"""
    every_word = "。".join(_OLD_CONFIRMATION_WORDS)
    got = _old_classify([_d("不適切な会計処理 内部統制上の重要な不備", every_word)])
    assert got["accounting_problem"] is DETECTED
    assert got["major_scandal"] is None
    assert got["listing_maintenance_risk"] is None


def test_old_material_event_detection_is_whole_text_substring() -> None:
    """確認の言葉の検出は title + summary の部分文字列一致(重複・順序に依存しない)。"""
    ds = [_d("監査意見", "経営陣の責任"), _d("監査意見")]
    assert detect_material_event_keywords(ds) == sorted(["監査意見", "経営陣の責任"])
    assert detect_material_event_keywords([]) == []


# =====================================================================================
# 2 新しい判定(domain/signals/disclosure_confirmation.py)の契約
# =====================================================================================

ACC = "accounting_problem"
_TWO_STAGE_WORDS = tuple(w for w in _OLD_CONFIRMATION_WORDS if w != "継続企業")
_RISK_OF_RULE = {rule: sorted(k for k, r in _RISK_TO_RULE.items() if r == rule) for rule in _RULES}
_ACC_RISKS = _RISK_OF_RULE[ACC]
_ALL_RISKS = sorted(_RISK_TO_RULE)
_SRC = _REPO / "src" / "jstock_advisor"
_MODULE_PATH = _SRC / "domain" / "signals" / "disclosure_confirmation.py"

SERIOUS = dc.AccountingLevel.SERIOUS_PROBLEM_CONFIRMED
FRAUD = dc.AccountingLevel.FRAUD_FACT_CONFIRMED
ACC_DETECTED = dc.AccountingLevel.RISK_KEYWORD_DETECTED
ACC_NONE = dc.AccountingLevel.NONE
TS_CONFIRMED = dc.ConfirmationLevel.MATERIAL_EVENT_CONFIRMED
TS_DETECTED = dc.ConfirmationLevel.RISK_KEYWORD_DETECTED
TS_NONE = dc.ConfirmationLevel.NONE


def _assess(*disclosures: Disclosure) -> dc.DisclosureConfirmation:
    return dc.assess_disclosure_confirmation(list(disclosures))


def _acc(*disclosures: Disclosure) -> dc.AccountingAssessment:
    return _assess(*disclosures).accounting_problem


def _two(rule: str, *disclosures: Disclosure) -> dc.RuleAssessment:
    result = _assess(*disclosures)
    assessment = getattr(result, rule)
    assert isinstance(assessment, dc.RuleAssessment)
    return assessment


# --- 2.0 語彙・型の対応 ----------------------------------------------------------------


def test_new_vocabulary_matches_the_old_one_except_c1() -> None:
    assert dict(dc.RISK_KEYWORD_TO_RULE) == _RISK_TO_RULE
    assert dc.ASSESSED_RULES == _RULES
    for rule in _TWO_STAGE:
        assert tuple(dc.DEFAULT_RULES.confirmation_words[rule]) == _TWO_STAGE_WORDS
    assert "継続企業" not in _TWO_STAGE_WORDS
    assert len(_TWO_STAGE_WORDS) == 8


def test_level_strings_match_the_existing_and_the_agreed_vocabulary() -> None:
    old = {level.value for level in DisclosureRiskConfirmationLevel}
    assert {lv.value for lv in dc.ConfirmationLevel} == old | {"NONE"}
    assert {lv.value for lv in dc.AccountingLevel} == {
        "NONE",
        "RISK_KEYWORD_DETECTED",
        "SERIOUS_PROBLEM_CONFIRMED",
        "FRAUD_FACT_CONFIRMED",
    }


def test_default_phrase_lists_are_unique_and_do_not_overlap_each_other() -> None:
    negation = dc.DEFAULT_NEGATION_PHRASES
    non_assertive = dc.DEFAULT_NON_ASSERTIVE_PHRASES
    assert len(set(negation)) == len(negation)
    assert len(set(non_assertive)) == len(non_assertive)
    assert all(negation) and all(non_assertive)
    # 非断定の語が否定の語を含むと、blocked_by_* の意味が崩れる
    assert not [n for n in negation for p in non_assertive if n in p]
    # 『予定』は確定の開示がありうるため非断定に含めない(境界。PR 本文に明記)
    assert "予定" not in non_assertive
    # 短い語は確定の文を巻き込むため含めない
    for short in ("調査", "検討", "次第", "なら", "ば", "ない", "なし"):
        assert short not in non_assertive and short not in negation


# --- 2.1 FN-1: 検出は消えない(現行の検出と完全に一致) -------------------------------------


def _detection_inputs() -> list[list[Disclosure]]:
    inputs: list[list[Disclosure]] = []
    combos = [c for r in (1, 2) for c in itertools.combinations(_ALL_RISKS, r)] + [
        tuple(_ALL_RISKS)
    ]
    for combo in combos:
        joined = "。".join(combo)
        inputs.append([_d(joined)])
        inputs.append([_d("標題", joined)])
        inputs.append([_d(k) for k in combo])
        inputs.append([_d(f"{combo[0]} 継続企業", "本文")])
    inputs.append([])
    inputs.append([_d("無関係な開示")])
    return inputs


@pytest.mark.parametrize("disclosures", _detection_inputs())
def test_fn1_detection_is_identical_to_the_old_detection(disclosures: list[Disclosure]) -> None:
    old_found = detect_disclosure_risk_keywords(disclosures, _config_keywords())
    result = dc.assess_disclosure_confirmation(disclosures)
    assert result.major_scandal.detected_keywords == tuple(
        k for k in old_found if _RISK_TO_RULE[k] == "major_scandal"
    )
    assert result.accounting_problem.detected_keywords == tuple(
        k for k in old_found if _RISK_TO_RULE[k] == ACC
    )
    assert result.listing_maintenance_risk.detected_keywords == tuple(
        k for k in old_found if _RISK_TO_RULE[k] == "listing_maintenance_risk"
    )


# --- 2.2 major_scandal / listing_maintenance_risk の格子(危険な言葉 × 確認の言葉 × 配置) -------


def _layouts(risk: str, word: str) -> list[tuple[str, list[Disclosure], bool, bool]]:
    """(配置の名前, 開示, 確認になるべきか, 否定のためだけに格上げされなかったか)。"""
    return [
        ("same_sentence", [_d(f"{risk} {word}")], True, False),
        ("risk_title_word_summary", [_d(risk, f"{word}について")], True, False),
        ("word_title_risk_summary", [_d(word, f"{risk}について")], True, False),
        ("both_in_summary", [_d("標題", f"{risk}。{word}")], True, False),
        (
            "unrelated_sentence_negated",
            [_d(risk, f"{word}について。別件は該当しません")],
            True,
            False,
        ),
        ("negated_word", [_d(risk, f"{word}は認められませんでした")], False, True),
        ("negated_risk", [_d(f"{risk}に該当しません", f"{word}について")], False, True),
        ("separate_disclosures", [_d(risk), _d(f"{word}について")], False, False),
        ("risk_only", [_d(risk)], False, False),
    ]


_TWO_STAGE_CELLS = [
    (rule, risk, word)
    for rule in _TWO_STAGE
    for risk in _RISK_OF_RULE[rule]
    for word in _TWO_STAGE_WORDS
]


@pytest.mark.parametrize(("rule", "risk", "word"), _TWO_STAGE_CELLS)
def test_two_stage_grid_every_cell_has_both_a_positive_and_a_negative_case(
    rule: str, risk: str, word: str
) -> None:
    outcomes: set[bool] = set()
    for name, disclosures, confirmed, blocked in _layouts(risk, word):
        result = dc.assess_disclosure_confirmation(disclosures)
        assessment = getattr(result, rule)
        assert (assessment.level is TS_CONFIRMED) is confirmed, name
        assert assessment.blocked_by_negation is blocked, name
        assert assessment.detected_keywords == (risk,), name
        assert assessment.confirmed_pairs == (((risk, word),) if confirmed else ()), name
        # 他の rule は、この入力で動かない(検出も確認もしない)
        for other in _RULES:
            if other != rule:
                assert getattr(result, other).level.value == "NONE", (name, other)
        outcomes.add(confirmed)
    assert outcomes == {True, False}


def test_two_stage_confirmation_word_alone_detects_nothing() -> None:
    for word in _TWO_STAGE_WORDS:
        result = _assess(_d(f"{word}について"))
        assert result.major_scandal.level is TS_NONE
        assert result.listing_maintenance_risk.level is TS_NONE
        assert result.accounting_problem.level is ACC_NONE


@pytest.mark.parametrize("word", _TWO_STAGE_WORDS)
def test_fn5_continuing_enterprise_alone_is_detected_only(word: str) -> None:
    """C1: 『継続企業』は確認の言葉ではない。他の確認の言葉があるときだけ確認になる。"""
    risk = "継続企業の前提に関する重要事象"
    assert _two("listing_maintenance_risk", _d(risk)).level is TS_DETECTED
    assert _two("listing_maintenance_risk", _d(risk, "継続企業")).level is TS_DETECTED
    assert _two("listing_maintenance_risk", _d(risk, f"継続企業。{word}")).level is TS_CONFIRMED


def test_c1_self_satisfaction_guard_is_generic_not_special_cased() -> None:
    """危険な言葉の出現範囲と重なる確認の言葉の出現は、確認の言葉の表に『継続企業』を戻しても
    数えない。範囲の外にもう 1 つ出現があれば数える。"""
    words = {rule: (*_TWO_STAGE_WORDS, "継続企業") for rule in _TWO_STAGE}
    rules = dataclasses.replace(dc.DEFAULT_RULES, confirmation_words=MappingProxyType(words))
    risk = "継続企業の前提に関する重要事象"
    overlapped = dc.assess_disclosure_confirmation([_d(risk)], rules)
    assert overlapped.listing_maintenance_risk.level is TS_DETECTED
    outside = dc.assess_disclosure_confirmation([_d(risk, "継続企業について")], rules)
    assert outside.listing_maintenance_risk.level is TS_CONFIRMED
    assert outside.listing_maintenance_risk.confirmed_pairs == ((risk, "継続企業"),)


def test_negation_applies_to_both_sides_symmetrically_q2() -> None:
    risk_negated = _two("listing_maintenance_risk", _d("上場廃止基準に該当しません。経営陣の責任"))
    assert risk_negated.level is TS_DETECTED and risk_negated.blocked_by_negation
    word_negated = _two("listing_maintenance_risk", _d("上場廃止基準。経営陣の責任は認めません"))
    assert word_negated.level is TS_DETECTED and word_negated.blocked_by_negation


def test_a_detected_keyword_stays_detected_even_when_negated() -> None:
    """B2: 否定された出現は格上げに数えないが、検出は残す。"""
    assessment = _two("major_scandal", _d("第三者委員会は設置しません。決算訂正はありません"))
    assert assessment.detected_keywords == ("第三者委員会",)
    assert assessment.level is TS_DETECTED


def test_known_limit_fn8_one_event_split_into_two_disclosures_is_not_confirmed() -> None:
    """KNOWN_LIMIT: 同じ出来事が 2 件の開示に分かれると結びつかない(A1)。『保証』ではない。"""
    assessment = _two("major_scandal", _d("第三者委員会の設置"), _d("決算訂正のお知らせ"))
    assert assessment.level is TS_DETECTED


def test_known_weakness_generic_words_still_confirm_the_two_stage_rules() -> None:
    """範囲外の既知の弱さ: 8 語は通常の開示にも現れる。USER の方針(A1・B2・C1)は 8 語の
    扱いを変えない。現状どおり確認になることを固定し、PR 本文に既知の限界として記録する。"""
    assert (
        _two("listing_maintenance_risk", _d("上場廃止基準。上場維持に努めます")).level
        is TS_CONFIRMED
    )
    assert _two("major_scandal", _d("第三者委員会。監査意見について")).level is TS_CONFIRMED


def test_non_assertive_filter_is_not_applied_to_the_two_stage_rules() -> None:
    """非断定(仮定・将来)の除外は会計だけ。8 語の rule へ範囲を広げない(方針は A1・B2・C1)。"""
    for phrase in dc.DEFAULT_NON_ASSERTIVE_PHRASES:
        assessment = _two("major_scandal", _d(f"第三者委員会。決算訂正{phrase}"))
        assert assessment.level is TS_CONFIRMED, phrase


@pytest.mark.parametrize("phrase", dc.DEFAULT_NEGATION_PHRASES)
def test_every_negation_phrase_blocks_the_upgrade_and_keeps_the_detection(phrase: str) -> None:
    sentence = f"決算訂正は{phrase}"
    control = _two("major_scandal", _d("第三者委員会", "決算訂正は行われました"))
    assert control.level is TS_CONFIRMED
    blocked = _two("major_scandal", _d("第三者委員会", sentence))
    assert blocked.level is TS_DETECTED and blocked.blocked_by_negation
    assert blocked.detected_keywords == ("第三者委員会",)
    elsewhere = _two("major_scandal", _d("第三者委員会", f"決算訂正について。別件は{phrase}"))
    assert elsewhere.level is TS_CONFIRMED


@pytest.mark.parametrize("phrase", ["該当ありません", "否定されました", "認められておりません"])
def test_negation_forms_named_by_the_independent_review_do_not_upgrade(phrase: str) -> None:
    """独立確認の観点(『該当ありません』『否定されました』のような言い回し)が格上げ側へ倒れない。"""
    for word in _TWO_STAGE_WORDS:
        assert _two("major_scandal", _d("第三者委員会", f"{word}は{phrase}")).level is TS_DETECTED


# --- 2.3 会計(D'α): 4 語の限定・成立 / 不成立 / 否定 / 非断定 / 別開示 -----------------------

_EXPRESSIONS: dict[str, dict[str, list[str]]] = {
    "決算訂正": {
        "positive": [
            "過年度の決算を、会計処理の誤りにより訂正します",
            "誤りのため訂正報告書を提出しました",
            "決算訂正を行います(会計処理に不適切な点があったため)",
            "検討の結果、過年度の決算を会計処理の誤りにより訂正します",
            "過年度の決算を、会計処理の誤りにより訂正する予定です",
        ],
        "negated": [
            "過年度の決算を訂正する誤りは認められませんでした",
            "過年度の決算の訂正は行いません(誤りのため)",
        ],
        "non_assertive": [
            "過年度の決算に誤りがあった場合は訂正します",
            "過年度の決算を訂正する可能性があります(誤りの有無を検討中)",
            "過年度の決算を、会計処理の誤りにより訂正する方針です",
        ],
        "generic": [
            "記載事項の一部を訂正します(誤記)",
            "過年度の決算に変更はありません",
            "決算の訂正の手続を定めています",
            "決算訂正の要否を検討しています",
        ],
    },
    "監査意見": {
        "positive": [
            "会計監査人は意見不表明としました",
            "限定付適正意見を表明しました",
            "不適正意見となりました",
            "監査意見の不表明となりました",
        ],
        "negated": [
            "意見不表明には該当しません",
            "限定付適正意見は表明しません",
        ],
        "non_assertive": [
            "意見不表明となる可能性があります",
            "限定付適正意見となるおそれがあります",
        ],
        "generic": [
            "無限定適正意見です",
            "監査意見については別途ご案内します",
            "適正意見を得ています",
        ],
    },
    "不正の事実": {
        "positive": [
            "不正の事実が認められました",
            "不正の事実が認められた",
            "不正の事実が判明しました",
            "不正の事実が認定されました",
            "不正の事実が確認されました",
            "調査の結果、不正の事実が認められました",
            "不正の事実が認められましたことを、お知らせする次第です",
        ],
        "negated": [
            "不正の事実は認められませんでした",
            "不正の事実は判明していません",
            "不正の事実は確認されておりません",
            "不正の事実は認定しません",
        ],
        "non_assertive": [
            "不正の事実が判明した場合は速やかに開示します",
            "不正の事実が認められた場合には、関係者の責任を追及します",
            "不正の事実が判明し次第、お知らせします",
            "不正の事実が認められれば、決算の訂正を行います",
            "不正の事実が判明したとみられます",
            "不正の事実が判明したという懸念があります",
            "不正の事実が判明する可能性があります",
            "不正の事実の有無について調査中です",
        ],
        "generic": [
            "不正の事実がないよう内部統制を整備します",
            "不正の事実の有無を点検します",
            "不正の事実の有無を調査します",
            "不正の事実に関する一般的な説明です",
        ],
    },
    "経営陣の責任": {
        "positive": [
            "旧経営陣の責任を認め、役員報酬を減額します",
            "経営陣の責任を追及します",
            "経営陣の責任を取り、代表取締役が辞任します",
        ],
        "negated": [
            "経営陣の責任は認めません",
            "経営陣の責任を追及しません",
            "経営陣の責任を認めておりません",
        ],
        "non_assertive": [
            "経営陣の責任を認める場合があります",
            "経営陣の責任を追及する可能性があります",
        ],
        "generic": [
            "経営陣の責任において実施します",
            "責任と権限を定めます",
        ],
    },
}
_FRAUD_KEY = "不正の事実"
_EXPR_OBJECTS = {e.key: e for e in dc.DEFAULT_ACCOUNTING_EXPRESSIONS}
_ACC_CELLS = [(risk, key) for risk in _ACC_RISKS for key in _EXPRESSIONS]


def test_accounting_expression_table_covers_exactly_the_four_approved_words() -> None:
    assert (
        set(_EXPR_OBJECTS)
        == set(_EXPRESSIONS)
        == {
            "決算訂正",
            "監査意見",
            "不正の事実",
            "経営陣の責任",
        }
    )
    assert [k for k, e in _EXPR_OBJECTS.items() if e.is_fraud_fact] == [_FRAUD_KEY]


@pytest.mark.parametrize(("risk", "key"), _ACC_CELLS)
def test_accounting_grid_every_cell_has_both_a_positive_and_a_negative_case(
    risk: str, key: str
) -> None:
    table = _EXPRESSIONS[key]
    expression = _EXPR_OBJECTS[key]
    expected_level = FRAUD if key == _FRAUD_KEY else SERIOUS
    # 成立(同じ開示。別の文 / 同じ文 / title・summary のどちらにあっても)
    for sentence in table["positive"]:
        assert expression.satisfied_by(sentence), sentence
        for disclosures in (
            [_d(risk, sentence)],
            [_d(sentence, risk)],
            [_d("標題", f"{risk}。{sentence}")],
            [_d(f"{risk}の件で、{sentence}")],
            [_d(risk, f"{sentence}。別件は該当しません")],  # 別の文の否定は影響しない
            [_d(risk, f"{sentence}。別件は調査中です")],  # 別の文の非断定は影響しない
            [_d(risk, f"{sentence}。 別件は、場合によります")],
        ):
            got = _acc(*disclosures)
            assert got.level is expected_level, (sentence, disclosures)
            assert got.serious_pairs == ((risk, key),)
            assert got.fraud_pairs == (((risk, key),) if key == _FRAUD_KEY else ())
    # 不成立(否定 / 非断定 / 一般的な記載 / 別の開示): 検出は残り、確認にならない
    for sentence in table["negated"]:
        got = _acc(_d(risk, sentence))
        assert got.level is ACC_DETECTED, sentence
        assert got.blocked_by_negation is expression.satisfied_by(sentence), sentence
        assert not got.blocked_by_non_assertive, sentence
    for sentence in table["non_assertive"]:
        got = _acc(_d(risk, sentence))
        assert got.level is ACC_DETECTED, sentence
        assert got.blocked_by_non_assertive is expression.satisfied_by(sentence), sentence
        assert not got.blocked_by_negation, sentence
    for sentence in table["generic"]:
        got = _acc(_d(risk, sentence))
        assert got.level is ACC_DETECTED, sentence
        assert not got.blocked_by_negation and not got.blocked_by_non_assertive, sentence
        assert got.detected_keywords == (risk,)
    for sentence in table["positive"]:
        got = _acc(_d(risk), _d(sentence))
        assert got.level is ACC_DETECTED, sentence
        assert not got.blocked_by_negation and not got.blocked_by_non_assertive


@pytest.mark.parametrize("risk", _ACC_RISKS)
def test_accounting_negation_applies_to_the_risk_side_too_q2(risk: str) -> None:
    """Q-2(危険な言葉側の否定): 危険な言葉の文が否定なら、確認の表現が肯定でも格上げしない。"""
    got = _acc(_d(f"{risk}に該当しません", "不正の事実が認められました"))
    assert got.level is ACC_DETECTED and got.blocked_by_negation
    assert got.detected_keywords == (risk,)
    unrelated = _acc(_d(risk, "不正の事実が認められました。別件は該当しません"))
    assert unrelated.level is FRAUD


def test_a_dangling_open_bracket_does_not_leak_across_a_newline() -> None:
    summary = "(注\n不正の事実が認められました。不正の事実が判明した場合は開示します"
    assert _acc(_d("不適切な会計処理", summary)).level is FRAUD


def test_blocked_flags_are_true_for_at_least_one_example_of_each_blocker() -> None:
    assert _acc(_d("不適切な会計処理", "不正の事実は判明していません")).blocked_by_negation
    assert _acc(
        _d("不適切な会計処理", "不正の事実が判明した場合は開示します")
    ).blocked_by_non_assertive


@pytest.mark.parametrize("phrase", dc.DEFAULT_NON_ASSERTIVE_PHRASES)
def test_every_non_assertive_phrase_blocks_only_its_own_sentence(phrase: str) -> None:
    risk = "不適切な会計処理"
    control = _acc(_d(risk, "不正の事実が判明しました"))
    assert control.level is FRAUD
    blocked = _acc(_d(risk, f"不正の事実が判明しました({phrase})"))
    assert blocked.level is ACC_DETECTED and blocked.blocked_by_non_assertive
    assert blocked.detected_keywords == (risk,)
    elsewhere = _acc(_d(risk, f"不正の事実が判明しました。別件は({phrase})"))
    assert elsewhere.level is FRAUD


def test_fn_c1_a_conditional_sentence_neither_counts_nor_cancels_a_definite_one() -> None:
    risk = "不適切な会計処理"
    conditional = "不正の事実が判明した場合は開示します"
    definite = "不正の事実が認められました"
    for summary in (f"{conditional}。{definite}", f"{definite}。{conditional}"):
        got = _acc(_d(risk, summary))
        assert got.level is FRAUD and got.fraud_pairs == ((risk, _FRAUD_KEY),), summary
    assert _acc(_d(risk, conditional)).level is ACC_DETECTED
    # title と summary は別の文
    assert _acc(_d(conditional, f"{risk}。{definite}")).level is FRAUD


def test_boundary_scheduled_counts_but_policy_does_not() -> None:
    """『予定』の境界(USER 向けに明示): 決定を知らせる『〜する予定です』は確定の開示でありうるため
    確認に数える。『〜する方針です』は意図の表明で、確定の前でありうるため数えない(格上げしない側)。"""
    risk = "不適切な会計処理"
    scheduled = "過年度の決算を、会計処理の誤りにより訂正する予定です"
    policy = "過年度の決算を、会計処理の誤りにより訂正する方針です"
    assert _acc(_d(risk, scheduled)).level is SERIOUS
    assert _acc(_d(risk, policy)).level is ACC_DETECTED
    assert _acc(_d(risk, policy)).blocked_by_non_assertive


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        pytest.param("不正の事実が認められました", FRAUD, id="no_full_stop"),
        pytest.param(
            "不正の事実が判明した場合は開示します。不正の事実が認められました",
            FRAUD,
            id="full_stop",
        ),
        pytest.param(
            "不正の事実が判明した場合は開示します\n不正の事実が認められました",
            FRAUD,
            id="newline_only",
        ),
        pytest.param(
            "不正の事実が判明した場合は開示します\r\n不正の事実が認められました", FRAUD, id="crlf"
        ),
        pytest.param(
            "不正の事実が認められました\n不正の事実が判明した場合は開示します",
            FRAUD,
            id="newline_reversed",
        ),
        pytest.param(
            "不正の事実が判明した(詳細は別紙。)場合は開示します",
            ACC_DETECTED,
            id="period_in_parentheses_keeps_the_condition",
        ),
        pytest.param("(不正の事実が認められました。)", FRAUD, id="definite_inside_parentheses"),
        pytest.param("不正の事実が認められました(詳細は別紙。)", FRAUD, id="trailing_parenthesis"),
        pytest.param(
            "「不正の事実が判明した。」場合は開示します",
            ACC_DETECTED,
            id="period_in_quote_keeps_the_condition",
        ),
        pytest.param(
            "不正の事実が判明した場合は開示しますが、不正の事実が認められました",
            ACC_DETECTED,
            id="KNOWN_LIMIT_comma_joined_condition_and_definite",
        ),
        pytest.param(
            "(注 不正の事実が判明した場合は。不正の事実が認められました",
            ACC_DETECTED,
            id="KNOWN_LIMIT_unclosed_parenthesis_merges_the_following",
        ),
        pytest.param(
            "(注 不正の事実が判明した場合は。\n不正の事実が認められました",
            FRAUD,
            id="unclosed_parenthesis_ends_at_newline",
        ),
    ],
)
def test_sentence_boundary_expectations(summary: str, expected: dc.AccountingLevel) -> None:
    """文の区切りの期待値の表(サブちゃんの独立確認の観点)。『KNOWN_LIMIT』の行は、格上げしない側
    (見逃し方向)に倒れる事実の固定であり、望ましい挙動という意味ではない。"""
    assert _acc(_d("不適切な会計処理", summary)).level is expected


def test_known_limit_restatement_split_across_two_sentences_is_detected_only() -> None:
    """KNOWN_LIMIT(見逃し方向): 『過年度の訂正』と『誤りの理由』が別の文にあると、決算訂正の限定
    (同じ文に過年度・訂正・誤りが揃う)を満たさず、確認要止まりになる(検出は残る)。同じ内容を
    1 文で書けば A になる。限定を開示単位へ広げるかは PR review での判断(USER)。"""
    risk = "不適切な会計処理"
    split = "過年度の決算を訂正します。会計処理の誤りによるものです"
    one_sentence = "過年度の決算を、会計処理の誤りにより訂正します"
    assert _acc(_d(risk, split)).level is ACC_DETECTED
    assert _acc(_d(risk, one_sentence)).level is SERIOUS


# --- 2.3b MUST-1(HANAKO の PR 前 review): 危険な言葉の中の語で限定を満たさない -----------------

_ISSUE_TOPICS = [
    "不適切な会計処理に関する過年度決算の訂正の要否について",
    "不適切な会計処理について、過年度の決算を訂正するか検討します",
    "不適切な会計処理の有無と過年度の訂正の要否を調査します",
]


@pytest.mark.parametrize("text", _ISSUE_TOPICS)
def test_must1_a_restatement_topic_is_not_a_restatement_fact(text: str) -> None:
    """危険な言葉の『不適切』で限定を満たさず、検討・調査・要否・有無の語で非断定になる。"""
    for disclosure in ([_d(text)], [_d("標題", text)], [_d(text, "本文")]):
        got = _acc(*disclosure)
        assert got.level is ACC_DETECTED, text
        assert got.detected_keywords == ("不適切な会計処理",)


@pytest.mark.parametrize("risk", _ACC_RISKS)
@pytest.mark.parametrize(
    "template",
    [
        "{risk}に関する過年度の決算の訂正について",
        "{risk}を受けた過年度の決算の訂正を行います",
        "過年度の決算の訂正と{risk}",
        "{risk}。過年度の決算を訂正します",
    ],
)
def test_must1_risk_keyword_plus_generic_words_never_makes_a_restatement(
    risk: str, template: str
) -> None:
    """property: 危険な言葉 + 『過年度』『訂正』という一般的な語だけでは A にならない
    (危険な言葉の出現範囲の語は、限定の語として数えない)。"""
    assert _acc(_d(template.format(risk=risk))).level is ACC_DETECTED


def test_must1_masking_is_what_prevents_the_self_satisfaction() -> None:
    expression = _EXPR_OBJECTS["決算訂正"]
    sentence = "不適切な会計処理に関する過年度決算の訂正について"
    assert expression.satisfied_by(sentence) is True  # 危険な言葉を知らない素の判定では満たす
    assert expression.satisfied_by(sentence, tuple(dc.RISK_KEYWORD_TO_RULE)) is False
    # 危険な言葉の外にある『不適切』『誤り』は数える
    outside = "不適切な会計処理に関する過年度決算を、誤りにより訂正します"
    assert expression.satisfied_by(outside, tuple(dc.RISK_KEYWORD_TO_RULE)) is True


def test_must1_removing_the_risk_words_own_wording_keeps_the_restatement_a() -> None:
    """危険な言葉の『不適切』を取り除いても、同じ訂正の文(誤りによる)は A のまま。"""
    for risk in _ACC_RISKS:
        base = "過年度の決算を、会計処理の誤りにより訂正します"
        assert _acc(_d(risk, base)).level is SERIOUS
        assert _acc(_d(f"{risk}。{base}")).level is SERIOUS
        assert _acc(_d(f"{risk}に伴い、{base}")).level is SERIOUS


def test_must1_correction_report_pairs_with_the_accounting_risk_word_by_design() -> None:
    """限定の語が危険な言葉そのものの場合(訂正報告書 + 『不適切な会計処理』)は、その出現を数える
    (意図: 訂正報告書と危険な言葉の同伴)。他の危険な言葉・非断定の文は A にならない。"""
    assert _acc(_d("不適切な会計処理に関する訂正報告書を提出しました")).level is SERIOUS
    assert _acc(_d("内部統制上の重要な不備に関する訂正報告書を提出しました")).level is ACC_DETECTED
    assert (
        _acc(_d("不適切な会計処理に関する訂正報告書の提出の要否を検討します")).level is ACC_DETECTED
    )
    assert _acc(_d("不適切な会計処理。誤りのため訂正報告書を提出しました")).level is SERIOUS


_INQUIRY_PAIRS = [
    ("要否", "過年度の決算の誤りによる訂正の要否について"),
    ("有無", "過年度の決算の誤りによる訂正の有無について"),
    ("適否", "過年度の決算の誤りによる訂正の適否について"),
    ("検討します", "過年度の決算の誤りによる訂正を行うか検討します"),
    ("調査します", "過年度の決算の誤りによる訂正を行うか調査します"),
]


@pytest.mark.parametrize(("word", "sentence"), _INQUIRY_PAIRS)
def test_must1_each_inquiry_word_alone_turns_an_otherwise_complete_restatement_into_a_topic(
    word: str, sentence: str
) -> None:
    """限定(過年度・訂正・誤り)が危険な言葉の外で揃っていても、論点・調査・検討の語を含む文は
    事実の確定ではない。各語を 1 つずつ外すと落ちるよう、文には当該の語だけを置く。"""
    others = [p for p in dc.DEFAULT_NON_ASSERTIVE_PHRASES if p != word and p in sentence]
    assert word in sentence and not others, (word, others)
    assert _EXPR_OBJECTS["決算訂正"].satisfied_by(sentence)
    got = _acc(_d("不適切な会計処理", sentence))
    assert got.level is ACC_DETECTED and got.blocked_by_non_assertive, word
    # 対: 同じ限定の確定の文(論点の語なし)は A
    assert _acc(_d("不適切な会計処理", "過年度の決算の誤りによる訂正を行います")).level is SERIOUS


_HEARSAY_PAIRS = [
    ("不明", "不正の事実が認められたとの見方もあり、真偽は不明です"),
    ("との報道", "不正の事実が認められたとの報道があります"),
    ("旨の報道", "不正の事実が認められた旨の報道があります"),
    ("との情報", "不正の事実が認められたとの情報があります"),
    ("とのこと", "不正の事実が認められたとのことです"),
]


@pytest.mark.parametrize(("word", "sentence"), _HEARSAY_PAIRS)
def test_hearsay_and_unknown_truth_are_not_a_confirmed_fact(word: str, sentence: str) -> None:
    others = [p for p in dc.DEFAULT_NON_ASSERTIVE_PHRASES if p != word and p in sentence]
    assert word in sentence and not others, (word, others)
    assert _EXPR_OBJECTS["不正の事実"].satisfied_by(sentence)
    got = _acc(_d("内部統制上の重要な不備", sentence))
    assert got.level is ACC_DETECTED and got.blocked_by_non_assertive, word
    assert _acc(_d("内部統制上の重要な不備", "不正の事実が認められました")).level is FRAUD


@pytest.mark.parametrize(
    "sentence",
    [
        "不正の事実が認められたことに間違いありません",
        "不正の事実が認められたことは疑いの余地はありません",
    ],
)
def test_known_limit_double_negation_in_a_real_confirmation_stays_detected(sentence: str) -> None:
    """KNOWN_LIMIT(見逃し方向。HANAKO S-2): 二重否定(『間違いありません』『疑いの余地はありません』)
    は『ありません』『疑い』で非確認に倒れる。二重否定・複雑な構文は誤りうる(module の docstring)。
    否定と非断定の両方に当たる文は、どちらか一方を外しても確認にならないため blocked_by_* は立たない
    (blocked_by_* は『その条件だけを外せば確認になる』の意味)。"""
    got = _acc(_d("内部統制上の重要な不備", sentence))
    assert got.level is ACC_DETECTED


def test_title_and_summary_are_separate_sentences() -> None:
    """title の仮定の語が、summary の確定の文へ及ばない(別の文)。"""
    got = _acc(_d("不正の事実が判明した場合", "不適切な会計処理 不正の事実が認められました"))
    assert got.level is FRAUD
    got_reverse = _acc(
        _d("不正の事実が認められました 不適切な会計処理", "判明した場合は開示します")
    )
    assert got_reverse.level is FRAUD


def test_known_limit_risk_words_own_wording_cannot_supply_the_error_evidence() -> None:
    """KNOWN_LIMIT(見逃し方向): 『不適切な会計処理により過年度の決算を訂正します』は、誤りの語が
    危険な言葉の中の『不適切』しか無いため A にならない(確認要止まり。検出は残る)。A は hard gate の
    入力ではない(提案は B のみ)。誤りを別の語で書けば A になる(上のテスト)。"""
    got = _acc(_d("不適切な会計処理により過年度の決算を訂正します"))
    assert got.level is ACC_DETECTED and not got.blocked_by_non_assertive


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("不正の事実の有無を調査した結果、不正の事実が認められました", "有無"),
        ("訂正の要否を検討した結果、過年度の決算を会計処理の誤りにより訂正します", "要否"),
    ],
)
def test_known_limit_a_result_report_containing_an_inquiry_word_is_not_counted(
    text: str, label: str
) -> None:
    """KNOWN_LIMIT(見逃し方向): 『有無』『要否』は同じ文の確定を巻き込む。『調査の結果』だけの
    確定の報告は残る(P-12)。結果の報告に論点の語が同居する形は確認にならない。"""
    got = _acc(_d("不適切な会計処理", text))
    assert got.level is ACC_DETECTED and got.blocked_by_non_assertive, label


# --- 2.3c MUST-2(HANAKO): 本物の不正の事実が、よくある書き方で B にならない(既知の限界) ---

_MUST2_INPUTS = [
    pytest.param(
        "内部統制上の重要な不備があり、不正の事実が認められたため、再発防止策を講じる方針です",
        {"blocked_by_non_assertive": True, "blocked_by_negation": False},
        id="fact_plus_policy_clause",
    ),
    pytest.param(
        "内部統制上の重要な不備があり、不正の事実が認められましたが、他の役職員の関与は認められておりません",
        {"blocked_by_non_assertive": False, "blocked_by_negation": True},
        id="fact_plus_contrast_negation_clause",
    ),
]


@pytest.mark.parametrize(("text", "flags"), _MUST2_INPUTS)
def test_known_limit_must2_a_real_fraud_fact_in_a_multi_clause_sentence_stays_detected(
    text: str, flags: dict[str, bool]
) -> None:
    """KNOWN_LIMIT(見逃し方向。PR 本文の既知の限界の先頭): 文を読点で区切らない設計(文単位)のため、
    同じ文の別の節にある将来の方針・否定が、過去の確定を打ち消す。暫定の既定は現行(文単位)のまま。
    節単位に変える・『方針』『見通し』を非断定から外す、の選択肢は PR 本文に比較表を載せ、
    USER が review で選ぶ。選択が変わったときは、このテストを意図して更新する。"""
    got = _acc(_d(text))
    assert got.level is ACC_DETECTED
    assert got.fraud_pairs == () and got.serious_pairs == ()
    assert got.blocked_by_non_assertive is flags["blocked_by_non_assertive"]
    assert got.blocked_by_negation is flags["blocked_by_negation"]
    # 同じ内容を節ごとに文へ分ければ B になる(文の区切りだけが原因であることの確認)
    split = text.replace("、", "。")
    assert _acc(_d(split)).level is FRAUD


def test_two_stage_negation_in_a_comma_joined_sentence_is_a_known_limit() -> None:
    """KNOWN_LIMIT: 読点で繋いだ 1 文の中に否定と確定が同居すると、その文の出現は数えない
    (格上げしない側)。"""
    got = _two(
        "major_scandal",
        _d("第三者委員会。決算訂正は認められませんでしたが、経営陣の責任を重く受け止めます"),
    )
    assert got.level is TS_DETECTED and got.blocked_by_negation


@pytest.mark.parametrize(
    "sentence",
    ["不正の事実が判明したとみられます", "不正の事実が判明したという懸念があります"],
)
def test_unlisted_non_assertive_forms_do_not_lean_to_upgrade(sentence: str) -> None:
    for risk in _ACC_RISKS:
        assert _acc(_d(risk, sentence)).level is ACC_DETECTED


# --- 2.4 会計の 2 区分(A: 重大な会計上の問題 / B: 会計不正の事実。B ⊂ A) ---------------------


def _first_positive(key: str) -> str:
    return _EXPRESSIONS[key]["positive"][0]


_KEYS = sorted(_EXPRESSIONS)


@pytest.mark.parametrize("risk", _ACC_RISKS)
@pytest.mark.parametrize("size", range(5))
def test_fn_a3_a5_category_matrix_over_every_subset_of_the_four_expressions(
    risk: str, size: int
) -> None:
    for subset in itertools.combinations(_KEYS, size):
        summary = "。".join(_first_positive(k) for k in subset)
        got = _acc(_d(risk, summary))
        assert got.serious_pairs == tuple(sorted((risk, k) for k in subset)), subset
        fraud_expected = _FRAUD_KEY in subset
        assert got.fraud_pairs == (((risk, _FRAUD_KEY),) if fraud_expected else ()), subset
        if fraud_expected:
            assert got.level is FRAUD
        elif subset:
            assert got.level is SERIOUS
        else:
            assert got.level is ACC_DETECTED
        assert set(got.fraud_pairs) <= set(got.serious_pairs)


def test_fn_a1_serious_but_not_fraud_is_not_missed_and_not_mixed_up() -> None:
    """『不正ではないが重大』(意見不表明など)は、確認になるが不正ではない。"""
    for sentence in (
        "会計監査人は意見不表明としました",
        "限定付適正意見を表明しました",
        "過年度の決算を、会計処理の誤りにより訂正します",
        "旧経営陣の責任を認め、役員報酬を減額します",
    ):
        got = _acc(_d("内部統制上の重要な不備", sentence))
        assert got.level is SERIOUS and got.fraud_pairs == (), sentence


def test_fn_a2_fraud_is_not_missed() -> None:
    for sentence in _EXPRESSIONS[_FRAUD_KEY]["positive"]:
        got = _acc(_d("内部統制上の重要な不備", sentence))
        assert got.level is FRAUD and got.serious_pairs == got.fraud_pairs, sentence


def test_bare_internal_control_weakness_is_detected_only_q5() -> None:
    """Q-5(範囲外): 危険な言葉だけでは A にならない(確認の表現の同伴を要する)。検出は残る。"""
    got = _acc(_d("内部統制上の重要な不備"))
    assert got.level is ACC_DETECTED and got.serious_pairs == ()


def test_c2_fraud_fact_alone_without_an_accounting_risk_word_is_none() -> None:
    """C-2: 『不正の事実』単独(会計の危険な言葉なし)は A にも B にもならない。他の rule の危険な語も
    無ければ、どの rule にも拾われない(既知の限界。検出語の集合に足すかは USER 判断)。"""
    result = _assess(_d("不正の事実が認められました"))
    assert result.accounting_problem.level is ACC_NONE
    assert result.accounting_problem.detected_keywords == ()
    assert result.major_scandal.level is TS_NONE
    assert result.listing_maintenance_risk.level is TS_NONE
    mixed = _assess(_d("第三者委員会", "不正の事実が認められました"))
    assert mixed.accounting_problem.level is ACC_NONE
    assert mixed.major_scandal.level is TS_CONFIRMED  # 8 語の rule では確認の言葉


def test_accounting_confirmation_requires_the_risk_word_in_the_same_disclosure() -> None:
    got = _acc(_d("不適切な会計処理"), _d("不正の事実が認められました"))
    assert got.level is ACC_DETECTED


def test_accounting_both_risk_keywords_pair_independently() -> None:
    got = _acc(_d("不適切な会計処理 内部統制上の重要な不備", "意見不表明としました"))
    assert got.serious_pairs == (
        ("不適切な会計処理", "監査意見"),
        ("内部統制上の重要な不備", "監査意見"),
    )


# --- 2.5 結果の不変条件(構築時に検証) ---------------------------------------------------


def _acc_kwargs(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "detected_keywords": ("不適切な会計処理",),
        "level": ACC_DETECTED,
        "serious_pairs": (),
        "fraud_pairs": (),
        "blocked_by_negation": False,
        "blocked_by_non_assertive": False,
    }
    base.update(over)
    return base


_PAIR = ("不適切な会計処理", "不正の事実")
_PAIR2 = ("不適切な会計処理", "監査意見")


@pytest.mark.parametrize(
    "over",
    [
        {"level": FRAUD},  # B なのに fraud_pairs が空
        {"level": FRAUD, "serious_pairs": (_PAIR2,), "fraud_pairs": (_PAIR,)},  # 非包含
        {"level": SERIOUS, "serious_pairs": (_PAIR,), "fraud_pairs": (_PAIR,)},  # A なのに B が非空
        {"level": SERIOUS},  # A なのに pairs が空
        {"serious_pairs": (_PAIR,)},  # 確認でないのに pairs
        {"level": ACC_NONE},  # NONE なのに検出
        {"detected_keywords": ()},  # DETECTED なのに検出なし
        {"level": SERIOUS, "serious_pairs": (_PAIR2,), "blocked_by_negation": True},
        {"detected_keywords": ("不適切な会計処理", "不適切な会計処理")},
        {"level": SERIOUS, "serious_pairs": (_PAIR2, _PAIR)},  # 未ソート
    ],
)
def test_accounting_assessment_rejects_inconsistent_construction(over: dict[str, object]) -> None:
    with pytest.raises(dc.ConfirmationContractError):
        dc.AccountingAssessment(**_acc_kwargs(**over))  # type: ignore[arg-type]


def test_accounting_assessment_accepts_the_consistent_shapes() -> None:
    dc.AccountingAssessment(**_acc_kwargs())  # type: ignore[arg-type]
    dc.AccountingAssessment(
        **_acc_kwargs(level=ACC_NONE, detected_keywords=())  # type: ignore[arg-type]
    )
    dc.AccountingAssessment(
        **_acc_kwargs(level=SERIOUS, serious_pairs=(_PAIR, _PAIR2))  # type: ignore[arg-type]
    )
    dc.AccountingAssessment(
        **_acc_kwargs(  # type: ignore[arg-type]
            level=FRAUD, serious_pairs=(_PAIR, _PAIR2), fraud_pairs=(_PAIR,)
        )
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"level": TS_NONE, "detected_keywords": ("上場廃止基準",), "confirmed_pairs": ()},
        {"level": TS_DETECTED, "detected_keywords": (), "confirmed_pairs": ()},
        {"level": TS_CONFIRMED, "detected_keywords": ("上場廃止基準",), "confirmed_pairs": ()},
        {
            "level": TS_CONFIRMED,
            "detected_keywords": ("上場廃止基準",),
            "confirmed_pairs": (("上場廃止基準", "監査意見"),),
            "blocked_by_negation": True,
        },
        {"level": TS_DETECTED, "detected_keywords": ("監理銘柄", "上場廃止基準")},
    ],
)
def test_rule_assessment_rejects_inconsistent_construction(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = {
        "rule": "listing_maintenance_risk",
        "blocked_by_negation": False,
        "confirmed_pairs": (),
    }
    base.update(kwargs)
    with pytest.raises(dc.ConfirmationContractError):
        dc.RuleAssessment(**base)  # type: ignore[arg-type]


def test_rules_reject_empty_phrases_and_unknown_rules() -> None:
    defaults = dc.DEFAULT_RULES
    with pytest.raises(ValueError, match="empty phrase"):
        dataclasses.replace(defaults, negation_phrases=("",))
    with pytest.raises(ValueError, match="empty phrase"):
        dataclasses.replace(defaults, non_assertive_phrases=(*defaults.non_assertive_phrases, ""))
    with pytest.raises(ValueError, match="invalid risk keyword"):
        dataclasses.replace(defaults, risk_keyword_to_rule={"語": "unknown_rule"})
    with pytest.raises(ValueError, match="exactly the 2-stage"):
        dataclasses.replace(defaults, confirmation_words={"major_scandal": ("決算訂正",)})
    with pytest.raises(ValueError):
        dc.ConfirmationExpression(key="x", alternatives=())
    with pytest.raises(ValueError):
        dc.ConfirmationExpression(key="x", alternatives=((("",),),))


def test_qualifier_table_is_swappable_data() -> None:
    """限定の表は差し替えられるデータ(USER が review で内容を変えても、判定の骨格は同じ)。"""
    only_audit = dataclasses.replace(
        dc.DEFAULT_RULES,
        accounting_expressions=(_EXPR_OBJECTS["監査意見"],),
    )
    fraud = [_d("不適切な会計処理", "不正の事実が認められました")]
    assert dc.assess_disclosure_confirmation(fraud, only_audit).accounting_problem.level is (
        ACC_DETECTED
    )
    assert dc.assess_disclosure_confirmation(fraud).accounting_problem.level is FRAUD


# --- 2.6 順序・重複に依存しない / 純粋 -----------------------------------------------------


def test_fn6_result_does_not_depend_on_order_or_duplicates() -> None:
    disclosures = [
        _d("第三者委員会 不適切な会計処理"),
        _d("上場廃止基準", "経営陣の責任を認め、役員報酬を減額します"),
        _d("標題", "不正の事実が認められました。内部統制上の重要な不備"),
    ]
    baseline = dc.assess_disclosure_confirmation(disclosures)
    for permutation in itertools.permutations(disclosures):
        assert dc.assess_disclosure_confirmation(list(permutation)) == baseline
    assert dc.assess_disclosure_confirmation([*disclosures, *disclosures]) == baseline
    assert dc.assess_disclosure_confirmation(tuple(disclosures)) == baseline
    assert dc.assess_disclosure_confirmation([]) == dc.assess_disclosure_confirmation(
        [_d("無関係")]
    )


def test_input_is_not_mutated() -> None:
    disclosures = [_d("第三者委員会", "決算訂正について")]
    snapshot = list(disclosures)
    dc.assess_disclosure_confirmation(disclosures)
    assert disclosures == snapshot


def _module_tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def test_module_is_pure_and_isolated() -> None:
    tree = _module_tree()
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    forbidden = (
        "jstock_advisor.services",
        "jstock_advisor.infrastructure",
        "jstock_advisor.lambda_handlers",
        "jstock_advisor.providers",
        "jstock_advisor.config",
        "logging",
        "datetime",
        "time",
        "random",
        "os",
        "boto3",
        "json",
        "pathlib",
    )
    assert not [m for m in imported if m.startswith(forbidden)], imported
    assert not [
        n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)
    ]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"print", "open", "logger", "input", "exec", "eval"}


def test_module_is_not_imported_from_anywhere_in_src() -> None:
    """dormant: 現行の判定・通知・スナップショットへ配線されていない(配線は PR-2)。"""
    needles = ("signals.disclosure_confirmation", "import disclosure_confirmation")
    importers = [
        str(path.relative_to(_REPO))
        for path in _SRC.rglob("*.py")
        if path != _MODULE_PATH
        and any(needle in path.read_text(encoding="utf-8") for needle in needles)
    ]
    assert importers == []


def test_fraud_label_is_produced_in_exactly_one_guarded_place() -> None:
    """2 区分を混ぜない: FRAUD_FACT_CONFIRMED を返すのは has_fraud の分岐だけ。"""
    tree = _module_tree()
    # 『返す』のは 1 か所だけ(__post_init__ の不変条件の検査での参照は、作る側ではない)
    uses = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Return)
        and isinstance(n.value, ast.Attribute)
        and n.value.attr == "FRAUD_FACT_CONFIRMED"
    ]
    assert len(uses) == 1
    function = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_accounting_level"
    )
    first = function.body[0]
    assert isinstance(first, ast.If)
    assert isinstance(first.test, ast.Name) and first.test.id == "has_fraud"
    assert uses[0] in list(ast.walk(first))


# --- 2.7 新 ⊆ 旧(確認が増えない)と、落ちた理由の説明 -------------------------------------------


def _reason_of_drop(name: str, risk: str, word: str) -> str | None:
    """旧が確認で新が確認要に落ちた理由。説明できなければ None(= 理由なしの落ち)。"""
    if name == "separate_disclosures":
        return "A1"
    if name in ("negated_word", "negated_risk"):
        return "B2"
    if word == "継続企業":
        return "C1"
    if name == "risk_only" and "継続企業" in risk:
        return "C1"
    return None


def test_new_confirmed_implies_old_confirmed_and_every_drop_has_a_reason() -> None:
    drops: dict[str, int] = {}
    checked = 0
    for rule in _TWO_STAGE:
        for risk in _RISK_OF_RULE[rule]:
            for word in _OLD_CONFIRMATION_WORDS:
                for name, disclosures, _, _ in _layouts(risk, word):
                    old = _old_classify(disclosures)[rule]
                    new = getattr(dc.assess_disclosure_confirmation(disclosures), rule)
                    checked += 1
                    if new.level is TS_CONFIRMED:
                        assert old is CONFIRMED, (rule, risk, word, name)
                    if old is CONFIRMED and new.level is not TS_CONFIRMED:
                        reason = _reason_of_drop(name, risk, word)
                        assert reason is not None, (rule, risk, word, name)
                        drops[reason] = drops.get(reason, 0) + 1
                    # 検出は常に一致(FN-1)
                    assert (old is not None) == bool(new.detected_keywords), (
                        rule,
                        risk,
                        word,
                        name,
                    )
    assert checked > 400
    assert set(drops) == {"A1", "B2", "C1"}  # 3 つの是正がそれぞれ実際に出力を変える


def test_accounting_new_state_is_new_and_old_never_confirmed() -> None:
    """会計の確認(A / B)は新設の段階: 旧は常に RISK_KEYWORD_DETECTED。検出の有無は一致。"""
    for risk in _ACC_RISKS:
        for table in _EXPRESSIONS.values():
            disclosures = [_d(risk, table["positive"][0])]
            assert _old_classify(disclosures)[ACC] is DETECTED
            assert _acc(*disclosures).level in (SERIOUS, FRAUD)
