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
from jstock_advisor.domain.entities.owner import log_ref
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
from jstock_advisor.services.write_plan import ConditionalPut, apply_conditional_put

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
            f"holding_ref={log_ref(holding_id)}: baseline活性化が{retries}回失敗しました"
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
        # ★ baseline_id(= f"{holding_id}:v{version}")は所有者名を含むため、生のまま出さない
        #   (Issue #416)。holding_ref(log_ref 済み)と version で、どの holding の何番目の
        #   baseline かは等価に特定できる。
        logger.info(
            "VALIDATION MODE baseline activation transient (not persisted) holding_ref=%s "
            "version=%d",
            log_ref(holding_id),
            version,
        )
        return baseline

    # --- InvestmentThesis / CustomThesisCondition -----------------------------

    def get_thesis(self, holding_id: str) -> InvestmentThesis | None:
        return self._thesis_repo.get_by_holding(holding_id)

    def get_or_create_thesis(
        self, holding_id: str, stock_code: str, now: dt.datetime | None = None
    ) -> InvestmentThesis:
        """holding_idに対応するInvestmentThesisを取得し、無ければ作成する。

        Issue #570(#71 F-C13から分離): 同一holding_idへの並行呼び出しが
        異なるinvestment_thesis_id(旧: uuid4)を持つ2件を二重生成しうる欠陥
        への対処。新規作成時のinvestment_thesis_idを`holding_id`自体から
        決定的に導出し(`investment_thesis_baseline_repository.py`の
        `baseline_id = f"{holding_id}:v{version}"`と同型の決定的キー生成)、
        既存のCollectionStore CAS primitive(`insert_if_absent()`。#17)で
        原子的に作成する。新しいlock/lease機構は作らない。

        `get_by_holding()`(線形scan)を既存確認に残すため、#570着手前に
        作成された旧形式(uuid4のinvestment_thesis_id)のレコードも引き続き
        見つかる(後方互換。migration/backfill不要)。
        """
        existing = self._thesis_repo.get_by_holding(holding_id)
        if existing is not None:
            return existing
        thesis = InvestmentThesis(
            investment_thesis_id=holding_id,
            holding_id=holding_id,
            stock_code=stock_code,
            conditions=[],
            updated_at=now or dt.datetime.now(dt.UTC),
        )
        if self._execution_context.is_validation:
            logger.info(
                "VALIDATION MODE investment thesis transient (not persisted) holding_ref=%s",
                log_ref(holding_id),
            )
            return thesis
        if self._thesis_repo.insert_if_absent(thesis):
            return thesis
        # 他プロセスが先に同じholding_idでinsert_if_absent()に成功していた
        # (同一holding_idへの並行create)。自分が構築したtransientなthesisは
        # 捨て、既に永続化された方を返す(#570が防ぎたい二重生成そのもの)。
        winner = self._thesis_repo.get(holding_id)
        if winner is not None:
            return winner
        # 理論上到達しない(insert_if_absent失敗直後にgetがNoneになるのは、
        # 直後に別プロセスが削除した場合のみで、本Issueのscopeでは削除経路が
        # 存在しない)。defensiveに線形scanへfallbackする。
        fallback = self._thesis_repo.get_by_holding(holding_id)
        if fallback is not None:
            return fallback
        raise RuntimeError(
            f"holding_ref={log_ref(holding_id)}: insert_if_absentが失敗したのに"
            "該当レコードが見つかりません(想定外の状態)"
        )

    def register_condition(
        self,
        holding_id: str,
        stock_code: str,
        description: str,
        now: dt.datetime | None = None,
    ) -> InvestmentThesis:
        """個別購入理由を1件追加する。

        Issue #570: 読み取り(get_or_create_thesis)と書き込み(save)の間に
        別の更新が入ると後勝ちで消える(lost update)欠陥への対処。#530と
        同型のCAS(services/write_plan.py::apply_conditional_put())へ切替え、
        既存のCollectionStore CAS primitive(replace_if_raw_matches。#17)を
        再利用する。新しいCAS方式は作らない。競合時は
        `services.write_plan.ConcurrentUpdateError`(ValueErrorサブクラス)を
        送出する(自動リトライしない。呼び出し元が最新状態を確認してやり直す)。
        """
        current_time = now or dt.datetime.now(dt.UTC)
        thesis = self.get_or_create_thesis(holding_id, stock_code, current_time)
        if self._execution_context.is_validation:
            # #570着手前からの既存の非対称をそのまま維持する(挙動を変えない):
            # get_or_create_thesis()はis_validation時にtransient(非永続)な
            # thesisを返すが、本メソッドはis_validationかどうかに関わらず
            # save()を無条件に呼んでいた(=conditions追加はVALIDATIONでも
            # 実際に永続化される)。この非対称はIssue #570のscope外として
            # 記録済み(OUT_OF_SCOPE。issuecomment-5841369172参照。是正を
            # 検討する場合はUSER判断で別Issue化する)。ここでCASへ切り替えると
            # (transient thesisにはget_raw_dataで拾える実データが無いため)
            # 新たな失敗を作ってしまうため、is_validation時は従来どおり
            # save()を直接呼ぶ(CASを経由しない)。
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
        # Issue #570: expected_dataはthesis読み取りの直後、updated構築より前に
        # 取得する(#530と同じ理由。既存条件から書き込み内容を組み立てた後に
        # 生データを取り直すと、その間の並行更新をCASが素通りさせてしまう)。
        existing_raw = self._thesis_repo.get_raw_data(thesis.investment_thesis_id)
        if existing_raw is None:
            raise ValueError(
                f"holding_ref={log_ref(holding_id)}のInvestmentThesisデータ取得に失敗しました"
                "(get_or_create_thesis直後にget_raw_dataがNoneを返した。並行削除の疑い)"
            )
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
        apply_conditional_put(
            self._thesis_repo,
            ConditionalPut(
                model=updated, id_field="investment_thesis_id", expected_data=existing_raw
            ),
        )
        return updated

    def attest_condition(
        self,
        holding_id: str,
        condition_id: str,
        status: ThesisConditionAttestationStatus,
        attested_by: str,
        now: dt.datetime | None = None,
    ) -> InvestmentThesis:
        """個別購入理由の維持状況を人間が申告する。

        Issue #570: register_condition()と同型のCAS対策(#530と同型。
        write_plan.py::apply_conditional_put()を再利用)。読み取り直後の生JSONを
        楽観ロック条件として保持する。
        """
        current_time = now or dt.datetime.now(dt.UTC)
        thesis = self._thesis_repo.get_by_holding(holding_id)
        if thesis is None:
            raise ValueError(
                f"holding_ref={log_ref(holding_id)}のInvestmentThesisが見つかりません"
            )
        # Issue #570(#530 F3と同型): get_by_holding()とget_raw_data()は別呼び出し
        # のため、その間に並行削除されるとexisting_rawがNoneに到達しうる
        # (assertではなく明示的なValueErrorとする)。
        existing_raw = self._thesis_repo.get_raw_data(thesis.investment_thesis_id)
        if existing_raw is None:
            raise ValueError(
                f"holding_ref={log_ref(holding_id)}のInvestmentThesisデータ取得に失敗しました"
            )

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
        apply_conditional_put(
            self._thesis_repo,
            ConditionalPut(
                model=updated, id_field="investment_thesis_id", expected_data=existing_raw
            ),
        )
        return updated
