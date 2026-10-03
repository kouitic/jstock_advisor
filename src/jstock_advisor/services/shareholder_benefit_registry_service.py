"""株主優待の手動登録サービス(要求仕様7節、未確定事項#5)。

株主優待は自動取得できる公式データ源が無いため、ユーザーが手動またはCSVで
登録した内容をそのまま返す。ここでの登録内容がスコアリング・売却判定
(優待廃止・改悪の検知)に直接使われるため、登録は必ずユーザー自身が
確認した一次情報(会社発表・証券会社サイト等)に基づいて行うこと。
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    BenefitUtilityCategory,
    RecordDateUnknownReason,
    SourceType,
)
from jstock_advisor.domain.valuation.shareholder_benefit_matching import (
    with_refreshed_next_record_date,
)
from jstock_advisor.infrastructure.local_repository.shareholder_benefit_registry_repository import (
    ShareholderBenefitRegistryRepository,
)
from jstock_advisor.interfaces.types import BenefitDetail, ShareholderBenefit
from jstock_advisor.services.incident_envelope_publisher import publish_incident_envelope

logger = logging.getLogger(__name__)
# Issue #413 / #493: Lambda の root logger は WARNING のため、module が宣言しないと INFO は出ない。
# check_registry_health() の「登録件数を INFO で常時記録する」(CSV の取込漏れを見つけるための
# 観測)は、宣言が無いと Production では一度も出力されていなかった。出力するのは登録件数のみで、
# 優待の内容・銘柄・所有者は出さない(WARNING / exception の既存の行は変更していない)。
logger.setLevel(logging.INFO)

_PROVIDER_NAME = "manual_registry"


def _source(fetched_at: dt.datetime) -> DataSourceReference:
    # ユーザー自身が一次情報を確認して登録するため、一次情報源として扱う
    return DataSourceReference(
        provider=_PROVIDER_NAME,
        fetched_at=fetched_at,
        source_type=SourceType.MANUAL_REGISTRY,
        primary_source_flag=True,
    )


def _record_date_unknown_reason(
    record_dates: list[dt.date],
) -> RecordDateUnknownReason | None:
    return None if record_dates else RecordDateUnknownReason.SOURCE_NOT_FOUND


class ShareholderBenefitRegistryService:
    def __init__(self, repository: ShareholderBenefitRegistryRepository | None = None) -> None:
        self._repo = repository or ShareholderBenefitRegistryRepository()

    def register(
        self,
        stock_code: str,
        min_shares_required: int,
        frequency_per_year: int,
        category: BenefitUtilityCategory,
        description: str,
        min_shares_for_tier: int,
        estimated_value: Decimal | None = None,
        long_term_holding_condition_months: int | None = None,
        long_term_holding_condition_max_months: int | None = None,
        tier_group: str | None = None,
        benefit_record_dates: list[dt.date] | None = None,
        benefit_record_date_recurrence_months: list[int] | None = None,
        benefit_ex_date: dt.date | None = None,
        long_term_holding_requirement: str | None = None,
        now: dt.datetime | None = None,
    ) -> ShareholderBenefit:
        if min_shares_required <= 0:
            raise ValueError("min_shares_requiredは正の整数である必要があります")
        if frequency_per_year <= 0:
            raise ValueError("frequency_per_yearは正の整数である必要があります")

        resolved_now = now or dt.datetime.now(dt.UTC)
        record_dates = benefit_record_dates or []
        recurrence_months = benefit_record_date_recurrence_months or []
        benefit = ShareholderBenefit(
            stock_code=stock_code,
            min_shares_required=min_shares_required,
            benefits=[
                BenefitDetail(
                    category=category,
                    description=description,
                    estimated_value=estimated_value,
                    min_shares_for_tier=min_shares_for_tier,
                    long_term_holding_condition_months=long_term_holding_condition_months,
                    long_term_holding_condition_max_months=long_term_holding_condition_max_months,
                    tier_group=tier_group,
                )
            ],
            frequency_per_year=frequency_per_year,
            benefit_record_dates=record_dates,
            source=_source(resolved_now),
            benefit_ex_date=benefit_ex_date,
            long_term_holding_requirement=long_term_holding_requirement,
            benefit_record_date_unknown_reason=_record_date_unknown_reason(record_dates),
            benefit_record_date_recurrence_months=recurrence_months,
        )
        benefit = with_refreshed_next_record_date(benefit, resolved_now.date())
        self._repo.save(benefit)
        return benefit

    def add_benefit_detail(
        self,
        stock_code: str,
        category: BenefitUtilityCategory,
        description: str,
        min_shares_for_tier: int,
        estimated_value: Decimal | None = None,
        long_term_holding_condition_months: int | None = None,
        long_term_holding_condition_max_months: int | None = None,
        tier_group: str | None = None,
        now: dt.datetime | None = None,
    ) -> ShareholderBenefit:
        existing = self._repo.get(stock_code)
        if existing is None:
            raise ValueError(
                f"stock_code={stock_code} は未登録です。先にregisterで登録してください"
            )

        detail = BenefitDetail(
            category=category,
            description=description,
            estimated_value=estimated_value,
            min_shares_for_tier=min_shares_for_tier,
            long_term_holding_condition_months=long_term_holding_condition_months,
            long_term_holding_condition_max_months=long_term_holding_condition_max_months,
            tier_group=tier_group,
        )
        updated = existing.model_copy(
            update={
                "benefits": [*existing.benefits, detail],
                "source": _source(now or dt.datetime.now(dt.UTC)),
            }
        )
        self._repo.save(updated)
        return updated

    def update_status(
        self,
        stock_code: str,
        is_abolished: bool | None = None,
        is_major_downgrade: bool | None = None,
        change_note: str | None = None,
        now: dt.datetime | None = None,
    ) -> ShareholderBenefit:
        existing = self._repo.get(stock_code)
        if existing is None:
            raise ValueError(f"stock_code={stock_code} は未登録です")

        updates: dict[str, object] = {"source": _source(now or dt.datetime.now(dt.UTC))}
        if is_abolished is not None:
            updates["is_abolished"] = is_abolished
        if is_major_downgrade is not None:
            updates["is_major_downgrade"] = is_major_downgrade
        if change_note is not None:
            updates["change_note"] = change_note

        updated = existing.model_copy(update=updates)
        self._repo.save(updated)
        return updated

    def set_record_date_recurrence(
        self,
        stock_code: str,
        recurrence_months: list[int],
        now: dt.datetime | None = None,
    ) -> ShareholderBenefit:
        """「毎年◯月末」の周期(月の一覧)を登録し、次回権利確定日を再計算する。"""
        existing = self._repo.get(stock_code)
        if existing is None:
            raise ValueError(f"stock_code={stock_code} は未登録です")

        resolved_now = now or dt.datetime.now(dt.UTC)
        updated = existing.model_copy(
            update={
                "benefit_record_date_recurrence_months": recurrence_months,
                "source": _source(resolved_now),
            }
        )
        updated = with_refreshed_next_record_date(updated, resolved_now.date())
        self._repo.save(updated)
        return updated

    def list_all(self, now: dt.datetime | None = None) -> list[ShareholderBenefit]:
        """全件を返す。**読み取り専用**(永続化を一切伴わない、Issue #61 → #120)。

        next_benefit_record_dateは保存済みの値をそのまま返さず、現行の計算契約に
        従って再導出した値を返す(`with_refreshed_next_record_date`)。
        再導出結果の書き戻しは行わない。
        """
        resolved_now = now or dt.datetime.now(dt.UTC)
        return [
            with_refreshed_next_record_date(benefit, resolved_now.date())
            for benefit in self._repo.list_all()
        ]

    def get(self, stock_code: str, now: dt.datetime | None = None) -> ShareholderBenefit | None:
        """1件を返す。**読み取り専用**(永続化を一切伴わない、Issue #120)。

        next_benefit_record_dateの扱いは`list_all`と同じ。
        """
        benefit = self._repo.get(stock_code)
        if benefit is None:
            return None
        resolved_now = now or dt.datetime.now(dt.UTC)
        return with_refreshed_next_record_date(benefit, resolved_now.date())

    def delete(self, stock_code: str) -> bool:
        return self._repo.delete(stock_code)


# Issue #675(HF-10): 健全性チェック自体の技術的失敗をHF-0契約(#665)でUSER通知する。
# job_nameは、買い候補・保有株の2バッチが共通で呼ぶ処理のため、呼び出し元に依存しない固定値
# (USER決定。どちらのバッチ由来かは利用者向けに区別しない)。内部名は
# domain/notification/incident_message.py::_INTERNAL_NAME_TO_JOBで利用者向けの名称
# 「株主優待データの確認」(USER確定)へ解決される。
_INCIDENT_SOURCE_REGISTRY = "shareholder_benefit_registry"
_INCIDENT_JOB_NAME_REGISTRY = "shareholder-benefit-registry"
_INCIDENT_FAILURE_TYPE = "UNHANDLED_EXCEPTION"
_FAILURE_STAGE_REGISTRY_HEALTH_CHECK = "REGISTRY_HEALTH_CHECK"
_REASON_CODE_REGISTRY_HEALTH_CHECK_FAILED = "SHAREHOLDER_BENEFIT_REGISTRY_HEALTH_CHECK_FAILED"


def _notify_handled_failure(failure_stage: str, reason_code: str, now: dt.datetime) -> None:
    """catchされた技術的失敗をHF-0契約(#665)でUSER通知する。

    envelopeはallowlistのキーのみ(銘柄コード・所有者・stack trace・生のexception messageは
    含めない。`publish_incident_envelope()`が最後の防御として再確認する)。
    `failure_class = HANDLED_FAILURE`のため、GitHub Issueは自動起票されない(HF-0)。
    """
    publish_incident_envelope(
        {
            "source": _INCIDENT_SOURCE_REGISTRY,
            "job_name": _INCIDENT_JOB_NAME_REGISTRY,
            "failure_stage": failure_stage,
            "failure_type": _INCIDENT_FAILURE_TYPE,
            "reason_code": reason_code,
            "occurred_at": now.isoformat(),
            "failure_count": 1,
            "failure_class": "HANDLED_FAILURE",
        }
    )


def _notify_handled_failure_safely(failure_stage: str, reason_code: str, now: dt.datetime) -> None:
    """`_notify_handled_failure()`の失敗(SNS権限不足・Topic ARN未設定等)が、fail-soft契約
    (Issue #120。健全性チェックの失敗が判定・通知処理を止めない)を破らないためのラッパー。

    失敗は握りつぶさず、WARNINGログへ残す(例外の型のみ。内容・識別子は出さない。#135)。
    """
    try:
        _notify_handled_failure(failure_stage, reason_code, now)
    except Exception as exc:  # noqa: BLE001 - HANDLED_FAILURE通知自体の失敗で本処理を止めない
        logger.warning(
            "event=shareholder_benefit_registry_health_check_notify_failed "
            "failure_stage=%s error_type=%s",
            failure_stage,
            type(exc).__name__,
        )


def check_registry_health(
    min_expected_entries: int,
    service: ShareholderBenefitRegistryService | None = None,
    now: dt.datetime | None = None,
) -> None:
    """優待レジストリの読み込み件数をINFOで常時記録し、想定より少ない場合は
    追加でWARNINGを出す(2026-07仕様レビュー対応: CSVは用意されているのに
    レジストリへ未反映という運用ミスをすぐ検知できるようにするため)。
    判定・通知の処理自体は止めない(ログのみ、例外は投げない)。

    min_expected_entries<=0でもINFOログ(件数記録)は常に出す(監視用途では
    「無効化された」ことと「0件登録されている」ことを区別できたほうが良いため)。
    WARNING条件のみmin_expected_entriesで制御する。

    serviceは主にテスト用(任意のリポジトリを注入できるようにするため)。
    未指定時は既定のリポジトリ(Lambda環境ではDynamoDB、それ以外はローカル
    JSON)を使う。nowは通知envelopeのoccurred_at用(テスト用。未指定時は現在時刻)。

    **fail-soft契約(Issue #120)**: 件数取得自体が失敗しても例外を外へ出さない。
    本関数は「登録件数をログへ残す」だけの観測処理であり、判定・通知に必要な
    データを供給していない。したがってその失敗は判定結果に影響せず、
    BUY/保有バッチ全体を停止させてはならない。

    2026-09-02のProduction incidentでは、当時のlist_all()が読み取り経路で
    `repository.save()`(DynamoDB PutItem)へ到達し、読み取り専用IAMの
    BUY/保有Lambdaで`AccessDeniedException`となり、この健全性チェックが
    **dispatch前にバッチ全体を停止**させた(当日の判定・通知が全件未実行)。
    読み取り経路の書き込みは同Issueで除去したが、`list_all()`はDynamoDB Scan
    であり、スロットリング等の一過性エラーでも同じ停止が起こりうるため、
    fail-softは書き込み除去とは独立に必要である。

    **沈黙fail-softは禁止**。失敗時は件数不明であることを観測できるよう
    `event=shareholder_benefit_registry_health_check_failed`のERRORを必ず残す。

    **Issue #675(HF-10)**: 上のERRORログに加えて、失敗をUSERへLINE通知する
    (HANDLED_FAILURE。headlineは「本番処理の一部で問題が発生しました」)。通知の発行自体が
    失敗しても、fail-soft契約は破らない(`_notify_handled_failure_safely()`)。
    「件数が少ないWARNING」(下)は別の事象(#493)であり、本通知の対象にしない。

    なお本契約は健全性チェック自身の失敗に限る。**判定に必要な優待データの
    取得失敗まで握り潰すものではない**(business dataの取得は
    shareholder_benefit provider経由で行われ、失敗時は従来どおり銘柄単位で
    失敗として終端する)。
    """
    try:
        count = len((service or ShareholderBenefitRegistryService()).list_all())
    except Exception:
        logger.exception(
            "event=shareholder_benefit_registry_health_check_failed "
            "min_expected_entries=%d "
            "株主優待レジストリの件数取得に失敗しました。健全性チェックのみを"
            "スキップし、判定・通知処理は継続します(登録件数は不明)。",
            min_expected_entries,
        )
        _notify_handled_failure_safely(
            _FAILURE_STAGE_REGISTRY_HEALTH_CHECK,
            _REASON_CODE_REGISTRY_HEALTH_CHECK_FAILED,
            now if now is not None else dt.datetime.now(dt.UTC),
        )
        return
    logger.info("ShareholderBenefitRegistry loaded %d entries.", count)
    if min_expected_entries > 0 and count < min_expected_entries:
        logger.warning(
            "ShareholderBenefitRegistry loaded %d entries (expected at least %d). "
            "株主優待レジストリが空または想定より少ない可能性があります。"
            "CSV取込漏れの可能性があるためjstock shareholder-benefit list等で確認してください。",
            count,
            min_expected_entries,
        )
