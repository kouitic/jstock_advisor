# 売却判定エンジン再設計 before/afterレポート(2026-07-29基準)

対象: 8306 三菱UFJフィナンシャル・グループ、4631 DIC、5401 日本製鉄、8136 サンリオ、2914 日本たばこ産業。

「Before」はユーザーのソースコードレビューで確認された旧ロジック(修正前のコード)がその条件でどう判定していたか、「After」は本レポート作成時点(2026-07-29 JST朝)に実データ(yfinance/EDINET)を使って新ロジックを実行した実際の出力。個別銘柄の例外登録は一切行っておらず、業種分類・独立根拠グループ・信頼度計算等の共通ロジックのみで判定が変化している。

## 1. 8306 三菱UFJフィナンシャル・グループ(MUFG)

実データ: `sector=Financial Services`, `industry=Banks - Diversified`, `equity_ratio_pct=5.16%`

- **Before**: `detect_financial_health_severe_deterioration`は業種を一切見ずに`equity_ratio_pct(5.16%) < equity_ratio_critical_pct(15.0%)`のみで判定 → `critical`該当。`judgment.critical_to_urgent_review_min_count=1`のため、この1件だけで即座に**URGENT_REVIEW**。銀行の自己資本比率は預金・貸出中心のビジネスモデル上、一般事業会社よりも構造的に低くなるのが通常であり、これは財務悪化ではなく業種特性の誤判定だった。
- **After**: `financial_health_severe_deterioration`は`sector`/`industry`から`classify_financial_industry`で`BANKING`と判定され、**NOT_EVALUATED**(「業種(BANKING)が金融業のため、一般事業会社向け自己資本比率ルールは適用しない」)。銀行専用指標(`regulatory_capital_breach`: CET1比率等)はデータソース未実装のため同じくNOT_EVALUATED。他の全ルールもNOT_TRIGGERED。
- **判定**: URGENT_REVIEW → **HOLD**(通知対象外)。

## 2. 4631 DIC

実データ: `actual_annual_dividend_per_share=200.0`(2025暦年、Dec特別配当150円を含む)、`forecast_annual_dividend_per_share=140.0`、`dividend_comparison_outcome=FORECAST_DIVIDEND_CUT`

- **Before**: `is_dividend_cut_announced`はyfinanceの暦年配当合計比較のみで`True`となり(140 vs 200、約30%減)、これがそのまま`dividend_cut`ルール(severity=major)の根拠となる。`judgment.major_to_sell_min_count=1`のため、この1件だけで**SELL**。実際には2025年12月の150円は特別配当(直近の支払履歴が30→50→50→50→**150**→70円と推移しており、突出した1回のみの支払)であり、普通配当が減配したわけではない。
- **After**: `official_dividend_cut_announced`は常に`False`(yfinance単独の推測を公式発表として扱わない、§11)。`dividend_cut`ルールは**NOT_TRIGGERED**(「一次情報で確認された正式な減配発表は無い」)。`inferred_dividend_decrease=True`は保持されるが、SELL判定の根拠には使わない(§12: yfinance単独で強い売却判定を出さない)。
- **判定**: SELL → **HOLD**(通知対象外)。

## 3. 5401 日本製鉄

実データ: `continuous_operating_income_decline=TRIGGERED`(営業利益2期連続悪化)、他ルールはNOT_TRIGGERED/NOT_EVALUATED。独立根拠グループ数=1。

- **Before**: major該当1件のみで`major_to_sell_min_count=1`により**SELL**。通知本文の「直ちに売却としない理由」は`len(reasons)==1`に基づく定型文("検出された懸念要因は…の1件のみです")で、実際の反対材料(増配実績等)は反映されなかった。`stop_review_price`は無条件に現在値へ設定されていた。
- **After**: 独立根拠グループが1件のみのため**REVIEW**(単一のルールだけではSELL/URGENT_REVIEWに進めない、§4)。信頼度はHIGH決め打ちではなく`MEDIUM`(実計算)。通知本文には実データに基づく反対材料「配当は1期連続増配中」、リスク説明「営業利益が2期連続で悪化している」、次の判断条件「次回決算発表(2026-08-04)後に本判定を再評価する」が、それぞれ判定エンジン側で生成された実際の値として表示される(通知層での自動生成なし、§9)。`immediate_execution_price`/`stop_review_price`はいずれもNone(算出不能を現在値へフォールバックしない、§7)。
- **判定**: SELL → **REVIEW**。

## 4. 8136 サンリオ

実データ: 全ルールNOT_TRIGGERED。

- **Before / After共通**: **HOLD**(変化なし)。分割調整・配当整合性等、以前のセッションで別途修正済みの問題を除き、本レンジの再設計による新規の回帰は確認されなかった。

## 5. 2914 日本たばこ産業

実データ: 全ルールNOT_TRIGGERED。

- **Before / After共通**: **HOLD**(変化なし)。

## まとめ

| 銘柄 | Before | After | 変化の理由 |
|---|---|---|---|
| 8306 MUFG | URGENT_REVIEW | HOLD | 業種別分類により一般事業会社向け自己資本比率ルールを金融業に非適用化(§2) |
| 4631 DIC | SELL | HOLD | 減配判定をofficial/inferredに分離し、yfinance単独推測をSELL根拠から除外(§10・§11・§12) |
| 5401 日本製鉄 | SELL | REVIEW | 単一major根拠ではSELLに進めない新ラダーを適用(§4) |
| 8136 サンリオ | HOLD | HOLD | 変化なし |
| 2914 日本たばこ産業 | HOLD | HOLD | 変化なし |

いずれの変化も個別銘柄名をコードに登録した結果ではなく、業種分類・独立根拠グループ・信頼度計算・データソース確認要件という共通ロジックの適用結果である。

## 未実装・feasibility制約(正直な申告)

- **§13 財務期間の構造化**(quarterly_operating_incomes/cashflowsをvalue/period_type/fiscal_year等の構造体に置き換える)は本パスでは実装していない。既存の`list[Decimal]`ベースの`detect_continuous_decline`をそのまま使用しており、四半期/累計/年次の混在を明示的に検出する仕組みは無い。影響は限定的(対象5銘柄では発現していない)だが、今後別途対応が必要。
- **配当の普通/特別/記念/臨時の実内訳**(§10の`ordinary_dividend_per_share`等)は、yfinance・EDINETいずれも支払種別を区別したデータを提供しないため、フィールドは追加したが常に`None`/`dividend_breakdown_confirmed=False`のままとなる恒久的な制約。§12のガード(yfinance単独では強い判定を出さない)で安全側に倒すことで実質的な誤判定は防止しているが、真の内訳分離自体は未達成。
- **銀行専用の健全性指標**(CET1比率等)は取得できるデータソースが無いため、`regulatory_capital_breach`は常にNOT_EVALUATED。金融業の財務健全性判定は現状「何も判定しない」状態であり、将来的にデータソースが確保できるまでは、金融業の財務悪化を検出する手段が無いことに留意。
- **MANUAL_REVIEW_REQUIRED**は、要求仕様§16が求める「修正完了までの一律自動停止」ではなく、新しい整合性検証(§15: 証拠1件のみのSELL/URGENT_REVIEW、独立根拠2件未満のHIGH、yfinance単独の強い判定等)に違反した場合の恒常的な安全弁として実装した。本パスで根本原因そのものを修正しているため、一律停止は不要と判断したが、この設計判断はユーザー確認が必要な事項として明記する。
