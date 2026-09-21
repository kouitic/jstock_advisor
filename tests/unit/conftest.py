from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from jstock_advisor.infrastructure.collection_store import running_on_lambda
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import WatchlistRepository
from jstock_advisor.services import holding_decision_runtime_config_service as runtime_config_module
from jstock_advisor.services import jpx_industry_source as jpx_industry_source_module
from jstock_advisor.services.csv_import_ledger import CsvImportLedger
from jstock_advisor.services.csv_import_service import HoldingsCsvImportService
from jstock_advisor.services.jpx_industry_source import (
    JpxIndustryEntry,
    reset_default_jpx_industry_source,
)
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.watchlist_service import WatchlistService


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    return tmp_path / "local_store"


@pytest.fixture
def portfolio_service(store_dir: Path) -> PortfolioService:
    return PortfolioService(
        holding_repository=HoldingRepository(store_dir=store_dir),
        lot_repository=PurchaseLotRepository(store_dir=store_dir),
    )


@pytest.fixture
def watchlist_service(store_dir: Path) -> WatchlistService:
    return WatchlistService(repository=WatchlistRepository(store_dir=store_dir))


@pytest.fixture
def csv_import_ledger(store_dir: Path) -> CsvImportLedger:
    """Issue #61 Phase B1: 取込済み台帳もtmp_pathへ隔離する
    (既定のAuditLogRepositoryを使うとテストが実データ領域へ書き込むため)。"""
    return CsvImportLedger(repository=AuditLogRepository(store_dir=store_dir))


@pytest.fixture
def csv_import_service(
    portfolio_service: PortfolioService, csv_import_ledger: CsvImportLedger
) -> HoldingsCsvImportService:
    return HoldingsCsvImportService(portfolio_service=portfolio_service, ledger=csv_import_ledger)


@pytest.fixture(autouse=True)
def _isolated_jpx_industry_source(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, JpxIndustryEntry]]:
    """JPX業種ソース(Issue #54 Phase B-1の観測用)をunit testから隔離する。

    `JpxIndustrySource` はプロセス内共有インスタンス(`_DEFAULT_SOURCE`)を持ち、
    その中に成功マップとnegative cacheのtimestampを保持する。テスト間で漏れると
    実行順序で結果が変わるため、本fixtureが **各テストの前後で必ずreset** する。
    resetは共有インスタンスそのものを破棄するため、成功マップとtimestampの
    双方が同時に消える(片方だけ残ることはない)。

    既定のローダは空マップ = 「一覧は読めたが当該銘柄が無い」(`NOT_FOUND`)。
    実キャッシュを読むと、開発者のローカルにdata_j.xlsが落ちているかどうかで
    テスト結果が変わり、`CandidateUniverseCacheIO` がキャッシュディレクトリを
    作る副作用も生じるため、実装関数ごと差し替える。

    JPXで解決できる状態を再現したいテストはyieldされるdictへ登録する。
    ローダ自体を検証するテストは、この差し替えを実装関数へ戻したうえで
    内側のキャッシュIOを差し替える(tests/unit/test_jpx_industry_source.py)。
    """
    entries: dict[str, JpxIndustryEntry] = {}
    monkeypatch.setattr(jpx_industry_source_module, "_load_jpx_industry_map", lambda: entries)
    reset_default_jpx_industry_source()
    yield entries
    reset_default_jpx_industry_source()


def reset_holding_decision_runtime_config_cache() -> None:
    """`holding_decision_runtime_config_service`のプロセス内cache(2つのモジュール変数)を破棄する。

    `_cached_config`(直近に取得できた設定)と`_cached_at`(その取得時刻)は、常に**対**で
    持ち越される。片方だけを消すと、TTL判定が食い違うため、必ず同時に消す。
    """
    runtime_config_module._cached_config = None
    runtime_config_module._cached_at = None


