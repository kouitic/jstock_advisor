"""業種分類のcanonical契約(Issue #54 Phase B-1)。

**本モジュールは観測(shadow)専用であり、現時点でいかなる投資判断も変更しない。**
既存の4分類器(`financial_industry` / `buy_industry` / `profit_taking_industry` /
`stock_type` 内のインライン分類)は無変更のまま残し、本モジュールが算出する
canonical分類は `buy_score_input_facts` へ記録されるだけである。
死んでいる判定(CYCLICAL/DEFENSIVE・REIT除外)の復活は **Phase B-2** で、
shadow観測の結果を確認したうえで実施する。

## 設計(Phase A で承認済み)

1. **canonical source は JPX TSE33**。安定キーは表示名ではなく **33業種コード**
   (ラベル変更に耐えるため)。yfinanceの `sector` / `industry` は truth source にしない。
2. **4つの軸を分離する**(1つのenumへ混ぜない):
   (a) 業種 = JPX 33業種 / (b) 投資スタイル = 財務指標由来 /
   (c) 景気敏感性 = (a)からの明示mapping / (d) 証券種別 = JPX市場・商品区分
   本モジュールは **(a) と (d)** のみを扱う。(b)(c) はPhase B-2以降。
3. **classification と policy を分離する**。ここは「何の業種か」だけを返し、
   「BUY/watchlistでどう扱うか」は各consumerのpolicyの責務(#29のpolicyは変更しない)。
4. **UNKNOWN を明示保持する**。`UNKNOWN → 一般事業会社` `UNKNOWN → 非金融`
   `UNKNOWN → DEFENSIVE` といった暗黙変換を禁止する。
5. **REIT は業種ではなく証券種別**。「不動産業だからREIT」「REITだから不動産業」という
   双方向の推論を行わない。

## 語彙の由来

JPXの33業種区分は取引所が管理する閉じた語彙で、コード(4桁)と名称が対になっている。
`providers/candidate_universe/jpx_impl.py` が既に必須列として検証つきでパースしており、
本モジュールはその値をそのまま canonical として受け取る(再パース・推測はしない)。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CanonicalIndustrySource(StrEnum):
    """canonical業種の決定に使った入力の由来。

    観測時に「JPXで解決できた割合」と「yfinanceフォールバックに頼っている割合」を
    区別するために保持する(Phase B-2の判断材料)。
    """

    JPX_TSE33 = "JPX_TSE33"
    YFINANCE_FALLBACK = "YFINANCE_FALLBACK"
    UNAVAILABLE = "UNAVAILABLE"


class CanonicalSecurityType(StrEnum):
    """証券種別(軸(d))。業種(軸(a))とは独立に決まる。

    JPXの「市場・商品区分」を権威とする。判定できない場合は **UNKNOWN**であり、
    「普通株とみなす」ことはしない(現行の `security_type` 既定値 "STOCK" とは異なる)。
    """

    COMMON_STOCK = "COMMON_STOCK"
    REIT = "REIT"
    ETF_ETN = "ETF_ETN"
    OTHER_LISTED = "OTHER_LISTED"
    UNKNOWN = "UNKNOWN"


# JPX「市場・商品区分」→ 証券種別。`jpx_impl._KNOWN_MARKET_SEGMENTS` と同じ文字列定数を
# 使う(JPX公開資料で確認済みの既知値)。ここに無い値は UNKNOWN とし、推測しない。
_MARKET_SEGMENT_TO_SECURITY_TYPE: dict[str, CanonicalSecurityType] = {
    "プライム（内国株式）": CanonicalSecurityType.COMMON_STOCK,
    "スタンダード（内国株式）": CanonicalSecurityType.COMMON_STOCK,
    "グロース（内国株式）": CanonicalSecurityType.COMMON_STOCK,
    "PRO Market": CanonicalSecurityType.COMMON_STOCK,
    "外国株式": CanonicalSecurityType.COMMON_STOCK,
    "ETF・ETN": CanonicalSecurityType.ETF_ETN,
    "REIT・ベンチャーファンド・カントリーファンド・インフラファンド": CanonicalSecurityType.REIT,
    "出資証券": CanonicalSecurityType.OTHER_LISTED,
}


@dataclass(frozen=True)
class CanonicalIndustryClassification:
    """1銘柄のcanonical業種・証券種別(観測用)。

    `industry_33_code` が canonical の安定キー。表示名(`industry_33_name`)は
    JPXのラベル変更で変わり得るため、判定の根拠に使ってはならない。
    """

    industry_33_code: str | None
    industry_33_name: str | None
    security_type: CanonicalSecurityType
    source: CanonicalIndustrySource
    # JPXで解決できなかった場合に、観測用として何を見たかを残す(判定には使わない)。
    fallback_sector: str | None = None
    fallback_industry: str | None = None

    @property
    def is_resolved(self) -> bool:
        """canonical業種コードが確定しているか(UNKNOWNを暗黙に埋めない)。"""
        return self.industry_33_code is not None


def classify_security_type(market_segment: str | None) -> CanonicalSecurityType:
    """JPX「市場・商品区分」から証券種別を決める。

    未知・欠落は **UNKNOWN**(普通株とみなさない)。業種からの推論は行わない
    (「不動産業だからREIT」は禁止)。
    """
    if not market_segment:
        return CanonicalSecurityType.UNKNOWN
    return _MARKET_SEGMENT_TO_SECURITY_TYPE.get(
        market_segment.strip(), CanonicalSecurityType.UNKNOWN
    )


def classify_canonical_industry(
    *,
    industry_33_code: str | None,
    industry_33_name: str | None,
    market_segment: str | None,
    fallback_sector: str | None = None,
    fallback_industry: str | None = None,
) -> CanonicalIndustryClassification:
    """canonical業種・証券種別を決める(観測専用)。

    JPXの33業種コードが得られればそれを canonical とする。得られない場合
    (保有銘柄などJPX universeを通らない経路)は **UNKNOWNのまま**とし、
    yfinanceの `sector`/`industry` を業種コードへ変換して埋めることはしない
    (第三者APIの開いた語彙を canonical へ昇格させない)。観測のために
    参照した値だけを `fallback_*` へ残す。
    """
    security_type = classify_security_type(market_segment)
    code = (industry_33_code or "").strip() or None
    name = (industry_33_name or "").strip() or None

    if code is not None:
        return CanonicalIndustryClassification(
            industry_33_code=code,
            industry_33_name=name,
            security_type=security_type,
            source=CanonicalIndustrySource.JPX_TSE33,
        )

    has_fallback_input = bool((fallback_sector or "").strip() or (fallback_industry or "").strip())
    return CanonicalIndustryClassification(
        industry_33_code=None,
        industry_33_name=None,
        security_type=security_type,
        source=(
            CanonicalIndustrySource.YFINANCE_FALLBACK
            if has_fallback_input
            else CanonicalIndustrySource.UNAVAILABLE
        ),
        fallback_sector=(fallback_sector or "").strip() or None,
        fallback_industry=(fallback_industry or "").strip() or None,
    )
