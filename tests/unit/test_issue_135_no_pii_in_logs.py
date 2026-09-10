"""Issue #135 O-A: Production のログ・例外 message へ所有者と holding_id を出さない。

`holding_id` は `<所有者>#<銘柄コード>` であり、所有者は実在人物を指す。これを
logger の書式引数や例外 message へ渡すと、その値は CloudWatch Logs を読める
principal へ露出する。**実行時に書き出す先も「記録」である**(CLAUDE.md)。

置換後は `sha256:` + SHA-256 の先頭 8 文字(`log_ref()`)を出す。所在は失われない
(運用者は候補の holding_id を手元で同じ形にハッシュして突き合わせられる)。

```
本ファイルの値はすべて**架空値**である。
所有者は "所有者A"、銘柄コードは実在しない "0000" を使う。
実在人物の氏名・実際の保有データは記載しない。Production への注入も行わない。
```
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import logging
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import ExecutionMode, NotificationMode
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.owner import (
    InvalidOwnerError,
    build_holding_id,
    log_ref,
    split_holding_id,
    validate_owner,
)
from jstock_advisor.services.investment_thesis_service import InvestmentThesisService

# 架空の所有者と銘柄コード。実在しない値だけを使う。
_OWNER = "所有者A"
_STOCK_CODE = "0000"
_HOLDING_ID = build_holding_id(_OWNER, _STOCK_CODE)


def _expected_ref(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}"


# --- T-5  ハッシュ形式が Issue #63 PR-2 と同一であること ----------------------


def test_log_ref_is_sha256_prefix_of_eight_characters() -> None:
    """★ 形式を固定する。

    #63 PR-2 の `ItemIdDisclosure.HASH` と #131 の公開面 PII 検出は同じ形を使う。
    実装は共有しない(#63 側は共通部品 S-17 にあり、依存を作ると以後この用途の
    都合で S-17 を触る動機が生まれる)ため、**形式が同じであることを両側の
    テストで固定する**。ここがずれると読む側が 2 つの規則を覚えることになる。
    """
    assert log_ref(_HOLDING_ID) == _expected_ref(_HOLDING_ID)
    assert log_ref(_HOLDING_ID).startswith("sha256:")
    assert len(log_ref(_HOLDING_ID)) == len("sha256:") + 8


def test_log_ref_matches_the_issue_63_hash_for_the_same_input() -> None:
    """#63 側の実装と同じ入力で同じ文字列になること(将来ずれないための固定)。"""
    from jstock_advisor.infrastructure.record_failure_policy import (
        ItemIdDisclosure,
        _disclose_item_id,
    )

    assert log_ref(_HOLDING_ID) == _disclose_item_id(_HOLDING_ID, ItemIdDisclosure.HASH)


# --- T-1 / T-2  E-1 logger の書式引数 ----------------------------------------


