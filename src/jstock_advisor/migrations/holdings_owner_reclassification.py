"""既存保有データのowner実態補正(M4.1、2026-08確定指示)。

M2(holdings_owner_migration.py)は「owner概念が存在しなかった時点のデータは
すべて本人保有」という前提でowner="本人"を機械的に付与した。本モジュールは
その後判明した実際の所有者(子供名義の証券口座で管理されている銘柄等)に
基づき、current-stateのownerを実態へ補正する一回限りの移行である。

対象(current-state、実態owner補正の対象): Holding / PurchaseLot /
HoldingsSnapshot(active) / InvestmentThesis / BaselineSequence /
BaselinePointer。

対象外(過去履歴、owner="本人"の残存を許容・確定指示): Recommendation /
NotificationLog / DecisionSnapshot / Transaction / HoldingDecisionResult /
InvestmentThesisBaseline。過去のowner帰属を現在の保有者から遡って推定する
ことはできないため。また4631のtombstone(active_holding=False、対応する
Holdingが存在しない)も対象外(owner確定不能のため現状維持、確定指示)。

4680(ラウンドワン)は1 Holding(400株)→2 Holding(大きい持分側+小さい持分側)へ
分割する特殊ケース。9434(ソフトバンク)はowner変更に加えて取得単価の
訂正(確定指示)を伴う。

実行前precondition(2026-08-23確定指示、コードレビュー対応): dry-runでの
人間確認だけに頼らず、build_plan()自身が実行のたびに対象銘柄の実データ
(shares・取得単価・4680のlot構成)をユーザー確定値と照合し、一致しない
場合はPlanValidationErrorでfail-closedに中止する(下記RealDataInput参照)。

owner型は引き続きEnum/allow-listではない(domain/entities/owner.py)。

実在の所有者名・実際の保有数量・取得単価はGit管理対象のソースコードへ
一切記録しない(2026-08-25コードレビュー対応、CLAUDE.md恒久ルール)。
これらは今回1回限りの実データ再分類の入力値にすぎず、実行時に
`RealDataInput`として外部(Git管理対象外のローカルJSONファイル、
`jstock migrate owner-reclassification run --plan-file`)から読み込む。
本モジュール自体は所有者名・数値をハードコードせず、どのような
`RealDataInput`が渡されても同じロジックで動作する汎用的な移行機構である。

冪等性・再実行安全性の設計:
  - build_plan()は毎回、その時点の実データ(Holding.owner=="本人"の
    存在有無)から計画を再構築する。あるstock_codeの旧Holding(owner="本人")
    が既に削除済みであれば、そのstock_codeは次回scanの対象に含まれず、
    計画にも一切現れない(=完了済みグループは自動的に再処理されない)。
  - 1グループ(旧holding_id1件、4680のみ新holding_id2件)内の書き込み順序は
    「新規作成(upsert、常に安全に再試行可能)」→「旧レコード削除」の順で、
    旧Holdingの削除を必ず最後に行う。これにより、旧Holdingがまだ存在する
    間はグループ全体が「移行未完了」とみなされ安全に再試行できる
    (pause_buy_sell=trueにより実際の取引書き込みは本移行の前提として
    停止されているため、新holding_idへの書き込み競合は発生しない)。
    旧Holdingの削除が成功した時点でグループは不可逆に「完了」となり、
    以降は絶対に再書き込みしない(完了後は新holding_id側で通常運用
    (実際の取引・cooldown更新・thesis編集等)が始まりうるため)。
  - HoldingsSnapshotのみ、旧スナップショットから内容を引き継ぐ
    (cooldown_until_date等)ため、旧スナップショットが既に削除された
    後の再試行では引き継ぎ元を失う。このケースを安全にするため、
    新holding_id側に既にスナップショットが存在する場合は再導出・
    上書きをせずスキップする(write-once)。
  - PurchaseLotはlot_idを主キーとし、変更しない(owner/holding_id/
    (9434のみ)purchase_priceフィールドのみをin-placeで書き換える)ため、
    削除は不要かつ常に同じlot_idで安全に再upsertできる。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.holding import Holding, PurchaseLot, summarize_lots
from jstock_advisor.domain.entities.holding_decision import (
    InvestmentThesis,
    InvestmentThesisBaselinePointer,
)
from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
from jstock_advisor.domain.entities.owner import build_holding_id, normalize_and_validate_owner
from jstock_advisor.infrastructure.collection_store import (
    build_collection_store,
    resolve_table_name,
    running_on_lambda,
)
from jstock_advisor.migrations.target import MigrationTarget, target_backend

_HOLDINGS_FILE = "holdings_v2.json"
_PURCHASE_LOTS_FILE = "purchase_lots.json"
_SNAPSHOTS_FILE = "holdings_snapshots_v2.json"
_INVESTMENT_THESES_FILE = "investment_theses.json"
_SEQUENCE_FILE = "investment_thesis_baseline_sequences_v2.json"
_POINTER_FILE = "investment_thesis_baseline_pointers_v2.json"

OLD_OWNER = "本人"


# --- 実データ入力(2026-08-25コードレビュー対応: 個人情報除去) ---------------
# 実在の所有者名・実際の保有数量・取得単価はGit管理対象のソースコードへ
# 記録しない(CLAUDE.md恒久ルール)。これらは`RealDataInput`として実行時に
# 外部の(Git管理対象外の)ローカルJSONファイルから読み込む
# (`jstock migrate owner-reclassification run --plan-file <path>`)。
# 以前はこれらがモジュール定数としてハードコードされていたが、本モジュール
# 自体はどのようなRealDataInputが渡されても同じロジックで動作する汎用的な
# 移行機構であり、特定の所有者名・数値に依存しない。


@dataclass(frozen=True)
class _SimplePrecondition:
    expected_shares: int
    expected_average_price: Decimal


@dataclass(frozen=True)
class RealDataInput:
    """M4.1実行時にのみ渡される実データ(Git管理対象外)。

    `load_real_data_input()`でローカルJSONファイルから読み込む。フィールドの
    意味は該当箇所のdocstring/コメント(旧モジュール定数のコメント)を参照。
    """

    default_new_owner: str
    child_owner_by_stock_code: dict[str, str]
    split_stock_code: str
    split_lot_owners: dict[str, str]
    price_corrections: dict[str, Decimal]
    simple_preconditions: dict[str, _SimplePrecondition]
    nine_four_three_four_stock_code: str
    nine_four_three_four_lot_id: str
    nine_four_three_four_shares: int
    nine_four_three_four_old_price: Decimal
    split_lot_preconditions: dict[str, tuple[int, Decimal]]
    split_old_shares: int
    split_old_average_price: Decimal
    split_old_total_amount: Decimal


def load_real_data_input(path: Path) -> RealDataInput:
    """`--plan-file`で指定されたローカルJSON(Git管理対象外)からRealDataInput
    を読み込む。想定するJSONスキーマ(値はすべて実データのプレースホルダ、
    実在の所有者名・金額はこのモジュールにもリポジトリ全体にも記録しない):

    {
      "default_new_owner": "<owner>",
      "child_owner_by_stock_code": {"<stock_code>": "<owner>", ...},
      "split_stock_code": "<stock_code>",
      "split_lot_owners": {"<lot_id>": "<owner>", ...},
      "price_corrections": {"<lot_id>": "<corrected_price>", ...},
      "simple_preconditions": {
        "<stock_code>": {"expected_shares": <int>, "expected_average_price": "<price>"}, ...
      },
      "nine_four_three_four": {
        "stock_code": "<stock_code>", "lot_id": "<lot_id>",
        "shares": <int>, "old_price": "<price>"
      },
      "split_lot_preconditions": {"<lot_id>": {"shares": <int>, "price": "<price>"}, ...},
      "split_old": {
        "shares": <int>, "average_price": "<price>", "total_amount": "<amount>"
      }
    }
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    nine_four_three_four = raw["nine_four_three_four"]
    split_old = raw["split_old"]
    return RealDataInput(
        default_new_owner=raw["default_new_owner"],
        child_owner_by_stock_code=dict(raw["child_owner_by_stock_code"]),
        split_stock_code=raw["split_stock_code"],
        split_lot_owners=dict(raw["split_lot_owners"]),
        price_corrections={k: Decimal(v) for k, v in raw["price_corrections"].items()},
        simple_preconditions={
            k: _SimplePrecondition(
                expected_shares=int(v["expected_shares"]),
                expected_average_price=Decimal(v["expected_average_price"]),
            )
            for k, v in raw["simple_preconditions"].items()
        },
        nine_four_three_four_stock_code=nine_four_three_four["stock_code"],
        nine_four_three_four_lot_id=nine_four_three_four["lot_id"],
        nine_four_three_four_shares=int(nine_four_three_four["shares"]),
        nine_four_three_four_old_price=Decimal(nine_four_three_four["old_price"]),
        split_lot_preconditions={
            k: (int(v["shares"]), Decimal(v["price"]))
            for k, v in raw["split_lot_preconditions"].items()
        },
        split_old_shares=int(split_old["shares"]),
        split_old_average_price=Decimal(split_old["average_price"]),
        split_old_total_amount=Decimal(split_old["total_amount"]),
    )


