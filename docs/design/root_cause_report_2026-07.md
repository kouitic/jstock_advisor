# 通知品質問題 根本原因レポート(2026年7月)

## 対象事例

- 5401 日本製鉄(2025-10-01に1:5株式分割実施)
- 8136 サンリオ(2026-04-01に株式分割実施)
- 2914 日本たばこ産業(JT)

3事例とも、コードとDynamoDBの実データを調査した結果、個別銘柄固有の不具合ではなく、**株式分割調整の欠如**と**利確価格計算ロジックの設計バグ**という2つの共通原因に起因することを確認した。以下、原因ごとに独立してまとめる。

---

## 原因1: 株式分割調整がシステム全体のどこにも実装されていない

### 原因

- `providers/corporate_action/yfinance_impl.py::YFinanceCorporateActionProvider`はyfinanceの分割履歴を正しく取得できるが、**唯一の利用箇所**は`providers/dividend_data/cross_validating_impl.py`の`_reconcilable_with_splits()`であり、これは「2つのデータソースの配当額の乖離が分割比率で説明できるか」を判定するpass/failゲートに過ぎない。判定に使うだけで、**実際に値を補正して返すことはない**(`primary_info`をそのまま返す)。
- `providers/market_data/yfinance_impl.py::_fetch_history`は`auto_adjust=False`を明示的に指定しており、生の(分割未調整)株価を返す。この生データがそのまま3年間の適正価格レンジ計算・52週高値ドローダウン計算に使われる。
- `providers/financial_data/yfinance_impl.py::get_historical_valuation()`は常に`[]`を返すスタブであり、過去EPS/BPS系列(PER/PBR手法に必須)がそもそも存在しない。
- `domain/entities/holding.py::Holding.average_purchase_price`/`shares`は`services/portfolio_service.py::summarize_lots()`が`PurchaseLot`から集計するだけで、株式分割時に一切調整されない。
- `providers/dividend_data/yfinance_impl.py::_sum_by_calendar_year`は、支払日ごとの生配当額をそのまま暦年合計しており、分割が年の途中で発生した場合、分割前(高額面)と分割後(低額面)の支払いが同一暦年内に混在して合算される。

### 影響を受ける処理

- `domain/valuation/fair_value.py::compute_historical_range_price`(3年ウィンドウの生株価を使用)
- `domain/signals/buy_signal.py::compute_drawdown_from_52w_high_pct`、`estimate_historical_average_dividend_yield_pct`
- `services/profit_taking_service.py`(`holding.average_purchase_price`を未調整のまま使用 → 含み損益率が虚偽の値になる)
- `providers/dividend_data/yfinance_impl.py`の`actual_annual_dividend_per_share`/`is_dividend_cut_announced`算出

### 影響を受ける銘柄

分割・併合を過去に一度でも経験した全銘柄(過去3年以内は特に影響が大きい)。今回確認された5401・8136に加え、保有銘柄27件のうち過去に分割歴のある銘柄すべてが対象。

### 再現手順

`tests/unit/test_portfolio_service.py`に`test_sell_shares_*`系のテストがあるが、分割調整の有無を検証するテストは存在しなかった(=再現テストが無いこと自体が問題の一部)。新規追加した`tests/unit/test_corporate_action_service.py`の`test_adjust_price_for_2for1_split`等で、分割調整ロジックの不在・必要性を確認できる。

### 修正方針

`services/corporate_action_service.py`(新規)による一元的な調整機構を導入し、株価・EPS/BPS/DPS・保有銘柄の平均取得単価/株数・適正価格計算の入力すべてに適用する(実装計画: 本レポートと同ディレクトリのplanに記載の§2に対応)。既存保有銘柄はCLIコマンド`holdings recompute-all`で遡及調整する。

### 回帰テスト

`tests/unit/test_corporate_action_service.py`、`tests/unit/test_portfolio_service.py::test_recompute_all_adjusts_shares_and_price_for_past_split`

### 既存通知の再評価結果

`jstock review before-after --stocks 5401,8136,2914`の出力(`docs/design/before_after_recommendation_report.md`、実装完了後に生成)を参照。

---

## 原因2: `profit_taking.py::_compute_sell_prices`のmin/max非対称ロジックと無条件floor

### 原因

`domain/signals/profit_taking.py::_compute_sell_prices`は以下の設計になっていた。

```python
recommended_candidates = [p for p in (gain_full_price, fv_full_price) if p is not None]
recommended = min(recommended_candidates) if recommended_candidates else None   # 低い方(=既に到達済み)

full_candidates = [p for p in (gain_full_price, fv_full_price) if p is not None]
full_take = max(full_candidates) if full_candidates else None                   # 高い方(=未到達)
```

「利確推奨価格」には**低い方**(既に通過済みの閾値)、「全株利確検討価格」には**同じ2候補の高い方**(未到達の閾値)を採用するという、コードコメント上は意図的だが結果的に矛盾を生む設計だった。さらに、

```python
def _floor_at_current_price(price):
    return max(price, current_price) if price is not None else None
```

が4フィールド全てに無条件適用され、閾値を既に超過している場合は現在値へ静かに丸められる。`PriceWithRationale`には「丸められたか」を示すフラグが無いため、結果を見ても「本物の目標値」か「衝突で丸められた値」かを区別できない。

