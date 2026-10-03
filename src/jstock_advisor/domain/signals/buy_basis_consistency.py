"""BUY候補経路の基準整合判定(Issue #698 PR-A)。

株価(価格履歴。データ提供元が分割後の基準へ遡及調整する)と、EPS・BPS・配当等の
1株当たり指標(別の取得経路。調整が反映される時期が銘柄・項目ごとに異なる)が、
同じ分割基準の上で組み合わされているかを判定する。2026-09-29の山九(9065)の
銘柄分析では、分割調整後の株価1,633円に、分割前の基準のEPS・BPSから算出した
PER 3.3・PBR 0.27が組み合わされ、適正価格・買付価格3段階・BUY判定まで伝播した。

3値(MATCH / MISMATCH / UNKNOWN)で扱う(USER決定、#122 issuecomment-5968760192)。

- MATCH: 財務指標の基準日(fiscal_period_end)から評価日までに、価格履歴が報告した
  分割・併合が1件も無い(両者の基準がずれる余地が無い)。
- MISMATCH: 異なる基準のデータが混在していることを**確認できた**。確認の根拠
  (mismatch_evidence)を呼び出し側が渡した場合のみ。**PR-Aの本番経路では根拠の
  供給元が無いため発火しない**(手動登録簿へ書き込む手段がsrcに存在せず、
  「財務指標が未調整」を表す項目も無い。値の整合の検査は標本不足で未実装。
  #698 PR-B / USER判断)。分岐とテストは、供給元が追加された時にBUY経路が
  正しく止まることを固定するために先に入れてある。
- UNKNOWN: 確認できない(窓に分割がある・取得元が分割を報告しなかった・基準日が
  不明・取得範囲外 等)。「確認できなかった」を「分割は無かった」へ潰さない。
  外部サービスの失敗はMISMATCHにならない。

この関数は純粋関数であり、I/Oを行わない。分割の取得は価格履歴の応答(追加の
呼び出しなし)から行う。既存のCorporateActionService.classify_basis_date_consistency
(PR-1の契約)は変更していない(あちらは窓の上限が評価日で、保有側の利確判定に
配線されている)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from jstock_advisor.interfaces.types import PriceSplit


class BasisConsistency(StrEnum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


class BasisReasonCode(StrEnum):
    """判定の理由(保存する。UNKNOWNの理由別に後から集計・gateを変えられるように分ける)。"""

    NO_EVENT = "BASIS_NO_EVENT"
    EVENT_IN_WINDOW = "BASIS_EVENT_IN_WINDOW"
    FUNDAMENTAL_DATE_UNKNOWN = "BASIS_FUNDAMENTAL_DATE_UNKNOWN"
    SPLIT_DATA_NOT_REPORTED = "BASIS_SPLIT_DATA_NOT_REPORTED"
    HISTORY_UNAVAILABLE = "BASIS_HISTORY_UNAVAILABLE"
    WINDOW_NOT_COVERED = "BASIS_WINDOW_NOT_COVERED"
    MISMATCH_EVIDENCE = "BASIS_MISMATCH_EVIDENCE"
    # 判定処理自体が想定外の例外で失敗した(呼び出し側がUNKNOWNへ変換して判定を継続する)。
    ASSESSMENT_FAILED = "BASIS_ASSESSMENT_FAILED"


# decide_buy_action()が、UNKNOWNのBUY系判定をWATCH_FOR_PRICEへ格下げしたときの理由コード。
BASIS_UNKNOWN_CAP_REASON_CODE = "BASIS_UNKNOWN_CAP"


@dataclass(frozen=True)
class BasisMismatchEvidence:
    """MISMATCHを確認できた根拠(呼び出し側が供給する。PR-Aの本番経路には供給元が無い)。"""

    source: str


@dataclass(frozen=True)
class BasisAssessment:
    status: BasisConsistency
    reason_code: BasisReasonCode
    fundamental_period_end: dt.date | None = None
    price_as_of_date: dt.date | None = None
    history_start: dt.date | None = None
    events: tuple[PriceSplit, ...] = ()
    mismatch_evidence_source: str | None = None

    def to_facts(self) -> dict[str, object]:
        """保存用(buy_score_input_facts / 監査ログ)。値はすべて判定時点の事実。"""
        return {
            "status": self.status.value,
            "reason_code": self.reason_code.value,
            "fundamental_period_end": (
                self.fundamental_period_end.isoformat() if self.fundamental_period_end else None
            ),
            "price_as_of_date": (
                self.price_as_of_date.isoformat() if self.price_as_of_date else None
            ),
            "history_start": self.history_start.isoformat() if self.history_start else None,
            "events": [
                {"date": event.date.isoformat(), "ratio": str(event.ratio)} for event in self.events
            ],
            "mismatch_evidence_source": self.mismatch_evidence_source,
        }


def assess_basis_consistency(
    *,
    price_as_of_date: dt.date,
    fundamental_period_end: dt.date | None,
    history_start: dt.date | None,
    bars_available: bool,
    splits: list[PriceSplit] | None,
    mismatch_evidence: BasisMismatchEvidence | None = None,
) -> BasisAssessment:
    """価格と財務指標の分割基準が揃っているかを判定する(判定順序は下記の6段)。

    1. mismatch_evidenceがある                         -> MISMATCH
    2. 財務指標の基準日が不明                           -> UNKNOWN
    3. 価格履歴が分割を報告しなかった(splitsがNone)     -> UNKNOWN(「分割なし」にしない。
       バーも無ければ理由はHISTORY_UNAVAILABLE、バーはあるのに報告が無ければSPLIT_DATA_NOT_REPORTED)
    4. 窓の始端が価格履歴の取得範囲より古い/範囲が不明      -> UNKNOWN(範囲外を「分割なし」にしない)
    5. ratio != 1の分割が fundamental_period_end < 日付 <= price_as_of_date にある -> UNKNOWN
    6. それ以外                                         -> MATCH

    設計(#698 issuecomment-5969186943)では「バーが無い」を独立の段(4)にしていたが、
    実際のproviderはバーが無い応答ではhistory自体をNoneにする(splitsも報告されない)ため、
    独立の段は冗長であり、splits=Noneの理由の区別へ畳んだ(判定結果は変わらない)。

    窓の境界: 日付 == fundamental_period_endは窓の外(PR-1の`lo < effective`と同じ)、
    日付 == price_as_of_dateは窓の内(山九の権利落ち日2026-09-29は評価日2026-09-29で内)。
    窓の上限は、価格履歴が報告する分割の日付が価格の最新日を超えないため、実質無い。
    """

    def result(
        status: BasisConsistency,
        reason_code: BasisReasonCode,
        *,
        events: tuple[PriceSplit, ...] = (),
        evidence_source: str | None = None,
    ) -> BasisAssessment:
        return BasisAssessment(
            status=status,
            reason_code=reason_code,
            fundamental_period_end=fundamental_period_end,
            price_as_of_date=price_as_of_date,
            history_start=history_start,
            events=events,
            mismatch_evidence_source=evidence_source,
        )

    if mismatch_evidence is not None:
        return result(
            BasisConsistency.MISMATCH,
            BasisReasonCode.MISMATCH_EVIDENCE,
            evidence_source=mismatch_evidence.source,
        )
    if fundamental_period_end is None:
        return result(BasisConsistency.UNKNOWN, BasisReasonCode.FUNDAMENTAL_DATE_UNKNOWN)
    if splits is None:
        # 価格履歴が無い(取得できたが期間にバーが無い)場合と、履歴はあるが分割を報告
        # しなかった場合を、理由コードで分ける(集計・将来のgateの別のため)。
        reason = (
            BasisReasonCode.SPLIT_DATA_NOT_REPORTED
            if bars_available
            else BasisReasonCode.HISTORY_UNAVAILABLE
        )
        return result(BasisConsistency.UNKNOWN, reason)
    if history_start is None or fundamental_period_end < history_start:
        return result(BasisConsistency.UNKNOWN, BasisReasonCode.WINDOW_NOT_COVERED)
    in_window = tuple(
        split
        for split in splits
        if split.ratio != Decimal(1) and fundamental_period_end < split.date <= price_as_of_date
    )
    if in_window:
        return result(BasisConsistency.UNKNOWN, BasisReasonCode.EVENT_IN_WINDOW, events=in_window)
    return result(BasisConsistency.MATCH, BasisReasonCode.NO_EVENT)
