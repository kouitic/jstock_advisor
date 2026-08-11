"""投資ストーリー維持スコアのbaseline確定・個別購入理由管理(実装プラン3節・7節)。

「現在有効なbaseline」の取得は履歴0件/履歴ありポインタ無し/正常/不整合の
4パターンを明確に区別する。活性化(activate_baseline)はbaseline本体を1回だけ
作成し、ポインタ更新のみを最大リトライ回数まで再試行する(2節)。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from jstock_advisor.domain.entities.enums import (
    BaselineOrigin,
    BaselineStatus,
    ThesisConditionAttestationStatus,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding_decision import (
    BaselineValueSnapshot,
    CustomThesisCondition,
    InvestmentThesis,
    InvestmentThesisBaseline,
    ThesisConditionAttestation,
)
from jstock_advisor.infrastructure.aws.baseline_pointer import (
    BaselinePointerConflictError,
    create_pointer,
    get_pointer,
    update_pointer,
)
from jstock_advisor.infrastructure.aws.baseline_sequence import allocate_next_baseline_version
from jstock_advisor.infrastructure.local_repository.investment_thesis_baseline_repository import (
    InvestmentThesisBaselineRepository,
)
from jstock_advisor.infrastructure.local_repository.investment_thesis_repository import (
    InvestmentThesisRepository,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RETRIES = 3
# 通知検証モード機能(2026-08)コードレビュー対応: VALIDATIONではbaseline/thesisの
# 本番書き込み(baseline repository save・version採番・pointer作成/更新・
# thesis repository save)を一切行わず、プロセス内限りのtransientオブジェクトを
# 返す(LineNotificationService/AuditServiceと同じ、コンストラクタ注入+
# choke point guardの流儀)。
_DEFAULT_EXECUTION_CONTEXT = ExecutionContext.normal()


class BaselineActivationExhaustedError(Exception):
    """activate_baseline()が最大リトライ回数まで失敗した(2節: 自動リトライは終了し、
    人間へ再実行を促す)。"""


@dataclass(frozen=True)
class BaselineLookupResult:
    """get_active_baseline()の結果。

    baseline=None, integrity_error=False: baseline履歴自体が0件(初回作成フローへ)
    baseline=None, integrity_error=True : DATA_INTEGRITY_ERROR(自動baseline作成禁止、評価中止)
    baseline!=None                       : このbaselineを使用する
    """

    baseline: InvestmentThesisBaseline | None
    integrity_error: bool = False


class InvestmentThesisService:
    def __init__(
        self,
        baseline_repository: InvestmentThesisBaselineRepository | None = None,
        thesis_repository: InvestmentThesisRepository | None = None,
        default_max_retries: int = _DEFAULT_MAX_RETRIES,
        store_dir: Path | None = None,
        execution_context: ExecutionContext = _DEFAULT_EXECUTION_CONTEXT,
    ) -> None:
        self._baseline_repo = baseline_repository or InvestmentThesisBaselineRepository(store_dir)
        self._thesis_repo = thesis_repository or InvestmentThesisRepository(store_dir)
        self._default_max_retries = default_max_retries
        self._store_dir = store_dir
        self._execution_context = execution_context

    # --- Baseline ------------------------------------------------------------

    def get_active_baseline(self, holding_id: str) -> BaselineLookupResult:
        history = self._baseline_repo.list_by_holding(holding_id)
        pointer = get_pointer(holding_id, self._store_dir)

        if pointer is None:
            if not history:
                return BaselineLookupResult(baseline=None, integrity_error=False)
            return BaselineLookupResult(baseline=None, integrity_error=True)

        baseline = self._baseline_repo.get(pointer.active_baseline_id)
        if baseline is None or baseline.version != pointer.active_baseline_version:
            return BaselineLookupResult(baseline=None, integrity_error=True)
        return BaselineLookupResult(baseline=baseline)

    def activate_baseline(
        self,
        holding_id: str,
        stock_code: str,
        origin: BaselineOrigin,
        baseline_values: BaselineValueSnapshot,
        status: BaselineStatus = BaselineStatus.APPROVED,
        approved_by: str | None = None,
        max_retries: int | None = None,
        now: dt.datetime | None = None,
    ) -> InvestmentThesisBaseline:
        """新しいbaselineを作成し、現在有効なbaselineとして活性化する。

        baseline本体の作成は1回のみ行い、ポインタ更新のみを競合時にリトライする
        (「同一操作を再試行する」という2節の方針。version自体は再採番しない)。
        """
        current_time = now or dt.datetime.now(dt.UTC)

        if self._execution_context.is_validation:
            return self._build_transient_baseline(
                holding_id, stock_code, origin, baseline_values, status, approved_by, current_time
            )

        retries = max_retries if max_retries is not None else self._default_max_retries
        version = allocate_next_baseline_version(holding_id, self._store_dir)
        baseline_id = f"{holding_id}:v{version}"
        existing_pointer = get_pointer(holding_id, self._store_dir)

        baseline = InvestmentThesisBaseline(
            baseline_id=baseline_id,
            holding_id=holding_id,
            stock_code=stock_code,
            version=version,
            origin=origin,
            status=status,
            created_at=current_time,
            approved_at=current_time if status == BaselineStatus.APPROVED else None,
            approved_by=approved_by,
            supersedes_baseline_id=(
                existing_pointer.active_baseline_id if existing_pointer is not None else None
            ),
            baseline_values=baseline_values,
        )
        self._baseline_repo.save_if_absent(baseline)

        last_error: BaselinePointerConflictError | None = None
        for _ in range(retries):
            pointer = get_pointer(holding_id, self._store_dir)
            try:
                if pointer is None:
                    created = create_pointer(
                        holding_id, baseline_id, version, approved_by, current_time, self._store_dir
                    )
                    if created is not None:
                        return baseline
                    continue  # 他プロセスが先にポインタを作成した。再取得して更新分岐へ回す
                update_pointer(
                    holding_id,
                    baseline_id,
                    version,
                    expected_pointer_version=pointer.pointer_version,
                    updated_by=approved_by,
                    now=current_time,
                    store_dir=self._store_dir,
                )
                return baseline
            except BaselinePointerConflictError as e:
                last_error = e
                continue

        raise BaselineActivationExhaustedError(
            f"holding_id={holding_id}: baseline活性化が{retries}回失敗しました"
            f"(最終エラー: {last_error})。最新状態を確認し、改めて実行してください。"
        ) from last_error

    def _build_transient_baseline(
        self,
        holding_id: str,
        stock_code: str,
        origin: BaselineOrigin,
        baseline_values: BaselineValueSnapshot,
        status: BaselineStatus,
        approved_by: str | None,
        current_time: dt.datetime,
    ) -> InvestmentThesisBaseline:
        """VALIDATION専用: 本番のbaseline sequence/pointer/repositoryへ一切
        書き込まず、本番の初回生成ルールと同等のbaselineをプロセス内でのみ生成する。

        activate_baseline()はHoldingDecisionService.evaluate()からlookup.baseline
        がNoneかつintegrity_error=False(=history自体が0件)の場合にのみ呼ばれる
        (BaselineLookupResultのdocstring参照)ため、versionは常に1になる。
        allocate_next_baseline_version()もholding_idごとの初回呼び出しでは1を
        返すため、この値は本番の初回採番結果と一致する。
        """
        version = 1
        baseline_id = f"{holding_id}:v{version}"
        baseline = InvestmentThesisBaseline(
            baseline_id=baseline_id,
            holding_id=holding_id,
            stock_code=stock_code,
            version=version,
            origin=origin,
            status=status,
            created_at=current_time,
            approved_at=current_time if status == BaselineStatus.APPROVED else None,
            approved_by=approved_by,
            supersedes_baseline_id=None,
            baseline_values=baseline_values,
        )
        logger.info(
            "VALIDATION MODE baseline activation transient (not persisted) holding_id=%s "
            "baseline_id=%s",
            holding_id,
            baseline_id,
        )
        return baseline

    # --- InvestmentThesis / CustomThesisCondition -----------------------------

    def get_thesis(self, holding_id: str) -> InvestmentThesis | None:
        return self._thesis_repo.get_by_holding(holding_id)

    def get_or_create_thesis(
        self, holding_id: str, stock_code: str, now: dt.datetime | None = None
    ) -> InvestmentThesis:
        existing = self._thesis_repo.get_by_holding(holding_id)
        if existing is not None:
            return existing
        thesis = InvestmentThesis(
            investment_thesis_id=str(uuid.uuid4()),
            holding_id=holding_id,
            stock_code=stock_code,
            conditions=[],
            updated_at=now or dt.datetime.now(dt.UTC),
        )
        if self._execution_context.is_validation:
            logger.info(
                "VALIDATION MODE investment thesis transient (not persisted) holding_id=%s",
                holding_id,
            )
            return thesis
        self._thesis_repo.save(thesis)
        return thesis

    def register_condition(
        self,
        holding_id: str,
        stock_code: str,
        description: str,
        now: dt.datetime | None = None,
    ) -> InvestmentThesis:
        current_time = now or dt.datetime.now(dt.UTC)
        thesis = self.get_or_create_thesis(holding_id, stock_code, current_time)
        condition = CustomThesisCondition(
            condition_id=str(uuid.uuid4()),
            description=description,
            registered_at=current_time,
        )
        updated = thesis.model_copy(
            update={
                "conditions": [*thesis.conditions, condition],
                "updated_at": current_time,
            }
        )
        self._thesis_repo.save(updated)
        return updated

    def attest_condition(
        self,
        holding_id: str,
        condition_id: str,
        status: ThesisConditionAttestationStatus,
        attested_by: str,
        now: dt.datetime | None = None,
    ) -> InvestmentThesis:
        current_time = now or dt.datetime.now(dt.UTC)
        thesis = self._thesis_repo.get_by_holding(holding_id)
        if thesis is None:
            raise ValueError(f"holding_id={holding_id}のInvestmentThesisが見つかりません")

        new_conditions: list[CustomThesisCondition] = []
        found = False
        for condition in thesis.conditions:
            if condition.condition_id == condition_id:
                found = True
                new_conditions.append(
                    condition.model_copy(
                        update={
                            "last_attestation": ThesisConditionAttestation(
                                status=status,
                                attested_at=current_time,
                                attested_by=attested_by,
                            )
                        }
                    )
                )
            else:
                new_conditions.append(condition)
        if not found:
            raise ValueError(f"condition_id={condition_id}が見つかりません")

        updated = thesis.model_copy(
            update={"conditions": new_conditions, "updated_at": current_time}
        )
        self._thesis_repo.save(updated)
        return updated
