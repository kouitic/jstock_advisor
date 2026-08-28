"""ProviderBundle生成のファクトリ。

MVPのローカル動作確認用にモックProviderを提供するほか、実データ提供元
(yfinance + EDINET)を組み合わせた実運用向けBundleも提供する
(責務分離の設計方針: 要求仕様3節)。株主優待は自動取得できる公式データ源が
無いため、ユーザーが手動/CSVで登録したデータ(local_registry_impl)を使う
(未確定事項#5)。適時開示(TDnet)には公式APIが無いため、EDINET臨時報告書
(重大な会社情報の変更は金融商品取引法上EDINETにも提出義務がある)と
yfinanceの決算発表予定日を組み合わせた実データ実装を使う(決算短信そのものは
TDnet専用のため取得不可)。銘柄名はyfinanceからは英語名しか取得できないため、
EDINET提出書類のfilerName(日本語)を優先する(financial_dataとdividend_dataで
同じEdinetFilingCacheRepositoryを共有し、書類スキャンを重複させない)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from jstock_advisor.config.models import AppConfig
from jstock_advisor.infrastructure.edinet.client import EdinetClient
from jstock_advisor.infrastructure.edinet.disclosure_finder import EdinetDisclosureCacheRepository
from jstock_advisor.infrastructure.edinet.document_finder import EdinetFilingCacheRepository
from jstock_advisor.interfaces.candidate_universe import CandidateUniverseProvider
from jstock_advisor.providers.candidate_universe.csv_impl import CsvCandidateUniverseProvider
from jstock_advisor.providers.candidate_universe.jpx_impl import JpxCandidateUniverseProvider
from jstock_advisor.providers.corporate_action.local_registry_impl import (
    LocalRegistryCorporateActionProvider,
)
from jstock_advisor.providers.corporate_action.merged_impl import MergedCorporateActionProvider
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.corporate_action.yfinance_impl import YFinanceCorporateActionProvider
from jstock_advisor.providers.disclosure.edinet_yfinance_impl import (
    EdinetYfinanceDisclosureProvider,
)
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.cross_validating_impl import (
    CrossValidatingDividendDataProvider,
)
from jstock_advisor.providers.dividend_data.edinet_impl import EdinetDividendDataProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.dividend_data.policy_enrichment_impl import (
    ShareholderReturnPolicyEnrichingDividendDataProvider,
)
from jstock_advisor.providers.dividend_data.yfinance_impl import YFinanceDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.financial_data.yfinance_impl import YFinanceFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.market_data.yfinance_impl import YFinanceMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.local_registry_impl import (
    LocalRegistryShareholderBenefitProvider,
)
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services.corporate_action_service import CorporateActionService
from jstock_advisor.services.provider_bundle import ProviderBundle


def build_mock_provider_bundle(now: dt.datetime) -> ProviderBundle:
    return ProviderBundle(
        market_data=MockMarketDataProvider(now=now),
        financial_data=MockFinancialDataProvider(now=now),
        dividend_data=MockDividendDataProvider(now=now),
        shareholder_benefit=MockShareholderBenefitProvider(now=now),
        disclosure=MockDisclosureProvider(now=now),
        corporate_action=MockCorporateActionProvider(),
    )


def build_real_provider_bundle(now: dt.datetime, config: AppConfig) -> ProviderBundle:
    """yfinance(株価・財務・配当の主データ源)+ EDINET(配当のクロスバリデーション・適時開示用)。

    株主優待はユーザーの手動/CSV登録データ(local_registry_impl)を使う。
    """
    edinet_client = EdinetClient()
    edinet_filing_cache = EdinetFilingCacheRepository()

    corporate_action = MergedCorporateActionProvider(
        auto_provider=YFinanceCorporateActionProvider(now=now),
        manual_provider=LocalRegistryCorporateActionProvider(),
    )
    corporate_action_service = CorporateActionService(corporate_action, now=now)
    # Issue #30 Phase 1: 外部データ取得(yfinance+EDINET)の後段で、手動レジストリの
    # 株主還元方針(累進配当/DOE)をenrichする(責務分離。レジストリが正本)。
    dividend_data = ShareholderReturnPolicyEnrichingDividendDataProvider(
        inner=CrossValidatingDividendDataProvider(
            primary=YFinanceDividendDataProvider(
                now=now, corporate_action_service=corporate_action_service
            ),
            secondary=EdinetDividendDataProvider(
                client=edinet_client, cache_repository=edinet_filing_cache, now=now
            ),
            corporate_action_service=corporate_action_service,
            config=config.data_validation,
            now=now,
        ),
        policies=config.shareholder_return_policies,
    )
    return ProviderBundle(
        market_data=YFinanceMarketDataProvider(now=now),
        financial_data=YFinanceFinancialDataProvider(
            now=now, edinet_client=edinet_client, edinet_cache_repository=edinet_filing_cache
        ),
        dividend_data=dividend_data,
        shareholder_benefit=LocalRegistryShareholderBenefitProvider(),
        disclosure=EdinetYfinanceDisclosureProvider(
            client=EdinetClient(), cache_repository=EdinetDisclosureCacheRepository(), now=now
        ),
        corporate_action=corporate_action,
    )


def build_candidate_universe_provider(
    config: AppConfig, now: dt.datetime | None = None
) -> CandidateUniverseProvider:
    """ウォッチリスト自動追加機能向けの候補銘柄ユニバースProviderを選択する。

    "csv": 固定CSV(小規模検証用)。"jpx": 東証プライム+スタンダード全銘柄を
    自動取得(候補ユニバース本格対応・2026-08)。JpxCandidateUniverseProviderは
    S3/ローカルのキャッシュのみを読み、ネットワークアクセスは行わない(6節)。
    キャッシュの取得・検証・昇格はservices/candidate_universe_downloader.pyの
    DownloaderがDispatcher起動時に別途行う。
    """
    cu = config.watchlist_screening.candidate_universe
    if cu.provider == "csv":
        return CsvCandidateUniverseProvider(Path(cu.csv_path))
    if cu.provider == "jpx":
        if cu.target_market_segments is None:
            raise ValueError("candidate_universe.target_market_segmentsが未設定です")
        if cu.listed_issues_max_stale_hours is None or cu.jpx400_max_stale_hours is None:
            raise ValueError(
                "candidate_universe.listed_issues_max_stale_hours/"
                "jpx400_max_stale_hoursが未設定です"
            )
        return JpxCandidateUniverseProvider(
            target_market_segments=set(cu.target_market_segments),
            listed_issues_max_stale_hours=cu.listed_issues_max_stale_hours,
            jpx400_max_stale_hours=cu.jpx400_max_stale_hours,
            now=now or dt.datetime.now(dt.UTC),
        )
    raise ValueError(f"unknown candidate_universe provider: {cu.provider}")
