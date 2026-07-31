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
from jstock_advisor.domain.valuation.shareholder_benefit_matching import compute_next_record_date
from jstock_advisor.infrastructure.local_repository.shareholder_benefit_registry_repository import (
    ShareholderBenefitRegistryRepository,
)
from jstock_advisor.interfaces.types import BenefitDetail, ShareholderBenefit

logger = logging.getLogger(__name__)

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


def _with_refreshed_next_record_date(
    benefit: ShareholderBenefit, now: dt.datetime
) -> ShareholderBenefit:
    """benefit_record_date_recurrence_monthsから次回権利確定日を再計算する。

    「毎年3月末」のような周期は日付を固定で持たせると時間の経過で陳腐化するため、
    保存・取得のたびにnowを基準に再計算し、変化があれば保存し直す(要求仕様16節拡張)。
    """
    next_date = compute_next_record_date(benefit.benefit_record_date_recurrence_months, now.date())
    if next_date == benefit.next_benefit_record_date:
        return benefit
    return benefit.model_copy(update={"next_benefit_record_date": next_date})


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
        benefit = _with_refreshed_next_record_date(benefit, resolved_now)
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
        updated = _with_refreshed_next_record_date(updated, resolved_now)
        self._repo.save(updated)
        return updated

    def list_all(self, now: dt.datetime | None = None) -> list[ShareholderBenefit]:
        resolved_now = now or dt.datetime.now(dt.UTC)
        return [
            self._refresh_and_persist(benefit, resolved_now) for benefit in self._repo.list_all()
        ]

    def get(self, stock_code: str, now: dt.datetime | None = None) -> ShareholderBenefit | None:
        benefit = self._repo.get(stock_code)
        if benefit is None:
            return None
        return self._refresh_and_persist(benefit, now or dt.datetime.now(dt.UTC))

    def delete(self, stock_code: str) -> bool:
        return self._repo.delete(stock_code)

    def _refresh_and_persist(
        self, benefit: ShareholderBenefit, now: dt.datetime
    ) -> ShareholderBenefit:
        refreshed = _with_refreshed_next_record_date(benefit, now)
        if refreshed is not benefit:
            self._repo.save(refreshed)
        return refreshed


def check_registry_health(
    min_expected_entries: int, service: ShareholderBenefitRegistryService | None = None
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
    JSON)を使う。
    """
    count = len((service or ShareholderBenefitRegistryService()).list_all())
    logger.info("ShareholderBenefitRegistry loaded %d entries.", count)
    if min_expected_entries > 0 and count < min_expected_entries:
        logger.warning(
            "ShareholderBenefitRegistry loaded %d entries (expected at least %d). "
            "株主優待レジストリが空または想定より少ない可能性があります。"
            "CSV取込漏れの可能性があるためjstock shareholder-benefit list等で確認してください。",
            count,
            min_expected_entries,
        )