判定レベル自体は`raw_level = max(level_gain, level_fv, level_yield)`という3軸の独立判定だが、価格フィールドは`level_yield`(総合利回り低下)からは一切計算されない。JTのケースでは`level_gain`または`level_yield`単独でFULLへ到達していたにもかかわらず、表示価格は無関係な`fair_value`ベースの計算(`full_take_consider = 8,490円`)と、無関係な配当利回り逆算(`reassessment_price = 242円 ÷ 2.0% = 12,100円`)がそのまま出力されていた。

### 影響を受ける処理

`domain/signals/profit_taking.py::_compute_sell_prices`、`evaluate_profit_taking`、`services/line_notification_service.py::_format_profit_taking_message`(テンプレート自体は正しくマッピングしているが、上流の値が矛盾している)

### 影響を受ける銘柄

利確判定(PARTIAL/FULL_PROFIT_TAKE)が一度でも発火した全銘柄。2914(JT)で実際に発現。

### 再現手順

`tests/unit/test_profit_taking.py`に追加した回帰テスト(`test_full_take_price_never_below_recommended_limit_price`)で、旧ロジックでは`full_take_consider < profit_take_recommended`となるケースが再現できる。

### 修正方針

`_floor_at_current_price`を完全に削除し、8フィールド(§10)への再定義・条件ベースのFULL/PARTIAL判定(§8-11)に置き換える。算出不能時はNone(「算出不能」表示)とし、現在値と一致する場合は理由(即時執行目安 or 監視開始価格)を明示する。

### 回帰テスト

`tests/unit/test_profit_taking.py`(全面改訂)、`tests/unit/test_recommendation_consistency_validator.py`(新規)

### 既存通知の再評価結果

`docs/design/before_after_recommendation_report.md`を参照。

---

## 原因3: 減配判定が異なる基準(予想レート vs 実績暦年合計)を比較している

### 原因

`providers/dividend_data/yfinance_impl.py`:

```python
forecast_annual = _to_decimal(info.get("dividendRate"))  # 現在時点の指標的な予想配当率
...
actual_annual = Decimal(str(round(yearly_totals[complete_years[-1]], 2)))  # 直近の完了暦年の実績合計
...
if forecast_annual < actual_annual:
    is_dividend_cut_announced = True
```

`forecast_annual`はyfinanceの「現在の」指標的な予想配当率、`actual_annual`は「直近に完了した暦年」の実績合計であり、同一年度の実績vs予想でも、前年vs今年のYoY実績同士でもない。しかも実際に比較した暦年(`complete_years[-1]`)はローカル変数のまま捨てられ、`DividendInfo.fiscal_year`には`str(self._now.year)`(単なる「今年」)が入る。ユーザーが指摘した通り、5401では比較年度によって「減配」の判定が逆転する(直近年度比では維持、2年前比では25%減)。

### 影響を受ける処理

`providers/dividend_data/yfinance_impl.py::get_dividend_info`、`domain/signals/sell_signal.py`(`dividend_cut`/`dividend_omission`トリガー)

### 影響を受ける銘柄

配当実績データを持つ全銘柄。5401で実際に誤判定を確認。

### 再現手順

`tests/unit/test_dividend_cut_analysis.py`(新規)の`test_classify_split_adjustment_only_not_a_cut`等。

### 修正方針

`domain/signals/dividend_cut_analysis.py::classify_dividend_change()`(新規)で、比較対象年度・分割調整後DPS・実績/予想の別を明示的に保存し、6種類の`DividendComparisonOutcome`に分類する。予想同士の比較は「予想減配」と表示し、確定的な「減配」と混同しない。

### 回帰テスト

`tests/unit/test_dividend_cut_analysis.py`

### 既存通知の再評価結果

`docs/design/before_after_recommendation_report.md`を参照。

---

## 副次的に確認された問題(通知の信頼性に影響)

| 項目 | 内容 |
|---|---|
| 信頼度がほぼ固定値 | `sell_signal_service.py`は`confidence=ConfidenceLevel.HIGH`をハードコード。`profit_taking_service.py`の`_confidence_for`もPER/PBRが常時Noneのため実質MEDIUM上限で頭打ち。 |
| 整合性検証が皆無 | `line_notification_service.py::notify_recommendation`は判定と価格の整合性を一切検証せずに送信していた。 |
| 監査ログが浅い | `AuditService.record`の`output_values`にはsell_pricesの4値・`level_gain`/`level_fv`/`level_yield`の個別値・`fair_value`が記録されておらず、事後のトレーサビリティが不十分だった。 |
| 権利確定月「不明」 | `dividend_record_dates`はyfinance/EDINETいずれも常に`[]`(意図的な「推測しない」設計)。恒久的なデータソース制約であり、優待側の権利確定日(取得可能)とは区別して扱う必要がある。 |
| 銘柄タイプ・モメンタム指標が存在しない | GROWTH/INCOME/CYCLICAL等の分類や移動平均・RSI・MACD等の技術指標は全く実装されていなかった。 |

これらは根本原因1・2・3ほど直接的ではないが、通知の信頼度表示・整合性・追跡可能性に関わるため、実装計画(`C:\Users\kouit\.claude\plans\ethereal-popping-aurora.md`)の該当節で合わせて修正する。