def _check_holding_matches(
    stock_code: str, holding: Holding, expected_shares: int, expected_average_price: Decimal
) -> None:
    shares_mismatch = holding.shares != expected_shares
    price_mismatch = holding.average_purchase_price != expected_average_price
    if shares_mismatch or price_mismatch:
        raise PlanValidationError(
            f"{stock_code}の旧Holdingが確定指示の値(shares={expected_shares}, "
            f"average_purchase_price={expected_average_price})と一致しません"
            f"(fail-closed): shares={holding.shares}, "
            f"average_purchase_price={holding.average_purchase_price}"
        )


def _check_simple_precondition(
    stock_code: str,
    holding: Holding,
    stock_lots: list[PurchaseLot],
    precondition: _SimplePrecondition,
) -> None:
    _check_holding_matches(
        stock_code, holding, precondition.expected_shares, precondition.expected_average_price
    )
    if not stock_lots:
        raise PlanValidationError(
            f"{stock_code}に対応するPurchaseLotが1件もありません(fail-closed)。"
        )
    total_shares, avg_price, _total, _first, _last = summarize_lots(stock_lots)
    shares_mismatch = total_shares != precondition.expected_shares
    avg_mismatch = avg_price != precondition.expected_average_price
    if shares_mismatch or avg_mismatch:
        raise PlanValidationError(
            f"{stock_code}のPurchaseLot再計算値(shares={total_shares}, "
            f"average={avg_price})が確定指示の値(shares={precondition.expected_shares}, "
            f"average={precondition.expected_average_price})と一致しません(fail-closed)。"
        )