def test_validation_log_does_not_contain_the_owner_but_does_contain_the_ref(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """★ E-1 の実経路。所有者は出ず、符号は出る。

    符号まで確認するのは、「消しただけで所在が追えなくなっていない」ことを
    同時に固定するためである(調査の手が落ちていないこと)。
    """
    service = InvestmentThesisService(
        store_dir=tmp_path,
        execution_context=ExecutionContext(
            mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
        ),
    )

    with caplog.at_level(logging.INFO):
        service.get_or_create_thesis(_HOLDING_ID, _STOCK_CODE)

    assert _OWNER not in caplog.text, "所有者名がログへ出てはならない"
    assert _HOLDING_ID not in caplog.text, "holding_id そのものが出てはならない"
    assert _expected_ref(_HOLDING_ID) in caplog.text, "符号は出る(所在は失われない)"


def test_no_logger_call_passes_owner_or_holding_id_as_a_format_argument() -> None:
    """★ T-6  AST guard。書式引数へ生の値を渡す箇所が **0 件**であることを固定する。

    CI の pii-scan は denylist であり、**架空値や将来追加される所有者は
    検出できない**。ここは「値そのもの」ではなく「渡し方」を見るため、
    denylist に依存せず経路を塞げる(#179 / #131 と同じ、検査で経路を塞ぐ形)。

    `log_ref(...)` を通した引数は許す。それが本 Issue の置換後の形である。
    """
    offenders = _scan_logger_format_arguments()

    assert offenders == [], f"logger の書式引数へ生の値を渡している: {offenders}"


def test_no_logger_call_passes_an_aggregate_object_as_a_format_argument() -> None:
    """★ T-6b  AST guard。**集約オブジェクトを丸ごと**渡す箇所が 0 件であることを固定する。

    Issue #309: T-6 は書式引数の**ソース断片の文字列**に `holding_id` / `owner` が
    含まれるかで判定していた。そのため

        logger.info("... single holding done: %s", result)

    のように **dict を変数 1 つで丸ごと渡す**形は、断片が `"result"` でしかなく
    ★ **構造的に当たらない**(すり抜けたのではなく検査対象になっていない)。
    実際にこの経路から所有者名が Production の CloudWatch Logs へ出ていた。

    ここでは名前ではなく **束縛の実体**を見る。同じ関数の中でその名前が
    dict リテラル、または `-> dict[...]` / `-> Mapping[...]` と注釈された関数の
    戻り値に束縛されているなら、中身が何であれ offender とする。

    ★ 「いま PII を含んでいないから良い」とはしない。dict を丸ごと出す形が
      残っている限り、後からキーが 1 つ増えただけで再び漏れるためである。

    ★ 限界を明記する: 本 guard が判定できるのは dict リテラルと dict/Mapping 注釈の
      関数戻り値だけである。dataclass / BaseModel の丸ごと出力は **検出できない**。
      検査で塞げていない範囲があることを、テスト側に残しておく。
    """
    offenders = _scan_logger_aggregate_arguments()

    assert offenders == [], f"logger の書式引数へ集約オブジェクトを丸ごと渡している: {offenders}"


_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
#: 集約とみなす戻り値注釈。`-> Any` は広すぎるため含めない(誤検知を避ける)。
_AGGREGATE_ANNOTATIONS = ("dict[", "Dict[", "Mapping[", "MutableMapping[")

#: レビュー済みで PII を含まないと確認した集約。**既定は禁止**であり、
#: ここへ追加してよいのは次の 2 つを満たす場合だけである(Issue #309)。
#:
#:   1  キー集合がモジュール定数等で**固定**されており、後から増えない
#:   2  値に所有者・保有数量・取得価格・通知本文が入り得ない
#:
#: ★ 限界: 照合は (モジュール, 変数名) であり行番号を見ない。同じモジュールに
#:   同名の別変数が現れると、そちらも免除されてしまう。追加時は現物を読むこと。
_REVIEWED_NON_PII_AGGREGATES = {
    # batch_summary の件数内訳。キーは _BATCH_SUMMARY_CATEGORIES(モジュール定数)で
    # 固定され、値は int のみ。所有者・銘柄・金額を含まない。
    ("services/line_notification_service.py", "counts"),
}


def _iter_logger_calls(tree: ast.AST) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _LOG_METHODS:
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "logger"):
            continue
        yield node


