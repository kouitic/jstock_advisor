<!--
このテンプレートは Issue #145(時刻・業務日・市場セッション依存変更の開発ゲート)
に基づく。判定基準は docs/development_workflow.md 3.5節を参照すること。
-->

## 概要

<!-- 何を、なぜ変更したか。関連 Issue を `Refs #<番号>` で記載する。 -->

Refs #

---

## TIME_SEMANTICS_IMPACT

<!--
時刻・日付・営業日・市場セッション・timezone・外部ライブラリの日付境界に
影響しないなら NO の1行のみでよい。追加の記入は不要。
-->

TIME_SEMANTICS_IMPACT = NO

<!--
YES の場合のみ以下を記入する。
TRIGGERS と REQUIRED_CONTROLS は 3.5節の決定表から機械的に導出すること
(「これは軽いから省略」という判断はしない)。複数 trigger に該当する場合、
REQUIRED_CONTROLS は各 trigger の control の **union** である。

TRIGGERS          =
REQUIRED_CONTROLS =
EVIDENCE          =

N/A とする control がある場合は理由を併記する。無言の省略は不可。

order-sensitive cohort(3.5.6 の registry で ORDER_CASES を持つ cohort)に
該当する場合は、EVIDENCE へ実行証拠を必ず記録する。order case の実行は
自動化していないため、記録が無いと実質的に検証されない。

ORDER_CASES_EXECUTED =
ORDER_CASE_RESULTS   =

その order case が KNOWN_FAILURE_ISSUE を持つ場合はさらに:

EXPECTED_FAILURE_SET =
ACTUAL_FAILURE_SET   =
FAILURE_SET_MATCH    = YES | NO

FAILURE_SET_MATCH = NO の場合、差分は当該変更の regression として扱う
(「red だが既知」で済ませない)。
order-sensitive でない cohort には、この追加記録を求めない。
-->

> **`TIME_SEMANTICS_IMPACT = NO` は免罪符ではない。**
> 変更内容と NO 宣言が明らかに矛盾する場合(例: `domain/market_session.py` に
> diff があるのに NO、provider の `as_of_date` 決定に diff があるのに NO、
> registry 登録モジュールの期待日を変えているのに NO)、reviewer はこれを FAIL とする。

---

## 確認

- [ ] `ruff check src tests`
- [ ] `mypy src`
- [ ] targeted tests / related regression(4節のローカルテスト方針に従う)
- [ ] 仕様に影響する変更なら `docs/functional_spec.md` を更新し、変更履歴に追記した