def _check_9434_precondition(
    holding: Holding, stock_lots: list[PurchaseLot], real_data: RealDataInput
) -> None:
    lot_id = real_data.nine_four_three_four_lot_id
    shares = real_data.nine_four_three_four_shares
    old_price = real_data.nine_four_three_four_old_price
    corrected_price = real_data.price_corrections[lot_id]
    stock_code = real_data.nine_four_three_four_stock_code
    if holding.shares != shares or holding.average_purchase_price not in (
        old_price,
        corrected_price,
    ):
        raise PlanValidationError(
            f"{stock_code}の旧Holdingが確定指示の値(shares={shares}, "
            f"average_purchase_price={old_price}(訂正前)または"
            f"{corrected_price}(訂正後))と一致しません(fail-closed): "
            f"shares={holding.shares}, average_purchase_price={holding.average_purchase_price}"
        )
    if len(stock_lots) != 1 or stock_lots[0].lot_id != lot_id:
        raise PlanValidationError(
            f"{stock_code}のPurchaseLot構成が確定指示(lot_id="
            f"{lot_id!r}の1件のみ)と一致しません(fail-closed): "
            f"実際={[lot.lot_id for lot in stock_lots]}"
        )
    lot = stock_lots[0]
    if lot.shares != shares or lot.purchase_price not in (old_price, corrected_price):
        raise PlanValidationError(
            f"{stock_code}のlot_id={lot_id!r}が確定指示の値"
            f"(shares={shares}, "
            f"price={old_price}(訂正前)または{corrected_price}"
            f"(訂正後))と一致しません(fail-closed、想定外の価格への上書きを防止するため): "
            f"shares={lot.shares}, price={lot.purchase_price}"
        )


def _check_4680_precondition(
    holding: Holding, stock_lots: list[PurchaseLot], real_data: RealDataInput
) -> None:
    stock_code = real_data.split_stock_code
    _check_holding_matches(
        stock_code, holding, real_data.split_old_shares, real_data.split_old_average_price
    )
    if holding.total_purchase_amount != real_data.split_old_total_amount:
        raise PlanValidationError(
            f"{stock_code}の旧Holding.total_purchase_amountが確定指示の値"
            f"({real_data.split_old_total_amount})と一致しません(fail-closed): "
            f"{holding.total_purchase_amount}"
        )
    lots_by_id = {lot.lot_id: lot for lot in stock_lots}
    if set(lots_by_id) != set(real_data.split_lot_preconditions):
        raise PlanValidationError(
            f"{stock_code}のPurchaseLot構成が確定指示のlot_id集合と一致しません(fail-closed): "
            f"実際={sorted(lots_by_id)} 期待={sorted(real_data.split_lot_preconditions)}"
        )
    for lot_id, (expected_shares, expected_price) in real_data.split_lot_preconditions.items():
        lot = lots_by_id[lot_id]
        if lot.shares != expected_shares or lot.purchase_price != expected_price:
            raise PlanValidationError(
                f"{stock_code}のlot_id={lot_id!r}が確定指示の内容(shares={expected_shares}, "
                f"price={expected_price})と一致しません(fail-closed): "
                f"shares={lot.shares}, price={lot.purchase_price}"
            )