def _scan_logger_format_arguments() -> list[str]:
    offenders: list[str] = []
    for path in sorted(pathlib.Path("src").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for node in _iter_logger_calls(ast.parse(source)):
            for argument in node.args[1:]:
                segment = ast.get_source_segment(source, argument) or ""
                if "log_ref(" in segment:
                    continue
                if "holding_id" in segment or "owner" in segment:
                    offenders.append(f"{path}:{node.lineno}: {segment}")
    return offenders


def _aggregate_returning_functions(source: str, tree: ast.Module) -> set[str]:
    """`-> dict[...]` 等と注釈された関数名を集める(同一モジュール内のみ)。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.returns is None:
            continue
        annotation = ast.get_source_segment(source, node.returns) or ""
        if annotation.startswith(_AGGREGATE_ANNOTATIONS):
            names.add(node.name)
    return names


def _names_bound_to_aggregates(
    source: str, function: ast.FunctionDef | ast.AsyncFunctionDef, aggregate_funcs: set[str]
) -> set[str]:
    """関数内で dict リテラル / dict を返す関数へ束縛されている名前を集める。

    1 つでも集約に束縛されていれば集約とみなす(再代入で紛れるのを防ぐ)。
    """
    bound: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not targets:
            continue
        value = node.value
        is_aggregate = isinstance(value, ast.Dict | ast.DictComp)
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            is_aggregate = is_aggregate or value.func.id in aggregate_funcs
        if is_aggregate:
            bound.update(targets)
    return bound


def _scan_logger_aggregate_arguments() -> list[str]:
    offenders: list[str] = []
    for path in sorted(pathlib.Path("src").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        aggregate_funcs = _aggregate_returning_functions(source, tree)
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            aggregates = _names_bound_to_aggregates(source, function, aggregate_funcs)
            if not aggregates:
                continue
            for node in _iter_logger_calls(function):
                for argument in node.args[1:]:
                    if not (isinstance(argument, ast.Name) and argument.id in aggregates):
                        continue
                    if _is_reviewed_non_pii(path, argument.id):
                        continue
                    offenders.append(f"{path}:{node.lineno}: {argument.id}")
    return offenders


def _is_reviewed_non_pii(path: pathlib.Path, name: str) -> bool:
    suffix = path.as_posix().removeprefix("src/jstock_advisor/")
    return (suffix, name) in _REVIEWED_NON_PII_AGGREGATES


# --- T-3  E-2 例外 message ----------------------------------------------------


def test_owner_validation_errors_do_not_leak_the_owner_and_keep_the_type() -> None:
    """★ 例外 message も露出面である(捕捉されず traceback ごとログへ出うる)。

    **型は変えない。** `InvalidOwnerError` を捕捉している呼び出し元の挙動を
    変えないためである(本 Issue はログの内容だけを変える)。
    """
    too_long = "所" * 21

    with pytest.raises(InvalidOwnerError) as long_error:
        validate_owner(too_long)
    assert too_long not in str(long_error.value)
    assert _expected_ref(too_long) in str(long_error.value)

    with_delimiter = f"{_OWNER}#x"
    with pytest.raises(InvalidOwnerError) as delimiter_error:
        validate_owner(with_delimiter)
    assert with_delimiter not in str(delimiter_error.value)
    assert _OWNER not in str(delimiter_error.value)

    doubled = f"{_OWNER}#{_OWNER}#{_STOCK_CODE}"
    with pytest.raises(InvalidOwnerError) as split_error:
        split_holding_id(doubled)
    assert _OWNER not in str(split_error.value)
    assert _expected_ref(doubled) in str(split_error.value)


def test_no_raise_embeds_owner_or_holding_id_in_the_changed_modules() -> None:
    """★ E-2 の 13 箇所を、個別の再現ではなく構造で固定する。

    例外を実際に起こすには CAS 競合や DynamoDB 状態の作り込みが要るものが
    あり、再現の手間に対して得られる保証が小さい。ここで見たいのは
    「message に生の値を埋めていないこと」そのものなので、埋め込み式を
    直接検査する。
    """
    modules = [
        "src/jstock_advisor/domain/entities/owner.py",
        "src/jstock_advisor/infrastructure/aws/baseline_pointer.py",
        "src/jstock_advisor/services/portfolio_service.py",
        "src/jstock_advisor/services/investment_thesis_service.py",
    ]
    offenders: list[str] = []
    for module in modules:
        source = pathlib.Path(module).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            for argument in node.exc.args:
                if not isinstance(argument, ast.JoinedStr):
                    continue
                for part in argument.values:
                    if not isinstance(part, ast.FormattedValue):
                        continue
                    segment = ast.get_source_segment(source, part.value) or ""
                    if "log_ref(" in segment or segment.startswith("len("):
                        continue
                    if "holding_id" in segment or "owner" in segment:
                        offenders.append(f"{module}:{node.lineno}: {segment}")

    assert offenders == [], f"例外 message へ生の値を埋めている: {offenders}"


# --- T-4  E-3 通知本文の複製 --------------------------------------------------


def test_dry_run_log_omits_the_message_body_but_the_audit_record_keeps_it(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """★ DRY_RUN のログへ通知本文を出さない。監査記録には残す。

    LINE の本文には所有者名を**残す**(通知の宛先はご本人であり、複数の
    所有者を扱ううえで識別にも必要なため = O-D)。だからこそ、その本文を
    そのままログへ複製すると実名が CloudWatch Logs へも出る。

    確認手段は失われない。監査記録側(`output_values.message_text`)に本文が
    残り、`content_hash` で同一性も追える。
    """
    from jstock_advisor.services.line_notification_service import LineNotificationService

    body = f"【保有】{_OWNER}さんの{_STOCK_CODE}は継続保有です"
    recorded: list[dict[str, Any]] = []

    class _SpyAudit:
        def record(self, **kwargs: Any) -> None:
            recorded.append(kwargs)

    service = LineNotificationService.__new__(LineNotificationService)
    service._audit = _SpyAudit()  # type: ignore[attr-defined]
    service._execution_context = ExecutionContext(  # type: ignore[attr-defined]
        mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
    )

    with caplog.at_level(logging.INFO):
        service._record_dry_run_notification(
            body,
            notification_type=None,
            stock_code=_STOCK_CODE,
            content_hash="hash-1",
            related_recommendation_id=None,
            now=dt.datetime(2026, 9, 7, 0, 0, tzinfo=dt.UTC),
        )

    assert body not in caplog.text, "通知本文をログへ出してはならない"
    assert _OWNER not in caplog.text, "本文に含まれる所有者名も出てはならない"
    assert "VALIDATION DRY_RUN" in caplog.text, "抑止した事実自体は残る"
    assert "content_hash=hash-1" in caplog.text, "同一性は content_hash で追える"

    assert recorded, "監査記録は残る"
    assert recorded[0]["output_values"]["message_text"] == body, "確認手段は失われない"
