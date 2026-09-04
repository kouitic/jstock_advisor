"""Issue #52 Phase B3-B1: 財務データの報告サイクル鮮度をBUYへ接続する際の契約。

B3-Aで作った`domain/financial_freshness.py`は判定経路から呼ばれていなかった
(call site 0)。B3-B1でBUYへ接続する。ここでは**接続のしかたの契約**を固定する。

## 確定仕様(人間確定。ここで再判断しない)

```
FINANCIAL_REPORTING_LAG_CALENDAR_DAYS = 50   暦日。営業日へ読み替えない
STALE   -> BUYへ警告(反対材料)。hard exclusionしない
UNKNOWN -> 警告なし・減点なし。監査項目としてのみ残す
FRESH   -> 警告なし・減点なし
```

## BUYに減点を入れない理由(OPTION_A)

BUY経路には`ConfidenceFactors`/`compute_confidence()`という共通confidence
scoreが**存在しない**(SELLと利確からのみ呼ばれている)。BUYが持つ
`determine_valuation_confidence()`は**適正価格の算出手法の信頼性**であって
データ鮮度ではないため、そこへ財務鮮度を混ぜると、本Issueの根本原因である
「異なる時間conceptの混同」を別の形で作り直すことになる。

同じ理由で`data_quality_warning`へも合流させない。あちらは取得時刻ベースの
鮮度であり、かつmargin_of_safety・買付価格信頼性・データ品質スコアの3経路へ
波及するため、合流させると「警告のみ」ではなく実質的な減点になる。

SELL/利確側のconfidence penaltyは、既存の`compute_confidence()`経路が実在する
ため**B3-B2**でそちらへ接続する。

## 本moduleが固定しないこと

判定そのものの境界値(domain契約)。それは
`tests/unit/test_issue_52_phase_b3_a_financial_freshness.py`にある。
BUYを実際に通した挙動は`tests/unit/test_buy_signal_service.py`の
`test_b3_b1_*`にある。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import DataQualityRules

_CONFIG = load_config()
_SRC = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_BUY_SERVICE = _SRC / "services" / "buy_signal_service.py"


def test_human_decided_reporting_lag_is_fifty_calendar_days() -> None:
    """人間が確定した猶予日数がconfigへ入っていること。"""
    assert _CONFIG.screening.data_quality.financial_reporting_lag_calendar_days == 50


def test_reporting_lag_is_independent_of_fetch_based_freshness() -> None:
    """取得時刻ベースの鮮度設定と別のキーであること(片方の変更が他方へ波及しない)。"""
    data_quality = _CONFIG.screening.data_quality
    assert data_quality.max_data_age_business_days == 3
    assert (
        data_quality.financial_reporting_lag_calendar_days
        != data_quality.max_data_age_business_days
    )


def test_negative_reporting_lag_is_rejected_at_config_load() -> None:
    """負の猶予日数は設定段階で弾く(判定時のUNKNOWNへ流さない)。"""
    with pytest.raises(ValidationError):
        DataQualityRules(
            max_data_age_business_days=3,
            financial_reporting_lag_calendar_days=-1,
        )


def test_zero_reporting_lag_is_accepted() -> None:
    """0は「期末当日から期限」という意味を持つ有効値であり、弾かない。"""
    rules = DataQualityRules(
        max_data_age_business_days=3,
        financial_reporting_lag_calendar_days=0,
    )
    assert rules.financial_reporting_lag_calendar_days == 0


def test_missing_reporting_lag_fails_fast() -> None:
    """キーが無い設定は起動時に落とす(暗黙のPython既定値で動かさない)。"""
    with pytest.raises(ValidationError):
        DataQualityRules(max_data_age_business_days=3)


def test_buy_uses_the_domain_contract_not_its_own_date_math() -> None:
    """BUYはdomainの判定を呼ぶ。サービス層で期限日を再計算しない。"""
    source = _BUY_SERVICE.read_text(encoding="utf-8")
    assert "evaluate_financial_freshness(" in source
    assert "resolve_latest_financial_period_end(" in source


def test_buy_does_not_introduce_a_confidence_score_path() -> None:
    """BUYへ共通confidence scoreを持ち込まない(OPTION_Aの中核)。

    ここが破れると、財務鮮度の減点がBUYの適正価格信頼度へ混ざる余地ができる。

    コメントへの言及ではなく、実際のimport・呼び出しだけを見る(説明のために
    名前を書いただけで落ちるテストは、説明を書けなくするだけで守りにならない)。
    """
    tree = ast.parse(_BUY_SERVICE.read_text(encoding="utf-8"))
    forbidden = {"compute_confidence", "ConfidenceFactors"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "confidence_scoring" not in (node.module or "")
            assert forbidden.isdisjoint({a.name for a in node.names})
        elif isinstance(node, ast.Name):
            assert node.id not in forbidden
        elif isinstance(node, ast.Attribute):
            assert node.attr not in forbidden


def test_financial_freshness_is_not_merged_into_data_quality_warning() -> None:
    """取得時刻ベースの鮮度と合流させない。

    `data_quality_warning`はmargin_of_safety・買付価格信頼性・データ品質スコアの
    3経路へ波及するため、合流させると「警告のみ」ではなくなる。
    """
    tree = ast.parse(_BUY_SERVICE.read_text(encoding="utf-8"))
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "data_quality_warning" for t in node.targets)
    ]
    assert assignments, "data_quality_warningの代入が見つからない(構造が変わった)"
    for node in assignments:
        names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
        assert "financial_freshness" not in names
        assert "financial_freshness_warning" not in names


def test_fetched_at_is_not_used_for_financial_freshness_in_buy() -> None:
    """財務鮮度の入力へ取得時刻を渡していないこと(Issue #52の根本原因の再発防止)。"""
    tree = ast.parse(_BUY_SERVICE.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "evaluate_financial_freshness"
    ]
    assert len(calls) == 1
    source_of_call = ast.unparse(calls[0])
    assert "data_fetched_at" not in source_of_call
    assert "fetched_at" not in source_of_call


def test_domain_still_does_not_read_config_itself() -> None:
    """猶予日数の供給は呼び出し側の責務のまま(domainがconfigを読まない)。"""
    domain_source = (_SRC / "domain" / "financial_freshness.py").read_text(encoding="utf-8")
    assert "load_config" not in domain_source
    assert "AppConfig" not in domain_source
    assert "50" not in domain_source