def _verify_preconditions(
    old_holdings: list[Holding],
    lots_by_stock_code: dict[str, list[PurchaseLot]],
    real_data: RealDataInput,
) -> None:
    """ユーザー確定済みの実データ(shares/取得単価/lot構成)を、実行のたびに
    このコード自身が検証する(2026-08-23確定指示、fail-closed)。対応する
    旧Holding(owner="本人")が既に存在しないstock_codeは、既に移行済みとみなし
    検証しない(冪等)。
    """
    old_holdings_by_stock = {h.stock_code: h for h in old_holdings}

    for stock_code, precondition in real_data.simple_preconditions.items():
        holding = old_holdings_by_stock.get(stock_code)
        if holding is None:
            continue
        _check_simple_precondition(
            stock_code, holding, lots_by_stock_code.get(stock_code, []), precondition
        )

    nine_four_three_four = old_holdings_by_stock.get(real_data.nine_four_three_four_stock_code)
    if nine_four_three_four is not None:
        _check_9434_precondition(
            nine_four_three_four,
            lots_by_stock_code.get(real_data.nine_four_three_four_stock_code, []),
            real_data,
        )

    split_holding = old_holdings_by_stock.get(real_data.split_stock_code)
    if split_holding is not None:
        _check_4680_precondition(
            split_holding, lots_by_stock_code.get(real_data.split_stock_code, []), real_data
        )


class ReclassificationAbortedError(Exception):
    """M4.1がfail-closedで中止された(pause未確認・想定外の状態等)。"""


class PlanValidationError(Exception):
    """計画構築時点でholding_id衝突・データ不整合を検出したためfail-closedで中止した。"""


# ============================================================================
# 計画構築(dry-run/実行いずれからも同じロジックを使う)
# ============================================================================


@dataclass(frozen=True)
class ReclassificationTarget:
    """1つの新holding(owner×stock_code)への移行計画。"""

    new_owner: str
    stock_code: str
    new_holding_id: str
    old_holding_id: str
    lot_ids: tuple[str, ...]
    is_split: bool
    price_corrected: bool
    # 4680分割時、既存InvestmentThesis/BaselineSequence/BaselinePointerを
    # 引き継ぐのはどちらか一方のみ(確定指示: 大きい持分側)。
    inherits_thesis_and_baseline: bool
    inherits_snapshot: bool


@dataclass(frozen=True)
class ReclassificationPlan:
    targets: tuple[ReclassificationTarget, ...]
    old_holding_ids: tuple[str, ...]


def _corrected_lot(lot: PurchaseLot, real_data: RealDataInput) -> PurchaseLot:
    corrected_price = real_data.price_corrections.get(lot.lot_id)
    if corrected_price is None:
        return lot
    return lot.model_copy(update={"purchase_price": corrected_price})


def _build_simple_target(
    holding: Holding, stock_lots: list[PurchaseLot], new_owner: str, real_data: RealDataInput
) -> ReclassificationTarget:
    if not stock_lots:
        raise PlanValidationError(
            f"stock_code={holding.stock_code!r}に対応するPurchaseLotが1件もありません"
            "(fail-closed)。"
        )
    normalized_owner = normalize_and_validate_owner(new_owner)
    new_holding_id = build_holding_id(normalized_owner, holding.stock_code)
    return ReclassificationTarget(
        new_owner=normalized_owner,
        stock_code=holding.stock_code,
        new_holding_id=new_holding_id,
        old_holding_id=holding.holding_id,
        lot_ids=tuple(lot.lot_id for lot in stock_lots),
        is_split=False,
        price_corrected=any(lot.lot_id in real_data.price_corrections for lot in stock_lots),
        inherits_thesis_and_baseline=True,
        inherits_snapshot=True,
    )


