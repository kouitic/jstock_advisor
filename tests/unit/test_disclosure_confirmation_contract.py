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

import datetime as dt
from pathlib import Path

import pytest
import yaml

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import DisclosureRiskConfirmationLevel
from jstock_advisor.domain.screening.rules import (
    MATERIAL_EVENT_KEYWORDS,
    detect_disclosure_risk_keywords,
    detect_material_event_keywords,
)
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
