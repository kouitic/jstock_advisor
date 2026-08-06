"""保有判断スコア方式のバックテスト/リプレイ(実装プラン修正5、コードレビュー対応)。

このシステムは財務・配当・優待データを「現在値」としてのみ保持しており、
過去の任意時点の財務スナップショットは保存していない(Phase0前提。過去の
株価時系列(HistoricalValuation)はあるが、スコアの入力である財務・配当・
優待データには時系列が無いため、真の意味での過去時点再現はできない)。

そのため本モジュールは2つのモードを提供する。

- **liveモード**(--start-date/--end-date省略時): 指定銘柄(または全保有銘柄)を
  現在のデータで旧方式(SellSignalService)・新方式(HoldingDecisionService)の
  両方にかけ、判定を並べて出力する(「今この瞬間、両エンジンはどう判定するか」の
  比較)。何も永続化・送信しないため、`*_recommendation_created`/`*_notification_*`
  は常にFalse/NOT_EXECUTED_LIVE_MODE相当となる(`*_should_notify`と混同しない)。
- **replayモード**(--start-date/--end-date指定時): 過去に実際に保存された
  HoldingDecisionResult/Recommendation(shadow運用等で蓄積された実データ)を
  指定期間で抽出し、そのまま並べて出力する(過去に実際に何が起きたかの再生)。
  蓄積が無い期間を指定した場合は素直に0件と報告する(推測で埋め合わせない)。

  旧方式Recommendationには新方式のようなFK(recommendation_id)が存在しないため、
  近接時刻(既定5分以内)→同一日(既定では無効、--allow-same-day-fallback指定時のみ)
  の優先順位で対応付ける。曖昧な場合(候補複数)は対応付けを行わずAMBIGUOUS_MATCH
  とする。詳細はdocs/operations_manual.mdのバックテスト節を参照。
"""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.enums import (
    AccountType,
    BacktestRecommendationSource,
    ExecutionPlanReason,
    classify_recommendation_source,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import HoldingDecisionResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.jst import JST
from jstock_advisor.infrastructure.local_repository.holding_decision_result_repository import (
    HoldingDecisionResultRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.holding_decision_service import HoldingDecisionService
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.sell_signal_service import SellSignalService
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot


class LegacyRecommendationMatchMethod(StrEnum):
    """旧方式Recommendationの対応付け方法。旧方式にはHoldingDecisionResultから
    参照できるFK(recommendation_id等)が存在しないため、RECOMMENDATION_IDは
    存在しない(新方式専用のNewRecommendationMatchMethodと明確に区別する)。
    """

    NEAREST_TIMESTAMP = "NEAREST_TIMESTAMP"
    SAME_DAY_FALLBACK = "SAME_DAY_FALLBACK"
    NO_MATCH = "NO_MATCH"
    # 旧方式は実行予定だった(execution_plan_reasonから判明)が、実行完了(HOLD判定)
    # までは過去データ(HoldingEvaluationAuditが非永続のため)から証明できない場合。
    UNKNOWN_NO_MATCH = "UNKNOWN_NO_MATCH"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"


class NewRecommendationMatchMethod(StrEnum):
    """新方式Recommendationの対応付け方法。HoldingDecisionResult.recommendation_id
    という明示的なFKがあるため、旧方式と異なり対応付けは常に決定論的。"""

    RECOMMENDATION_ID = "RECOMMENDATION_ID"
    RECOMMENDATION_ID_MISSING = "RECOMMENDATION_ID_MISSING"
    RECOMMENDATION_ID_TYPE_MISMATCH = "RECOMMENDATION_ID_TYPE_MISMATCH"
    NO_RECOMMENDATION = "NO_RECOMMENDATION"


class RecommendationMatchConfidence(StrEnum):
    """既存ConfidenceLevel(スコアリング信頼度)とは意味が異なるため専用Enumとする。"""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


class BacktestNotificationStatus(StrEnum):
    """backtest専用。本番のNotificationStatusとは意味が異なるため独立のEnumとする。"""

    SENT = "SENT"
    UNKNOWN = "UNKNOWN"
    NOT_EXECUTED_LIVE_MODE = "NOT_EXECUTED_LIVE_MODE"


# 同一Lambda呼び出し内での新旧評価は通常ミリ秒〜秒単位でしか離れないため、
# 日次バッチ間(数時間〜1日)と明確に区別できる値。
_NEAR_TIMESTAMP_WINDOW = dt.timedelta(minutes=5)

_LIVE_NOT_APPLICABLE = "LIVE_NOT_APPLICABLE"


def _jst_date_range_to_utc(
    start_date: dt.date, end_date: dt.date
) -> tuple[dt.datetime, dt.datetime]:
    """[start_date 00:00 JST, (end_date+1日) 00:00 JST) の半開区間をUTCで返す。"""
    start_utc = dt.datetime.combine(start_date, dt.time.min, tzinfo=JST).astimezone(dt.UTC)
    end_exclusive_utc = dt.datetime.combine(
        end_date + dt.timedelta(days=1), dt.time.min, tzinfo=JST
    ).astimezone(dt.UTC)
    return start_utc, end_exclusive_utc


def _as_aware_utc(value: dt.datetime) -> dt.datetime:
    """naiveはUTCとみなし(既存now生成規約dt.datetime.now(dt.UTC)に合わせる)、
    awareなら必ずUTCへ変換する。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def business_date(value: dt.datetime) -> dt.date:
    """JST基準の暦日(UTC日跨ぎによる誤判定を防ぐ)。"""
    return _as_aware_utc(value).astimezone(JST).date()


_CSV_HEADER = (
    "date",
    "stock_code",
    "source",
    "legacy_recommendation_type",
    "legacy_should_notify",
    "legacy_recommendation_created",
    "legacy_notification_status",
    "legacy_notification_sent",
    "legacy_match_method",
    "legacy_match_warning",
    "legacy_notification_warning",
    "new_score",
    "new_category",
    "new_should_notify",
    "new_recommendation_created",
    "new_notification_status",
    "new_notification_sent",
    "new_match_method",
    "new_match_warning",
    "new_notification_warning",
    "match_confidence",
)


def _opt_bool_str(value: bool | None) -> str:
    return "" if value is None else str(value)


@dataclass(frozen=True)
class BacktestRow:
    stock_code: str
    evaluated_at: dt.datetime
    source: str  # "live" | "history"

    legacy_recommendation_type: str | None
    legacy_should_notify: bool | None
    legacy_recommendation_created: bool | None
    legacy_notification_status: str | None
    legacy_notification_sent: bool | None
    legacy_match_method: str
    legacy_match_warning: str | None
    legacy_notification_warning: str | None

    new_score: float | None
    new_category: str | None
    new_should_notify: bool | None
    new_recommendation_created: bool | None
    new_notification_status: str | None
    new_notification_sent: bool | None
    new_match_method: str
    new_match_warning: str | None
    new_notification_warning: str | None

    # 旧方式側の対応付け品質(新方式は常に決定論的なためmatch_confidenceの対象外)。
    match_confidence: str

    def as_csv_row(self) -> tuple[str, ...]:
        return (
            self.evaluated_at.date().isoformat(),
            self.stock_code,
            self.source,
            self.legacy_recommendation_type or "",
            _opt_bool_str(self.legacy_should_notify),
            _opt_bool_str(self.legacy_recommendation_created),
            self.legacy_notification_status or "",
            _opt_bool_str(self.legacy_notification_sent),
            self.legacy_match_method,
            self.legacy_match_warning or "",
            self.legacy_notification_warning or "",
            "" if self.new_score is None else f"{self.new_score:.2f}",
            self.new_category or "",
            _opt_bool_str(self.new_should_notify),
            _opt_bool_str(self.new_recommendation_created),
            self.new_notification_status or "",
            _opt_bool_str(self.new_notification_sent),
            self.new_match_method,
            self.new_match_warning or "",
            self.new_notification_warning or "",
            self.match_confidence,
        )


def placeholder_holding(stock_code: str, now: dt.datetime) -> Holding:
    """保有していない銘柄をbacktest対象にする場合のダミー保有データ。

    保有判断スコアは現在株価・取得単価・含み益率を一切入力に含めないため
    (実装プラン10節。HoldingDecisionService.evaluate()はholding引数からstock_code
    のみを読む)、これらの値がスコア計算結果へ影響することはない。

    **新方式(HoldingDecisionService)専用。旧方式(SellSignalService)へは絶対に
    渡さないこと**(コードレビュー対応: 旧方式は含み益率・保有期間を実際に使うため、
    ダミー値を渡すと架空の評価結果になる)。
    """
    return Holding(
        stock_code=stock_code,
        stock_name=stock_code,
        shares=100,
        average_purchase_price=Decimal("1"),
        total_purchase_amount=Decimal("100"),
        first_purchase_date=now.date(),
        last_purchase_date=now.date(),
        account_type=AccountType.GENERAL,
        created_at=now,
        updated_at=now,
    )


def resolve_target_stock_codes(
    explicit_stock_codes: list[str], portfolio_service: PortfolioService | None = None
) -> list[str]:
    """--stock-codeが1件以上指定されていればそれを使い、無指定なら全保有銘柄を使う。"""
    if explicit_stock_codes:
        return list(dict.fromkeys(explicit_stock_codes))  # 重複除去・順序維持
    portfolio = portfolio_service or PortfolioService()
    return [h.stock_code for h in portfolio.list_holdings()]


def _data_error_row(stock_code: str, now: dt.datetime, error: str | None) -> BacktestRow:
    return BacktestRow(
        stock_code=stock_code,
        evaluated_at=now,
        source="live",
        legacy_recommendation_type=f"DATA_ERROR: {error}",
        legacy_should_notify=None,
        legacy_recommendation_created=False,
        legacy_notification_status=BacktestNotificationStatus.NOT_EXECUTED_LIVE_MODE.value,
        legacy_notification_sent=False,
        legacy_match_method=_LIVE_NOT_APPLICABLE,
        legacy_match_warning=None,
        legacy_notification_warning=None,
        new_score=None,
        new_category=None,
        new_should_notify=None,
        new_recommendation_created=False,
        new_notification_status=BacktestNotificationStatus.NOT_EXECUTED_LIVE_MODE.value,
        new_notification_sent=False,
        new_match_method=_LIVE_NOT_APPLICABLE,
        new_match_warning=None,
        new_notification_warning=None,
        match_confidence=RecommendationMatchConfidence.NONE.value,
    )


def run_live_comparison(
    stock_codes: list[str],
    providers: ProviderBundle,
    config: AppConfig,
    now: dt.datetime,
    sell_service: SellSignalService | None = None,
    holding_decision_service: HoldingDecisionService | None = None,
    portfolio_service: PortfolioService | None = None,
    holding_overrides: Mapping[str, Holding] | None = None,
) -> list[BacktestRow]:
    """指定銘柄(または保有中の全銘柄)を現在のデータで新旧両エンジンにかける。

    非保有銘柄は、`holding_overrides`で明示的な仮の保有情報が渡されない限り
    旧方式(SellSignalService)を評価しない(コードレビュー対応: 架空の取得単価・
    保有期間による誤評価を防ぐ)。新方式は非保有銘柄でも安全に評価できる
    (placeholder_holding参照)。
    """
    sell_service = sell_service or SellSignalService(providers=providers, config=config)
    holding_decision_service = holding_decision_service or HoldingDecisionService(providers, config)
    portfolio = portfolio_service or PortfolioService()

    rows: list[BacktestRow] = []
    for stock_code in stock_codes:
        snapshot, error = build_stock_snapshot(providers, stock_code, now, config)
        if snapshot is None:
            rows.append(_data_error_row(stock_code, now, error))
            continue

        actual_holding = portfolio.get_holding(stock_code)
        override = (holding_overrides or {}).get(stock_code)
        if override is not None and actual_holding is not None:
            raise ValueError(
                f"{stock_code}は既に保有銘柄として登録されているため"
                "--purchase-price等の指定はできません"
            )
        holding = actual_holding or override

        legacy_recommendation_type: str | None
        legacy_should_notify: bool | None
        if holding is None:
            legacy_recommendation_type = "NOT_EVALUATED_NON_HOLDING"
            legacy_should_notify = None
        else:
            legacy_outcome = sell_service.analyze(holding, now, snapshot=snapshot)
            legacy_should_notify = legacy_outcome.recommendation is not None
            legacy_recommendation_type = (
                legacy_outcome.recommendation.recommendation_type.value
                if legacy_outcome.recommendation is not None
                else "HOLD"
            )

        new_holding = holding or placeholder_holding(stock_code, now)
        new_outcome = holding_decision_service.evaluate(
            new_holding, now, ExecutionPlanReason.NORMAL_SHADOW, snapshot=snapshot
        )
        new_score: float | None
        new_category: str | None
        new_should_notify: bool | None
        if new_outcome.integrity_error or new_outcome.result is None:
            new_score = None
            new_category = "DATA_INTEGRITY_ERROR" if new_outcome.integrity_error else None
            new_should_notify = None
        else:
            new_score = new_outcome.result.final_score
            new_category = new_outcome.result.category.value
            new_should_notify = new_outcome.result.should_notify

        rows.append(
            BacktestRow(
                stock_code=stock_code,
                evaluated_at=now,
                source="live",
                legacy_recommendation_type=legacy_recommendation_type,
                legacy_should_notify=legacy_should_notify,
                legacy_recommendation_created=False,
                legacy_notification_status=BacktestNotificationStatus.NOT_EXECUTED_LIVE_MODE.value,
                legacy_notification_sent=False,
                legacy_match_method=_LIVE_NOT_APPLICABLE,
                legacy_match_warning=None,
                legacy_notification_warning=None,
                new_score=new_score,
                new_category=new_category,
                new_should_notify=new_should_notify,
                new_recommendation_created=False,
                new_notification_status=BacktestNotificationStatus.NOT_EXECUTED_LIVE_MODE.value,
                new_notification_sent=False,
                new_match_method=_LIVE_NOT_APPLICABLE,
                new_match_warning=None,
                new_notification_warning=None,
                match_confidence=RecommendationMatchConfidence.NONE.value,
            )
        )
    return rows


def _notification_facts(
    recommendation_id: str, notification_log_repo: NotificationLogRepository
) -> tuple[BacktestNotificationStatus, bool | None, str | None]:
    """戻り値: (status, sent, notification_warning)。

    NotificationLogは実送信成功時にのみ書き込まれる(LineNotificationService.
    send_recommendation_notificationが唯一の書き込み元。抑止・失敗時には作られない)
    ため、ログが無い=「未送信と確定」ではなく「確認不能」を意味する(UNKNOWN)。
    """
    logs = notification_log_repo.list_by_recommendation_id(recommendation_id)
    if not logs:
        return BacktestNotificationStatus.UNKNOWN, None, None
    warning = (
        f"recommendation_id={recommendation_id}に{len(logs)}件の送信ログがあり"
        "重複の可能性があります"
        if len(logs) > 1
        else None
    )
    return BacktestNotificationStatus.SENT, True, warning


def _resolve_new_match(
    result: HoldingDecisionResult, recommendation_repo: RecommendationRepository
) -> tuple[bool | None, NewRecommendationMatchMethod, str | None]:
    """新方式側のRecommendation実在確認(コードレビュー対応: recommendation_idが
    設定されていても、保存が失敗・欠落している可能性があるため実在確認まで行う)。
    """
    if result.recommendation_id is None:
        return False, NewRecommendationMatchMethod.NO_RECOMMENDATION, None
    found = recommendation_repo.get(result.recommendation_id)
    if found is None:
        return (
            None,
            NewRecommendationMatchMethod.RECOMMENDATION_ID_MISSING,
            f"recommendation_id={result.recommendation_id}に対応するRecommendationが"
            "見つかりません(データ不整合)",
        )
    if found.stock_code != result.stock_code:
        return (
            None,
            NewRecommendationMatchMethod.RECOMMENDATION_ID_MISSING,
            f"recommendation_id={result.recommendation_id}のstock_code({found.stock_code})が"
            f"HoldingDecisionResultのstock_code({result.stock_code})と一致しません"
            "(採用しません)",
        )
    if (
        classify_recommendation_source(found.recommendation_type)
        != BacktestRecommendationSource.HOLDING_DECISION
    ):
        return (
            None,
            NewRecommendationMatchMethod.RECOMMENDATION_ID_TYPE_MISMATCH,
            f"recommendation_id={result.recommendation_id}のrecommendation_type"
            f"({found.recommendation_type.value})が新方式の想定型と一致しません",
        )
    return True, NewRecommendationMatchMethod.RECOMMENDATION_ID, None


def _match_legacy_recommendation(
    evaluated_at: dt.datetime,
    candidates: list[Recommendation],
    allow_same_day_fallback: bool,
) -> tuple[
    Recommendation | None,
    LegacyRecommendationMatchMethod,
    RecommendationMatchConfidence,
    str | None,
]:
    """旧方式Recommendationの対応付け。candidatesは事前にLEGACY_SELL分類済み・
    未消費・同一stock_codeへ絞り込み済みであること。

    近接時間内(既定5分)に候補が複数ある場合は、最小差分が一意であっても
    自動的に最も近い1件を採用せずAMBIGUOUS_MATCHとする(安全側設計)。
    """
    evaluated_at = _as_aware_utc(evaluated_at)
    within_window = [
        c
        for c in candidates
        if abs(_as_aware_utc(c.recommended_at) - evaluated_at) <= _NEAR_TIMESTAMP_WINDOW
    ]
    if len(within_window) == 1:
        return (
            within_window[0],
            LegacyRecommendationMatchMethod.NEAREST_TIMESTAMP,
            RecommendationMatchConfidence.HIGH,
            None,
        )
    if len(within_window) > 1:
        return (
            None,
            LegacyRecommendationMatchMethod.AMBIGUOUS_MATCH,
            RecommendationMatchConfidence.LOW,
            f"近接時刻({_NEAR_TIMESTAMP_WINDOW}以内)に{len(within_window)}件の候補があり"
            "一意に対応付けできません",
        )

    same_day = [
        c for c in candidates if business_date(c.recommended_at) == business_date(evaluated_at)
    ]
    if not allow_same_day_fallback:
        if same_day:
            return (
                None,
                LegacyRecommendationMatchMethod.NO_MATCH,
                RecommendationMatchConfidence.NONE,
                f"同一日に{len(same_day)}件の候補が存在しましたが、"
                "same-day fallbackが無効なため採用しませんでした",
            )
        return (
            None,
            LegacyRecommendationMatchMethod.NO_MATCH,
            RecommendationMatchConfidence.NONE,
            None,
        )

    if len(same_day) == 1:
        return (
            same_day[0],
            LegacyRecommendationMatchMethod.SAME_DAY_FALLBACK,
            RecommendationMatchConfidence.MEDIUM,
            "同一日フォールバックによる対応付けのため信頼度は中程度です",
        )
    if len(same_day) > 1:
        return (
            None,
            LegacyRecommendationMatchMethod.AMBIGUOUS_MATCH,
            RecommendationMatchConfidence.LOW,
            f"同一日に{len(same_day)}件の候補があり一意に対応付けできません",
        )
    return None, LegacyRecommendationMatchMethod.NO_MATCH, RecommendationMatchConfidence.NONE, None


@dataclass(frozen=True)
class _LegacySideFields:
    recommendation_type: str | None
    should_notify: bool | None
    recommendation_created: bool | None
    notification_status: str | None
    notification_sent: bool | None
    match_method: LegacyRecommendationMatchMethod
    match_confidence: RecommendationMatchConfidence
    match_warning: str | None
    notification_warning: str | None


def _legacy_fields_for_result(
    result: HoldingDecisionResult,
    candidates: list[Recommendation],
    allow_same_day_fallback: bool,
    notification_log_repo: NotificationLogRepository,
) -> tuple[_LegacySideFields, str | None]:
    """戻り値の2つ目は、今回消費した(対応付けに使った)recommendation_id(あれば)。"""
    legacy_rec, match_method, confidence, match_warning = _match_legacy_recommendation(
        result.evaluated_at, candidates, allow_same_day_fallback
    )
    if legacy_rec is not None:
        status, sent, notif_warning = _notification_facts(
            legacy_rec.recommendation_id, notification_log_repo
        )
        return (
            _LegacySideFields(
                recommendation_type=legacy_rec.recommendation_type.value,
                should_notify=True,
                recommendation_created=True,
                notification_status=status.value,
                notification_sent=sent,
                match_method=match_method,
                match_confidence=confidence,
                match_warning=match_warning,
                notification_warning=notif_warning,
            ),
            legacy_rec.recommendation_id,
        )
    if match_method == LegacyRecommendationMatchMethod.AMBIGUOUS_MATCH:
        return (
            _LegacySideFields(
                recommendation_type=None,
                should_notify=None,
                recommendation_created=None,
                notification_status=None,
                notification_sent=None,
                match_method=match_method,
                match_confidence=confidence,
                match_warning=match_warning,
                notification_warning=None,
            ),
            None,
        )
    # NO_MATCH: 旧方式が実行予定だったか自体をexecution_plan_reasonから判定する。
    if result.execution_plan_reason == ExecutionPlanReason.NORMAL_ACTIVE:
        return (
            _LegacySideFields(
                recommendation_type=None,
                should_notify=None,
                recommendation_created=False,
                notification_status=None,
                notification_sent=None,
                match_method=match_method,
                match_confidence=confidence,
                match_warning=match_warning,
                notification_warning=None,
            ),
            None,
        )
    return (
        _LegacySideFields(
            recommendation_type="UNKNOWN_NO_MATCH",
            should_notify=None,
            recommendation_created=None,
            notification_status=None,
            notification_sent=None,
            match_method=LegacyRecommendationMatchMethod.UNKNOWN_NO_MATCH,
            match_confidence=confidence,
            match_warning=(
                "execution_plan_reasonから旧方式実行予定だったことは分かりますが、"
                "実行完了(HOLD判定)までは過去データから証明できません"
            ),
            notification_warning=None,
        ),
        None,
    )


@dataclass(frozen=True)
class _NewSideFields:
    recommendation_created: bool | None
    notification_status: str | None
    notification_sent: bool | None
    match_method: NewRecommendationMatchMethod
    match_warning: str | None
    notification_warning: str | None


def _new_fields_for_result(
    result: HoldingDecisionResult,
    recommendation_repo: RecommendationRepository,
    notification_log_repo: NotificationLogRepository,
) -> _NewSideFields:
    created, method, match_warning = _resolve_new_match(result, recommendation_repo)
    if created is True:
        assert result.recommendation_id is not None
        status, sent, notif_warning = _notification_facts(
            result.recommendation_id, notification_log_repo
        )
        return _NewSideFields(True, status.value, sent, method, match_warning, notif_warning)
    if created is False:
        return _NewSideFields(False, None, False, method, match_warning, None)
    return _NewSideFields(None, None, None, method, match_warning, None)


def run_history_replay(
    stock_codes: list[str],
    start_date: dt.date,
    end_date: dt.date,
    holding_decision_result_repo: HoldingDecisionResultRepository | None = None,
    recommendation_repo: RecommendationRepository | None = None,
    notification_log_repo: NotificationLogRepository | None = None,
    allow_same_day_fallback: bool = False,
) -> list[BacktestRow]:
    """指定期間に実際に保存されたHoldingDecisionResult/Recommendationを再生する。

    蓄積が無ければ空リストを返す(推測で埋め合わせない)。期間はJST基準の暦日
    半開区間[start_date 00:00 JST, end_date翌日 00:00 JST)で絞り込む。

    allow_same_day_fallback(既定False)を有効にしない限り、同一日候補のみによる
    対応付け(SAME_DAY_FALLBACK)は行わない(信頼度が低いため安全側で無効)。
    """
    hd_repo = holding_decision_result_repo or HoldingDecisionResultRepository()
    rec_repo = recommendation_repo or RecommendationRepository()
    notif_repo = notification_log_repo or NotificationLogRepository()

    start_utc, end_exclusive_utc = _jst_date_range_to_utc(start_date, end_date)
    stock_code_filter = set(stock_codes) if stock_codes else None

    hd_results = [
        r
        for r in hd_repo.list_between(start_utc, end_exclusive_utc)
        if start_utc <= _as_aware_utc(r.evaluated_at) < end_exclusive_utc
        and (stock_code_filter is None or r.stock_code in stock_code_filter)
    ]
    hd_results.sort(key=lambda r: (r.evaluated_at, r.stock_code, r.holding_decision_result_id))

    legacy_candidates_by_stock: dict[str, list[Recommendation]] = {}
    for rec in rec_repo.list_all():
        if not (start_utc <= _as_aware_utc(rec.recommended_at) < end_exclusive_utc):
            continue
        if stock_code_filter is not None and rec.stock_code not in stock_code_filter:
            continue
        if classify_recommendation_source(rec.recommendation_type) != (
            BacktestRecommendationSource.LEGACY_SELL
        ):
            continue
        legacy_candidates_by_stock.setdefault(rec.stock_code, []).append(rec)
    for candidates in legacy_candidates_by_stock.values():
        candidates.sort(key=lambda r: (r.recommended_at, r.recommendation_id))

    consumed_recommendation_ids: set[str] = set()
    rows: list[BacktestRow] = []
    for result in hd_results:
        available = [
            c
            for c in legacy_candidates_by_stock.get(result.stock_code, [])
            if c.recommendation_id not in consumed_recommendation_ids
        ]
        legacy_fields, consumed_id = _legacy_fields_for_result(
            result, available, allow_same_day_fallback, notif_repo
        )
        if consumed_id is not None:
            consumed_recommendation_ids.add(consumed_id)

        new_fields = _new_fields_for_result(result, rec_repo, notif_repo)

        rows.append(
            BacktestRow(
                stock_code=result.stock_code,
                evaluated_at=result.evaluated_at,
                source="history",
                legacy_recommendation_type=legacy_fields.recommendation_type,
                legacy_should_notify=legacy_fields.should_notify,
                legacy_recommendation_created=legacy_fields.recommendation_created,
                legacy_notification_status=legacy_fields.notification_status,
                legacy_notification_sent=legacy_fields.notification_sent,
                legacy_match_method=legacy_fields.match_method.value,
                legacy_match_warning=legacy_fields.match_warning,
                legacy_notification_warning=legacy_fields.notification_warning,
                new_score=result.final_score,
                new_category=result.category.value,
                new_should_notify=result.should_notify,
                new_recommendation_created=new_fields.recommendation_created,
                new_notification_status=new_fields.notification_status,
                new_notification_sent=new_fields.notification_sent,
                new_match_method=new_fields.match_method.value,
                new_match_warning=new_fields.match_warning,
                new_notification_warning=new_fields.notification_warning,
                match_confidence=legacy_fields.match_confidence.value,
            )
        )

    # 新方式の評価が無い日でも、旧方式(LEGACY_SELL分類済み)のRecommendationだけは
    # 行として残す(未消費のもののみ。HOLDING_DECISION/EXCLUDED分類は対象外)。
    for stock_code, candidates in legacy_candidates_by_stock.items():
        for rec in candidates:
            if rec.recommendation_id in consumed_recommendation_ids:
                continue
            status, sent, notif_warning = _notification_facts(rec.recommendation_id, notif_repo)
            rows.append(
                BacktestRow(
                    stock_code=stock_code,
                    evaluated_at=rec.recommended_at,
                    source="history",
                    legacy_recommendation_type=rec.recommendation_type.value,
                    legacy_should_notify=True,
                    legacy_recommendation_created=True,
                    legacy_notification_status=status.value,
                    legacy_notification_sent=sent,
                    legacy_match_method=LegacyRecommendationMatchMethod.NO_MATCH.value,
                    legacy_match_warning=(
                        "対応するHoldingDecisionResultが見つからない旧方式Recommendationです"
                    ),
                    legacy_notification_warning=notif_warning,
                    new_score=None,
                    new_category=None,
                    new_should_notify=None,
                    new_recommendation_created=False,
                    new_notification_status=None,
                    new_notification_sent=False,
                    new_match_method=NewRecommendationMatchMethod.NO_RECOMMENDATION.value,
                    new_match_warning=None,
                    new_notification_warning=None,
                    match_confidence=RecommendationMatchConfidence.NONE.value,
                )
            )

    return sorted(rows, key=lambda r: (r.evaluated_at, r.stock_code))


def write_backtest_csv(rows: list[BacktestRow], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_HEADER)
        for row in rows:
            writer.writerow(row.as_csv_row())