def _build_split_targets(
    holding: Holding, stock_lots: list[PurchaseLot], real_data: RealDataInput
) -> list[ReclassificationTarget]:
    by_owner: dict[str, list[PurchaseLot]] = {}
    for lot in stock_lots:
        owner = real_data.split_lot_owners.get(lot.lot_id)
        if owner is None:
            raise PlanValidationError(
                f"{real_data.split_stock_code}(分割対象)のlot_id={lot.lot_id!r}が"
                "split_lot_ownersに定義されていません(fail-closed)。想定外のlotが"
                "増えている可能性があるため、マッピングを確認してから再実行してください。"
            )
        by_owner.setdefault(owner, []).append(lot)

    # 大きい持分側がInvestmentThesis/BaselineSequence/BaselinePointerを
    # 引き継ぐ(確定指示: 既存thesisはconditions=0件の空状態のため、
    # 継承先として大きい持分側が自然)。
    inheriting_owner = max(by_owner, key=lambda o: sum(lot.shares for lot in by_owner[o]))

    targets: list[ReclassificationTarget] = []
    for owner, owner_lots in sorted(by_owner.items()):
        normalized_owner = normalize_and_validate_owner(owner)
        new_holding_id = build_holding_id(normalized_owner, holding.stock_code)
        targets.append(
            ReclassificationTarget(
                new_owner=normalized_owner,
                stock_code=holding.stock_code,
                new_holding_id=new_holding_id,
                old_holding_id=holding.holding_id,
                lot_ids=tuple(lot.lot_id for lot in owner_lots),
                is_split=True,
                price_corrected=any(
                    lot.lot_id in real_data.price_corrections for lot in owner_lots
                ),
                inherits_thesis_and_baseline=(owner == inheriting_owner),
                inherits_snapshot=(owner == inheriting_owner),
            )
        )
    return targets


def build_plan(
    holdings: list[Holding], lots: list[PurchaseLot], real_data: RealDataInput
) -> ReclassificationPlan:
    """その時点の実データからreclassification計画を構築する(副作用なし)。

    holding_idはstock_codeではなく実際に存在するHolding.owner=="本人"の
    行から導出する。stock_codeでlotsをグルーピングする(holding_idではなく)
    ことで、途中失敗後の再実行で一部lotのholding_idフィールドが既に新owner
    へ書き換わっていても正しく再グルーピングできる(4680分割の場合、
    lot単位でreal_data.split_lot_ownersにより最終的な帰属先を決定するため、
    現在のlot.owner値に依存しない)。
    """
    old_holdings = [h for h in holdings if h.owner == OLD_OWNER]
    lots_by_stock_code: dict[str, list[PurchaseLot]] = {}
    for lot in lots:
        lots_by_stock_code.setdefault(lot.stock_code, []).append(lot)

    _verify_preconditions(old_holdings, lots_by_stock_code, real_data)

    targets: list[ReclassificationTarget] = []
    for holding in old_holdings:
        stock_lots = lots_by_stock_code.get(holding.stock_code, [])
        if holding.stock_code == real_data.split_stock_code:
            targets.extend(_build_split_targets(holding, stock_lots, real_data))
        else:
            new_owner = real_data.child_owner_by_stock_code.get(
                holding.stock_code, real_data.default_new_owner
            )
            targets.append(_build_simple_target(holding, stock_lots, new_owner, real_data))

    new_ids = [t.new_holding_id for t in targets]
    duplicates = sorted({i for i in new_ids if new_ids.count(i) > 1})
    if duplicates:
        raise PlanValidationError(
            f"新holding_id内部で衝突を検出したためfail-closedで中止しました: {duplicates}"
        )

    # 新holding_idが既存の(owner!="本人"の)Holdingと衝突する場合、それが
    # 「本移行を部分的に再実行した結果として既に正しく書き込み済みの状態」
    # なのか、「本移行と無関係な既存データとの真の衝突」なのかを、lotから
    # 独立に再計算した期待値との内容一致で判定する(途中失敗後の再実行時、
    # 4680分割の片方だけが既に書き込まれている状態を誤って衝突と判定しない
    # ため)。内容が一致しない場合のみfail-closedで中止する。
    existing_by_id = {h.holding_id: h for h in holdings if h.owner != OLD_OWNER}
    lots_by_id = {lot.lot_id: lot for lot in lots}
    collisions: list[str] = []
    for t in targets:
        existing = existing_by_id.get(t.new_holding_id)
        if existing is None:
            continue
        target_lots = [_corrected_lot(lots_by_id[lot_id], real_data) for lot_id in t.lot_ids]
        total_shares, avg_price, total_amount, _first, _last = summarize_lots(target_lots)
        matches_expected = (
            existing.owner == t.new_owner
            and existing.stock_code == t.stock_code
            and existing.shares == total_shares
            and existing.average_purchase_price == avg_price
            and existing.total_purchase_amount == total_amount
        )
        if not matches_expected:
            collisions.append(t.new_holding_id)

    if collisions:
        raise PlanValidationError(
            "新holding_idが本移行対象外の既存Holdingと衝突したためfail-closedで"
            f"中止しました: {sorted(set(collisions))}"
        )

    return ReclassificationPlan(
        targets=tuple(targets),
        old_holding_ids=tuple(h.holding_id for h in old_holdings),
    )


