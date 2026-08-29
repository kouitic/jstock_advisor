"""業種分類のcanonical契約テスト(Issue #54 Phase B-1)。

Phase A設計の4原則のうち、本モジュールが担う部分を固定する。

1. canonical source は JPX TSE33 であり、**安定キーは33業種コード**(表示名ではない)
2. 業種(軸a)と証券種別(軸d)は**独立**に決まる。相互推論しない
3. UNKNOWN を明示保持する(「不明 → 普通株」「不明 → 一般事業会社」等の暗黙変換の禁止)
4. yfinance の開いた語彙を canonical へ昇格させない

**本モジュールは観測専用であり、いかなる投資判断も変更しない。** 判定への非影響は
tests/unit/test_buy_signal_service.py 側で固定する。
"""

from __future__ import annotations

from jstock_advisor.domain.classification.canonical_industry import (
    CanonicalIndustrySource,
    CanonicalSecurityType,
    classify_canonical_industry,
    classify_security_type,
)

_PRIME = "プライム（内国株式）"
_REIT_SEGMENT = "REIT・ベンチャーファンド・カントリーファンド・インフラファンド"


def test_jpx_33_code_becomes_canonical_and_source_is_jpx() -> None:
    """33業種コードが得られれば、それがcanonicalであり source=JPX_TSE33。"""
    result = classify_canonical_industry(
        industry_33_code="3050",
        industry_33_name="医薬品",
        market_segment=_PRIME,
    )

    assert result.industry_33_code == "3050"
    assert result.industry_33_name == "医薬品"
    assert result.source is CanonicalIndustrySource.JPX_TSE33
    assert result.is_resolved is True


def test_display_name_alone_does_not_resolve_canonical_industry() -> None:
    """安定キーはコード。表示名だけではcanonicalを確定させない。

    JPXが業種の表示ラベルを変更しても判定が壊れないための契約であり、
    「名前が一致したから同じ業種」という推測を禁止する。
    """
    result = classify_canonical_industry(
        industry_33_code=None,
        industry_33_name="医薬品",
        market_segment=_PRIME,
    )

    assert result.industry_33_code is None
    assert result.is_resolved is False
    # 表示名もcanonicalとしては保持しない(観測用のfallback_*にも入らない)。
    assert result.industry_33_name is None


def test_domestic_stock_segments_are_common_stock() -> None:
    for segment in (
        "プライム（内国株式）",
        "スタンダード（内国株式）",
        "グロース（内国株式）",
        "PRO Market",
        "外国株式",
    ):
        assert classify_security_type(segment) is CanonicalSecurityType.COMMON_STOCK, segment


def test_reit_segment_is_reit_security_type() -> None:
    assert classify_security_type(_REIT_SEGMENT) is CanonicalSecurityType.REIT


def test_etf_segment_is_etf_etn_security_type() -> None:
    assert classify_security_type("ETF・ETN") is CanonicalSecurityType.ETF_ETN


def test_investment_certificate_segment_is_other_listed() -> None:
    assert classify_security_type("出資証券") is CanonicalSecurityType.OTHER_LISTED


def test_unknown_or_missing_segment_is_unknown_not_common_stock() -> None:
    """未知・欠落の市場区分は UNKNOWN。「普通株とみなす」暗黙変換を禁止する。

    provider既定値の `security_type="STOCK"` と異なり、canonicalは
    「判定できなかった」ことを消さない。
    """
    for segment in (None, "", "   ", "東証二部", "未知の区分"):
        assert classify_security_type(segment) is CanonicalSecurityType.UNKNOWN, segment


def test_real_estate_industry_is_not_inferred_as_reit() -> None:
    """業種 → 証券種別の推論を行わない(「不動産業だからREIT」の禁止)。"""
    result = classify_canonical_industry(
        industry_33_code="8050",
        industry_33_name="不動産業",
        market_segment=_PRIME,
    )

    assert result.security_type is CanonicalSecurityType.COMMON_STOCK


def test_reit_security_type_does_not_fill_industry_code() -> None:
    """証券種別 → 業種の推論を行わない(「REITだから不動産業」の禁止)。"""
    result = classify_canonical_industry(
        industry_33_code=None,
        industry_33_name=None,
        market_segment=_REIT_SEGMENT,
    )

    assert result.security_type is CanonicalSecurityType.REIT
    assert result.industry_33_code is None
    assert result.is_resolved is False


def test_provider_vocabulary_is_not_promoted_to_canonical() -> None:
    """yfinanceのsector/industryを33業種コードへ変換して埋めない。

    第三者APIの開いた語彙(GICS準拠の英語値)をcanonicalへ昇格させると、
    語彙変更が判定へ直接波及する。観測用にそのまま残すだけとする。
    """
    result = classify_canonical_industry(
        industry_33_code=None,
        industry_33_name=None,
        market_segment=None,
        fallback_sector="Healthcare",
        fallback_industry="Drug Manufacturers - Specialty & Generic",
    )

    assert result.industry_33_code is None
    assert result.is_resolved is False
    assert result.source is CanonicalIndustrySource.YFINANCE_FALLBACK
    assert result.fallback_sector == "Healthcare"
    assert result.fallback_industry == "Drug Manufacturers - Specialty & Generic"
    assert result.security_type is CanonicalSecurityType.UNKNOWN


def test_no_input_at_all_is_unavailable_not_fallback() -> None:
    """JPXもproviderも値が無い状態を UNAVAILABLE として区別する。

    「JPXで解決できなかった」と「そもそも何も観測できなかった」を混同すると、
    Phase B-2でJPX解決率を評価できない。
    """
    result = classify_canonical_industry(
        industry_33_code=None,
        industry_33_name=None,
        market_segment=None,
    )

    assert result.source is CanonicalIndustrySource.UNAVAILABLE
    assert result.fallback_sector is None
    assert result.fallback_industry is None


def test_whitespace_only_values_are_treated_as_missing() -> None:
    result = classify_canonical_industry(
        industry_33_code="  ",
        industry_33_name="  ",
        market_segment="  ",
        fallback_sector="  ",
        fallback_industry="  ",
    )

    assert result.industry_33_code is None
    assert result.source is CanonicalIndustrySource.UNAVAILABLE
    assert result.security_type is CanonicalSecurityType.UNKNOWN


def test_security_type_is_resolved_independently_of_industry_resolution() -> None:
    """業種が解決できなくても証券種別は決まる(2軸が独立であることの確認)。"""
    result = classify_canonical_industry(
        industry_33_code=None,
        industry_33_name=None,
        market_segment="ETF・ETN",
        fallback_sector="Financial Services",
    )

    assert result.is_resolved is False
    assert result.security_type is CanonicalSecurityType.ETF_ETN