@pytest.fixture(autouse=True)
def _isolated_holding_decision_runtime_config_cache() -> Iterator[None]:
    """保有判断のRuntimeConfig cache(Issue #148)を、unit testから隔離する。

    `HoldingDecisionRuntimeConfigService.get_config()`は、取得に成功した設定を**モジュール
    レベル**の`_cached_config` / `_cached_at`へ保持し、後続の取得失敗(レコード未作成を含む)では
    安全側の既定値(LEGACY)ではなく、この持ち越した値を使う。テスト間でリセットされないため、
    先行テストが書いた mode(SHADOW / ACTIVE)が、別の保存先で動く後続テストへ漏れ、
    新エンジンが呼ばれて偽の失敗になる(#148 の11件失敗。実行順序で結果が変わる)。

    本fixtureが**各テストの前後で必ずresetする**(monkeypatchの復元では、漏れた値へ戻るため
    使わない)。cacheそのものの挙動を検証するテストは、テスト内で明示的にcacheを設定する
    (tests/unit/test_holding_decision_runtime_config.py)。
    """
    reset_holding_decision_runtime_config_cache()
    yield
    reset_holding_decision_runtime_config_cache()


_LAMBDA_ENV_FUNCTION_NAME = "jstock-advisor-test-lambda"


@pytest.fixture
def lambda_runtime_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Issue #367(b): mocked AWS(moto)テストを、本番と同じ「Lambda上」の経路で動かす。

    本番のrepository / batch trackerは`running_on_lambda()`(=環境変数
    `AWS_LAMBDA_FUNCTION_NAME`の有無)でDynamoDBかローカルJSONかを選ぶ。motoを使う
    テストがこの変数を設定しないと、同じテスト内で「motoのDynamoDB」と「ローカルJSON
    フォールバック」が混在し、本番には無い組み合わせを検証してしまう(#275 H11)。

    **opt-in**: autouseにしない。対象テストが明示的に要求する。
    **環境変数で設定する**: module属性だけをmonkeypatchするとrepository側と
    batch tracker側で経路が分裂するため、プロセス全体で`running_on_lambda()`が
    Trueになる形にする。fixture自身がそれを確認する。
    """
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", _LAMBDA_ENV_FUNCTION_NAME)
    assert running_on_lambda() is True
    yield


@pytest.fixture
def assert_dynamodb_backend(lambda_runtime_env: None) -> Callable[[object], None]:
    """Issue #367(b)条件4: 「fixtureを付けただけ」で完了扱いにしないための確認。

    `build_collection_store()`が返したstoreがDynamoDBバックエンドであることを
    テスト内でassertするための関数を返す(JSONへフォールバックしていないことの証明)。
    """
    from jstock_advisor.infrastructure.aws.dynamodb_store import DynamoDbCollectionStore

    def _check(store: object) -> None:
        assert isinstance(store, DynamoDbCollectionStore), (
            f"DynamoDBバックエンドを通っていない: {type(store).__name__}"
        )

    return _check


@pytest.fixture
def create_collection_table() -> Callable[..., None]:
    """Issue #367(b): motoへ、repositoryが本番で使うcollection表(HASHキー1本)を作る。

    表名は本番と同じ`resolve_table_name(file_name)`で決める(表名をテスト側へ
    ハードコードして本番とずれることを避ける)。`mock_aws()`の内側で呼ぶこと。
    """
    import boto3

    from jstock_advisor.infrastructure.collection_store import resolve_table_name

    def _create(file_name: str, id_field: str, *, region: str = "ap-northeast-1") -> None:
        boto3.client("dynamodb", region_name=region).create_table(
            TableName=resolve_table_name(file_name),
            KeySchema=[{"AttributeName": id_field, "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": id_field, "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    return _create


@pytest.fixture(autouse=True)
def _market_calendar_is_business_day_by_default(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #440: 市場休場日gate(`_market_holiday`)の休場判定を、既定では「常に営業日」にする。

    3つの市場依存entry(BuyCandidates / HoldingsWatchlist parent、WatchlistDispatcher
    NEW_CANDIDATE)は、JPX休場日に判定・通知を行わずno-opで返る。既存の親経路のテストは、
    実時刻(`dt.datetime.now`)で動き、`{"dispatched": n}`のような返り値を厳密に比較する。
    実時刻が土日祝のとき(CIが週末に走る等)にそれらが落ちないよう、gateの休場判定だけを
    既定で「営業日」へ固定する(テストが実行時刻に依存しないようにする。Issue #143と同じ方針)。

    gateそのものを検証するテストは、`@pytest.mark.real_market_calendar`(モジュール単位なら
    `pytestmark`)で実際の判定(BusinessCalendar)へ戻す。
    """
    if request.node.get_closest_marker("real_market_calendar") is not None:
        return
    from jstock_advisor.lambda_handlers import _market_holiday

    monkeypatch.setattr(
        _market_holiday, "is_market_closed", lambda business_date_jst, config: False
    )