# ============================================================================
# BaselineSequence(トップレベル属性、生boto3。M2のbaseline_migration.pyと
# 同じ理由でCollectionStoreの汎用upsertを使わない)
# ============================================================================


class _SequenceCounter(Entity):
    holding_id: str
    current_version: int
    updated_at: dt.datetime


def _get_sequence(holding_id: str, store_dir: Path | None) -> _SequenceCounter | None:
    if running_on_lambda():
        return _get_sequence_dynamodb(holding_id)
    return build_collection_store(
        _SequenceCounter, _SEQUENCE_FILE, "holding_id", store_dir
    ).get(holding_id)


def _get_sequence_dynamodb(holding_id: str) -> _SequenceCounter | None:
    import boto3

    table: Any = boto3.resource("dynamodb").Table(resolve_table_name(_SEQUENCE_FILE))
    response = table.get_item(Key={"holding_id": holding_id})
    item = response.get("Item")
    if item is None:
        return None
    return _SequenceCounter(
        holding_id=str(item["holding_id"]),
        current_version=int(item["current_version"]),
        updated_at=dt.datetime.fromisoformat(str(item["updated_at"])),
    )


def _put_sequence(entry: _SequenceCounter, store_dir: Path | None) -> None:
    if running_on_lambda():
        import boto3

        table: Any = boto3.resource("dynamodb").Table(resolve_table_name(_SEQUENCE_FILE))
        table.put_item(
            Item={
                "holding_id": entry.holding_id,
                "current_version": entry.current_version,
                "updated_at": entry.updated_at.isoformat(),
            }
        )
        return
    build_collection_store(_SequenceCounter, _SEQUENCE_FILE, "holding_id", store_dir).upsert(entry)


def _delete_sequence(holding_id: str, store_dir: Path | None) -> None:
    if running_on_lambda():
        import boto3

        table: Any = boto3.resource("dynamodb").Table(resolve_table_name(_SEQUENCE_FILE))
        table.delete_item(Key={"holding_id": holding_id})
        return
    build_collection_store(_SequenceCounter, _SEQUENCE_FILE, "holding_id", store_dir).delete(
        holding_id
    )


# --- BaselinePointer(標準CollectionStore、dataブロブ) -----------------------


def _get_pointer(holding_id: str, store_dir: Path | None) -> InvestmentThesisBaselinePointer | None:
    return build_collection_store(
        InvestmentThesisBaselinePointer, _POINTER_FILE, "holding_id", store_dir
    ).get(holding_id)


def _put_pointer(pointer: InvestmentThesisBaselinePointer, store_dir: Path | None) -> None:
    build_collection_store(
        InvestmentThesisBaselinePointer, _POINTER_FILE, "holding_id", store_dir
    ).upsert(pointer)


def _delete_pointer(holding_id: str, store_dir: Path | None) -> None:
    build_collection_store(
        InvestmentThesisBaselinePointer, _POINTER_FILE, "holding_id", store_dir
    ).delete(holding_id)


# ============================================================================
# Holding / HoldingsSnapshot 新レコード構築
# ============================================================================


def _new_holding_for_target(
    old_holding: Holding,
    target: ReclassificationTarget,
    target_lots: list[PurchaseLot],
    now: dt.datetime,
) -> Holding:
    total_shares, avg_price, total_amount, first_date, last_date = summarize_lots(target_lots)
    update: dict[str, Any] = {
        "owner": target.new_owner,
        "holding_id": target.new_holding_id,
        "shares": total_shares,
        "average_purchase_price": avg_price,
        "total_purchase_amount": total_amount,
        "first_purchase_date": first_date,
        "last_purchase_date": last_date,
        "updated_at": now,
    }
    if target.is_split and not target.inherits_thesis_and_baseline:
        # 分割で新設される側(小さい持分側)は、累積配当・優待受取実績や直近
        # 売却日等、旧Holding全体(400株)に紐づく履歴的な累積値をそのまま
        # 引き継がない(継承側=大きい持分側が旧Holdingの継続とみなされる
        # ことと対称的な設計。完了報告で明示し、実行前にユーザー確認を得ること)。
        update.update(
            {
                "cumulative_dividend_received": Decimal("0"),
                "cumulative_benefit_value_received": Decimal("0"),
                "last_sale_date": None,
            }
        )
    return old_holding.model_copy(update=update)


