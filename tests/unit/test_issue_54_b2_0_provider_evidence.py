"""Issue #54 Phase B-2-0: JPX解決時も provider の生値を観測に残す。

## なぜ必要か

Phase B-1 の observation は、JPXで解決できた場合に `fallback_sector` /
`fallback_industry` を保持しない実装だった。Production の JPX 解決率は
2営業日連続 100% であるため、**provider の生値が構造的に常に null** になり、

    canonical(JPX 33業種)  と  既存分類器が実際に見た入力

を同一 observation から突き合わせることができなかった。
canonical を採用するかどうかを実測で判断するには、解決できた場合こそ生値が要る。

## 本モジュールが固定する契約

```
JPX RESOLVED でも provider 生値を保持する
provider 値を canonical へ昇格させない(source は JPX_TSE33 のまま)
canonical 値を provider 値で上書きしない
provider 値が無い場合に null を捏造しない
```

## 本モジュールが判定しないこと

**業務判定は一切変えない。** 本 Phase は shadow-only であり、
`fallback_sector` / `fallback_industry` はいずれの判定経路からも参照されない。
その不変性は `test_provider_evidence_is_not_referenced_by_decision_paths` で固定する。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from jstock_advisor.domain.classification.canonical_industry import (
    CanonicalIndustrySource,
    CanonicalSecurityType,
    JpxLookupStatus,
    classify_canonical_industry,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PRIME = "プライム（内国株式）"
_REIT_SEGMENT = "REIT・ベンチャーファンド・カントリーファンド・インフラファンド"

_PROVIDER_SECTOR = "Technology"
_PROVIDER_INDUSTRY = "Software - Application"


# --- CASE 1: JPX RESOLVED + provider あり ----------------------------------------


def test_case1_resolved_keeps_provider_evidence() -> None:
    """JPXで解決できても provider の生値を保持する(本 Phase の中心的な契約)。

    修正前はここが null になり、比較評価が成立しなかった。
    """
    result = classify_canonical_industry(
        industry_33_code="5250",
        industry_33_name="情報・通信業",
        market_segment=_PRIME,
        jpx_lookup_status=JpxLookupStatus.RESOLVED,
        fallback_sector=_PROVIDER_SECTOR,
        fallback_industry=_PROVIDER_INDUSTRY,
    )

    # canonical は JPX のまま
    assert result.industry_33_code == "5250"
    assert result.industry_33_name == "情報・通信業"
    assert result.source is CanonicalIndustrySource.JPX_TSE33
    assert result.jpx_lookup_status is JpxLookupStatus.RESOLVED
    assert result.is_resolved is True

    # provider の生値も残る
    assert result.fallback_sector == _PROVIDER_SECTOR
    assert result.fallback_industry == _PROVIDER_INDUSTRY


def test_case1_provider_value_is_not_promoted_to_canonical() -> None:
    """provider 値を保持しても canonical へ昇格させない。

    `source` が YFINANCE_FALLBACK へ変わってしまうと、JPX解決率の算出が壊れる。
    """
    result = classify_canonical_industry(
        industry_33_code="5250",
        industry_33_name="情報・通信業",
        market_segment=_PRIME,
        jpx_lookup_status=JpxLookupStatus.RESOLVED,
        fallback_sector=_PROVIDER_SECTOR,
        fallback_industry=_PROVIDER_INDUSTRY,
    )

    assert result.source is CanonicalIndustrySource.JPX_TSE33
    assert result.industry_33_name != _PROVIDER_INDUSTRY
    assert result.industry_33_code != _PROVIDER_SECTOR


def test_case1_canonical_is_not_overwritten_by_provider() -> None:
    """provider が別業種を主張しても canonical は JPX の値のままである。"""
    result = classify_canonical_industry(
        industry_33_code="3050",
        industry_33_name="医薬品",
        market_segment=_PRIME,
        jpx_lookup_status=JpxLookupStatus.RESOLVED,
        fallback_sector="Financial Services",
        fallback_industry="Banks - Regional",
    )

    assert result.industry_33_code == "3050"
    assert result.industry_33_name == "医薬品"
    assert result.security_type is CanonicalSecurityType.COMMON_STOCK
    # 突き合わせのため provider 側の主張も残っている
    assert result.fallback_sector == "Financial Services"
    assert result.fallback_industry == "Banks - Regional"


# --- CASE 2: JPX NOT_FOUND + provider あり ---------------------------------------


def test_case2_not_found_behavior_is_unchanged() -> None:
    """NOT_FOUND の fallback 挙動は従来どおり(本 Phase で変えない)。"""
    result = classify_canonical_industry(
        industry_33_code=None,
        industry_33_name=None,
        market_segment=None,
        jpx_lookup_status=JpxLookupStatus.NOT_FOUND,
        fallback_sector=_PROVIDER_SECTOR,
        fallback_industry=_PROVIDER_INDUSTRY,
    )

    assert result.industry_33_code is None
    assert result.is_resolved is False
    assert result.source is CanonicalIndustrySource.YFINANCE_FALLBACK
    assert result.jpx_lookup_status is JpxLookupStatus.NOT_FOUND
    assert result.fallback_sector == _PROVIDER_SECTOR
    assert result.fallback_industry == _PROVIDER_INDUSTRY


# --- CASE 3: JPX SOURCE_UNAVAILABLE + provider あり -------------------------------


def test_case3_source_unavailable_behavior_is_unchanged() -> None:
    """SOURCE_UNAVAILABLE の fail-soft 挙動は従来どおり。

    `NOT_FOUND`(一覧に無い)と `SOURCE_UNAVAILABLE`(一覧を読めない)は
    別事象であり、`jpx_lookup_status` で区別され続けること。
    """
    result = classify_canonical_industry(
        industry_33_code=None,
        industry_33_name=None,
        market_segment=None,
        jpx_lookup_status=JpxLookupStatus.SOURCE_UNAVAILABLE,
        fallback_sector=_PROVIDER_SECTOR,
        fallback_industry=_PROVIDER_INDUSTRY,
    )

    assert result.is_resolved is False
    assert result.source is CanonicalIndustrySource.YFINANCE_FALLBACK
    assert result.jpx_lookup_status is JpxLookupStatus.SOURCE_UNAVAILABLE
    assert result.fallback_sector == _PROVIDER_SECTOR
    assert result.fallback_industry == _PROVIDER_INDUSTRY


# --- CASE 4: provider 値が無い ----------------------------------------------------


@pytest.mark.parametrize(
    ("sector", "industry"),
    [(None, None), ("", ""), ("   ", "\t"), (None, "  ")],
    ids=["both_none", "both_empty", "whitespace", "mixed"],
)
def test_case4_missing_provider_values_are_not_fabricated(
    sector: str | None, industry: str | None
) -> None:
    """provider 値が無い場合に null を捏造しない(空文字を値として残さない)。"""
    result = classify_canonical_industry(
        industry_33_code="5250",
        industry_33_name="情報・通信業",
        market_segment=_PRIME,
        jpx_lookup_status=JpxLookupStatus.RESOLVED,
        fallback_sector=sector,
        fallback_industry=industry,
    )

    assert result.fallback_sector is None
    assert result.fallback_industry is None
    # canonical 側は影響を受けない
    assert result.industry_33_code == "5250"
    assert result.source is CanonicalIndustrySource.JPX_TSE33


def test_case4_no_input_at_all_stays_unavailable() -> None:
    """JPXもproviderも値が無い状態は従来どおり UNAVAILABLE(FALLBACK にしない)。"""
    result = classify_canonical_industry(
        industry_33_code=None,
        industry_33_name=None,
        market_segment=None,
        jpx_lookup_status=JpxLookupStatus.SOURCE_UNAVAILABLE,
    )

    assert result.source is CanonicalIndustrySource.UNAVAILABLE
    assert result.fallback_sector is None
    assert result.fallback_industry is None


# --- security type は業種解決から独立(E の比較軸)---------------------------------


def test_security_type_stays_independent_of_provider_evidence() -> None:
    """provider 生値を残しても security_type の決定は市場区分のみに依存する。

    JPX security type と provider security type の比較(評価軸 E)が
    成立するために、両者が混ざらないことを固定する。
    """
    result = classify_canonical_industry(
        industry_33_code=None,
        industry_33_name=None,
        market_segment=_REIT_SEGMENT,
        jpx_lookup_status=JpxLookupStatus.RESOLVED,
        fallback_sector="Real Estate",
        fallback_industry="REIT - Diversified",
    )

    assert result.security_type is CanonicalSecurityType.REIT
    # REIT は33業種コードを持たない。provider 値で埋めない
    assert result.industry_33_code is None
    assert result.fallback_sector == "Real Estate"


# --- shadow-only であることの構造的保証 -------------------------------------------

# 判定経路がこのフィールドを読み始めたら、本 Phase の前提(shadow-only)が壊れる。
_PROVIDER_EVIDENCE_FIELDS = ("fallback_sector", "fallback_industry")

# 参照が許されるのは、定義元と observation 記録箇所のみ。
_ALLOWED_REFERENCES = frozenset(
    {
        "src/jstock_advisor/domain/classification/canonical_industry.py",
        "src/jstock_advisor/services/buy_signal_service.py",
    }
)


def _iter_source_files() -> list[Path]:
    return sorted((_REPO_ROOT / "src").rglob("*.py"))


def test_provider_evidence_is_not_referenced_by_decision_paths() -> None:
    """provider 生値が判定経路から参照されていないこと(shadow-only の保証)。

    B-2-0 は observation のみを変更する。将来この値をスコアや除外判定へ
    使い始めた場合、それは Phase B-2-A 以降の設計判断であり、
    本テストが落ちることで気づけるようにする。
    """
    offenders: list[str] = []
    for path in _iter_source_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _ALLOWED_REFERENCES:
            continue
        source = path.read_text(encoding="utf-8")
        if any(field in source for field in _PROVIDER_EVIDENCE_FIELDS):
            offenders.append(rel)

    assert offenders == [], (
        f"provider 生値({', '.join(_PROVIDER_EVIDENCE_FIELDS)})が "
        f"observation 以外から参照されています: {offenders}。"
        "B-2-0 は shadow-only です。判定へ接続する場合は Phase B-2-A 以降として"
        "設計判断を経てください(Issue #54)。"
    )


def test_allowed_reference_files_still_exist() -> None:
    """許可リストが実体から外れて空振りしないこと。"""
    for rel in sorted(_ALLOWED_REFERENCES):
        assert (_REPO_ROOT / rel).is_file(), f"許可リストのファイルが見つかりません: {rel}"


def test_observation_records_provider_evidence_field_names() -> None:
    """observation が provider 生値を記録し続けること(記録の削除を検知する)。"""
    path = _REPO_ROOT / "src/jstock_advisor/services/buy_signal_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "provider_sector" in keys
    assert "provider_industry" in keys
    # 比較の相手側(canonical / 既存分類器)も揃っていること
    for key in (
        "canonical_industry_33_code",
        "canonical_security_type",
        "jpx_lookup_status",
        "financial_industry_classification",
        "buy_industry_sector",
        "profit_taking_industry_sector",
        "stock_type_cyclical_or_defensive",
        "provider_security_type",
    ):
        assert key in keys, f"比較に必要な observation 項目が失われています: {key}"