def _new_snapshot_for_target(
    old_snapshot: HoldingsSnapshotEntry | None,
    target: ReclassificationTarget,
    new_shares: int,
    new_average_price: Decimal,
    today: dt.date,
) -> HoldingsSnapshotEntry:
    if target.inherits_snapshot and old_snapshot is not None:
        # cooldown_until_date/last_trade_event_type/trade_detected_atは旧
        # スナップショットからそのまま引き継ぎ、shares/average_purchase_price
        # のみ分割後の値へ補正する(旧400株スナップショットをそのまま複製すると
        # 次回TradeCooldownService実行時に虚偽のPARTIAL_SELLイベントが誤検知
        # されるため。実コード(trade_event_detection.py)確認済み)。
        return old_snapshot.model_copy(
            update={
                "owner": target.new_owner,
                "holding_id": target.new_holding_id,
                "shares": new_shares,
                "average_purchase_price": new_average_price,
            }
        )
    # 引き継ぎ元が無い(分割で新設される側、または元々スナップショット未作成)
    # 場合は、cooldown状態を持たない新規baselineとして作成する。
    return HoldingsSnapshotEntry(
        owner=target.new_owner,
        holding_id=target.new_holding_id,
        stock_code=target.stock_code,
        shares=new_shares,
        average_purchase_price=new_average_price,
        recorded_at=today,
        last_trade_event_type=None,
        trade_detected_at=None,
        cooldown_until_date=None,
        active_holding=True,
    )


# ============================================================================
# pause強制確認
# ============================================================================


def _ensure_trading_paused(store_dir: Path | None) -> None:
    from jstock_advisor.infrastructure.aws import trading_pause_config

    try:
        config = trading_pause_config.get(store_dir)
    except Exception as e:
        raise ReclassificationAbortedError(
            f"TradingPauseConfigの取得に失敗しました(fail-closedで中止): {e}"
        ) from e
    if config is None:
        raise ReclassificationAbortedError(
            "TradingPauseConfigが未初期化です(fail-closedで中止)。"
            "先にtrading-pause initおよびsetでpause_buy_sell=trueにしてください。"
        )
    if not config.pause_buy_sell:
        raise ReclassificationAbortedError(
            "pause_buy_sell=falseのため中止しました(fail-closed)。"
            "先にtrading-pause set --buy-sellでBUY/SELLを一時停止してください。"
        )


# ============================================================================
# 実行本体
# ============================================================================


def _holding_id_matcher(holding_id: str) -> Any:
    def _matches(item: InvestmentThesis) -> bool:
        return item.holding_id == holding_id

    return _matches


@dataclass(frozen=True)
class ReclassificationResult:
    dry_run: bool
    plan: ReclassificationPlan
    processed_new_holding_ids: tuple[str, ...]
    skipped_already_migrated_old_holding_ids: tuple[str, ...]

    def render_text(self) -> str:
        lines = [
            f"M4.1 owner実態補正結果: {'DRY-RUN(書き込みなし)' if self.dry_run else '実行完了'}",
            "",
            f"[新holding_id(合計{len(self.processed_new_holding_ids)}件)]",
        ]
        for holding_id in self.processed_new_holding_ids:
            lines.append(f"  {holding_id}")
        if self.skipped_already_migrated_old_holding_ids:
            lines.append("")
            lines.append("[既に移行済みのためスキップした旧holding_id]")
            for holding_id in self.skipped_already_migrated_old_holding_ids:
                lines.append(f"  {holding_id}")
        return "\n".join(lines)


def run_reclassification(
    target: MigrationTarget,
    dry_run: bool,
    real_data: RealDataInput,
    store_dir: Path | None = None,
    now: dt.datetime | None = None,
) -> ReclassificationResult:
    """M4.1本体。実行直前にpause_buy_sell==trueであることをこのコード自身が
    確認する(fail-closed)。dry_run既定はTrue、書き込みには明示的な
    dry_run=Falseが必要。real_dataは`load_real_data_input()`でGit管理対象外の
    ローカルJSONファイルから読み込んだ実データ入力(個人情報除去対応、
    2026-08-25)。

    Store生成・読み書きはすべて単一のtarget_backend(target)コンテキスト内で
    行う(M2と同じ設計、local/AWSの途中混在を防ぐため)。
    """
    now_value = now or dt.datetime.now(dt.UTC)
    today = now_value.date()

    with target_backend(target):
        _ensure_trading_paused(store_dir)

        holding_store = build_collection_store(Holding, _HOLDINGS_FILE, "holding_id", store_dir)
        lot_store = build_collection_store(
            PurchaseLot, _PURCHASE_LOTS_FILE, "lot_id", store_dir
        )
        snapshot_store = build_collection_store(
            HoldingsSnapshotEntry, _SNAPSHOTS_FILE, "holding_id", store_dir
        )
        thesis_store = build_collection_store(
            InvestmentThesis, _INVESTMENT_THESES_FILE, "investment_thesis_id", store_dir
        )

        holdings = holding_store.list_all()
        lots = lot_store.list_all()

        plan = build_plan(holdings, lots, real_data)

        targets_by_old_id: dict[str, list[ReclassificationTarget]] = {}
        for t in plan.targets:
            targets_by_old_id.setdefault(t.old_holding_id, []).append(t)

        processed: list[str] = []
        skipped: list[str] = []

        for old_holding_id, group_targets in targets_by_old_id.items():
            old_holding = holding_store.get(old_holding_id)
            if old_holding is None:
                # build_plan()時点では存在したが処理直前に消えている
                # (単一プロセスCLIでは通常発生し得ない防御的分岐)。
                skipped.append(old_holding_id)
                continue

            old_snapshot = snapshot_store.get(old_holding_id)
            old_thesis_matches = thesis_store.find(_holding_id_matcher(old_holding_id))
            old_thesis = old_thesis_matches[0] if old_thesis_matches else None
            old_sequence = _get_sequence(old_holding_id, store_dir)
            old_pointer = _get_pointer(old_holding_id, store_dir)

            for t in group_targets:
                target_lots_raw = []
                for lot_id in t.lot_ids:
                    lot = lot_store.get(lot_id)
                    if lot is None:
                        raise ReclassificationAbortedError(
                            f"lot_id={lot_id!r}が見つかりません"
                            "(fail-closed、想定外の状態のため中止しました)"
                        )
                    target_lots_raw.append(lot)
                target_lots = [_corrected_lot(lot, real_data) for lot in target_lots_raw]

                new_holding = _new_holding_for_target(old_holding, t, target_lots, now_value)

                existing_new_snapshot = snapshot_store.get(t.new_holding_id)
                if existing_new_snapshot is None:
                    new_snapshot = _new_snapshot_for_target(
                        old_snapshot,
                        t,
                        new_holding.shares,
                        new_holding.average_purchase_price,
                        today,
                    )
                else:
                    # write-once: 新holding_id側に既に存在する場合は再導出・
                    # 上書きしない(旧スナップショットが既に削除済みの再試行時に
                    # cooldown状態を失わないため)。
                    new_snapshot = existing_new_snapshot

                if not dry_run:
                    holding_store.upsert(new_holding)
                    for lot in target_lots:
                        lot_store.upsert(
                            lot.model_copy(
                                update={"owner": t.new_owner, "holding_id": t.new_holding_id}
                            )
                        )
                    if existing_new_snapshot is None:
                        snapshot_store.upsert(new_snapshot)

                    if t.inherits_thesis_and_baseline:
                        if old_thesis is not None:
                            thesis_store.upsert(
                                old_thesis.model_copy(update={"holding_id": t.new_holding_id})
                            )
                        if old_sequence is not None:
                            _put_sequence(
                                old_sequence.model_copy(
                                    update={"holding_id": t.new_holding_id}
                                ),
                                store_dir,
                            )
                        if old_pointer is not None:
                            _put_pointer(
                                old_pointer.model_copy(
                                    update={"holding_id": t.new_holding_id}
                                ),
                                store_dir,
                            )

                processed.append(t.new_holding_id)

            if not dry_run:
                snapshot_store.delete(old_holding_id)
                if old_sequence is not None:
                    _delete_sequence(old_holding_id, store_dir)
                if old_pointer is not None:
                    _delete_pointer(old_holding_id, store_dir)
                # 旧Holdingの削除を必ず最後に行う(グループ完了の唯一の合図)。
                holding_store.delete(old_holding_id)

    return ReclassificationResult(
        dry_run=dry_run,
        plan=plan,
        processed_new_holding_ids=tuple(processed),
        skipped_already_migrated_old_holding_ids=tuple(skipped),
    )
