# 開発ワークフロー・運用ガバナンス

**この文書の位置づけ**

本リポジトリにおける開発の進め方(lane / WIP / 実装パイプライン / テスト方針 /
記録先 / 検証 / release 判断 / 人間承認の境界)の**正本(SSoT)**である。

AI 非依存のリポジトリ運用ポリシーであり、特定の AI エージェントやセッションに
依存しない。[CLAUDE.md](../CLAUDE.md) は本文書への入口にすぎない。

label の 4 軸モデル(Issue Type / Priority / Severity / Release Blocker)そのものは
[docs/issue_label_policy.md](issue_label_policy.md) が正本である。本文書は label の
意味を定義せず、**release-blocker の lifecycle**についてのみ同文書と接続する。

Production の具体的な運用手順(deploy 手順・テーブル追加時の注意・
read-only 観測での副作用確認等)は [docs/operations_manual.md](operations_manual.md)
が正本である。本文書はそれらの手順を複製しない。

**ルールを変更する場合は本文書を更新する。**

---

## 0. 現在のモード: Stabilization Sprint

2026-09-02 より、開発運用を **STABILIZATION_SPRINT / PIPELINE_MODE** とする
(Issue #122、人間承認済み)。

```
期間            約10営業日
新機能          原則停止
P2 / P3 修正    原則停止
P0              即対応
P1              最優先で着手
queue reorder   原則1日1回
割込み          P0 のみ即割込み可
```

安定化を最優先し、既存の未解消 P0/P1 を短期で殲滅することを目的とする。
Sprint 終了後は 1 節以降の lane 定義のみを残し、「P2/P3 原則停止」は解除する。

---

## 1. Lane(A)

Issue を次の 4 lane へ割り当てる。上位 lane を優先する。

| Lane | 対象 |
|---|---|
| **Lane0** | Production incident / P0 |
| **Lane1** | P1 の `bug` / `design-defect` |
| **Lane2** | Production Verification Wait(実装・deploy 済みで検証待ち) |
| **Lane3** | P2 / P3 / `enhancement` |

- **P0 は即時割込み可。** 他 lane の作業中でも中断して着手してよい。
- Lane2 の Issue は**新規実装候補から除外する**(実装は完了しているため)。
- queue の並び替えは原則 1 日 1 回に限る(頻繁な組み替えで着手が分散するのを防ぐ)。
  P0 の割込みはこの制限の対象外。

Issue の実装状況は次の区分で表現する。

| 区分 | 意味 |
|---|---|
| `CODE_WORK_REQUIRED` | 実装が必要。implementation WIP の対象 |
| `PRODUCTION_VERIFICATION_ONLY` | 実装・deploy 済み。検証の完了待ち |
| `HUMAN_DECISION_BLOCKED` | 人間の判断・外部前提の確定待ち |
| `ALREADY_IMPLEMENTED` | 対応済み。close 判断のみ残る |
| `NOT_READY` | 依存 Issue の未完了等により着手できない |

---

## 2. WIP 制限(B)

AI エージェント 1 体あたり:

```
IMPLEMENTATION_WIP <= 1
INVESTIGATION_WIP  <= 2
```

- **同一 Issue の parallel implementation を禁止する。**
  1 つの Issue に対する implementation owner は常に 1 体
  (`one Issue / one implementation owner`)。
  他エージェントが担当する Issue の branch / PR を操作しない。
- **PR CI 待ち / review 待ち / Production verification 待ちは、
  implementation WIP から解放してよい。** これらの待ち時間を
  AI の待機理由にしない。次の Issue へ進む。
- **ただし同一 Issue へ修正が必要になった場合は、最優先で戻る。**
  review 指摘・CI 失敗・verification 失敗が発生したら、
  新しく着手した作業より優先して対応する。

---

## 3. 実装パイプライン(C)

標準の流れ:

```
implementation
  → targeted pytest
  → related regression
  → ruff
  → mypy
  → local commit
  → remote branch push
  → ChatGPT remote diff review
  → PASS
  → PR
  → required CI
  → MERGE_READY
  → HUMAN MERGE APPROVAL
  → merge
  → main CI
```

- **push 前の ChatGPT review は不要。**
  push は **review checkpoint** として扱い、レビュワーが GitHub 上の確定 diff を
  直接参照できるようにする。
- push は通常 push とし、**force push を使わない**
  (remote branch 公開後の履歴 rewrite を避けるため、rebase ではなく
  `git merge --no-ff origin/main` で最新 main を取り込む)。
- **merge には人間の明示承認が必要**(9 節)。
- merge method は通常の merge commit。squash / rebase merge / auto-merge /
  `--admin` は使用しない。承認対象の exact SHA に対してのみ成立させるため
  `--match-head-commit` を指定する。

### レビュー対象の指定

branch の分岐点が古い場合、`git diff origin/main..HEAD` は**他 Issue の変更を
削除差分として表示する**。レビュー依頼時は必ず merge-base を明示する。

```
正しい   git diff <merge-base>..<HEAD>
誤り     git diff origin/main..HEAD   ← 他 Issue の変更が削除として混入する
```

### Issue の自動 close を避ける

PR 本文では、Issue に後続 Phase が残る場合や Production 自然検証を close 条件と
する場合、`Fixes` / `Closes` を使わず **`Refs #xxx`** を使う。
`Fixes #xxx` は merge 時に Issue を自動 close するため、検証前に close されてしまう。

---

## 3.5 時間意味論変更ゲート(C')

Issue #52 / #143 / #148 は、いずれも「時刻・営業日・市場セッションの扱い」に
起因した。本節はその再発を上流で止めるためのゲートである(Issue #145)。

**大多数の PR には何も課さない。** トリガに該当する変更にのみ適用する。

### 3.5.1 トリガ判定

次の diff があるときだけ `TIME_SEMANTICS_IMPACT = YES` となる。
判定はファイルパスと diff の有無で決まり、著者・レビュワーの主観に依存しない。

```
T1  domain/market_session.py / business_calendar.py / jst.py / price_freshness.py
T2  provider が返す日付・時刻の決定ロジック
    (as_of_date / fetched_at / bar date / 取得窓の start・end / timezone 変換 /
     外部ライブラリの日付境界の使い方)
T3  時刻由来値を受け取って業務分岐する consumer
T4  test / mock / fixture の clock または期待日
```

T4 を含めるのは、**Issue #143 が「テスト期待値の変更」として現れた**ためである。

### 3.5.2 control identifiers

```
C-BM  固定 clock の境界マトリクス(全境界)
C-BS  分岐する状態のみの固定 clock テスト
C-MG  mutation guard(緩める方向の変更で必ず落ちること)
C-CO  affected cohort の組み合わせ実行(同一プロセス)
C-CS  clock 固定化の影響範囲分析(どの cohort に属するかの特定と記録)
C-PC  provider contract テスト
C-EL  外部ライブラリの境界仕様の独立根拠(version を必ず記録)
C-TZ  timezone / 取得窓の証拠
C-MD  mock 期待値が provider contract 由来であること(リテラルの期待日で固定)
```

### 3.5.3 決定表

| TRIGGER | REQUIRED_CONTROLS |
|---|---|
| **T1** | `C-BM` `C-MG` `C-CO` |
| **T2** | `C-PC` `C-EL` `C-TZ` `C-MD` |
| **T3** | `C-BS` |
| **T4** | `C-CS` `C-CO` + 変更対象が属する層(T1/T2/T3)の control |

**T1-T4 は階層ではない。** 番号の大小に上下関係はなく、それぞれが独立した
control の集合である。T1 は T2 を包含しない(共有ヘルパを変えても provider
契約の証拠は得られない)。

複数トリガに該当する場合:

```
REQUIRED_CONTROLS = 該当する全トリガの control の union
```

「より強いトリガを1つ選ぶ」方式は採らない。control が欠落するためである。

```
例  T1 + T2  ->  C-BM C-MG C-CO C-PC C-EL C-TZ C-MD
    T2 + T4  ->  C-PC C-EL C-TZ C-MD C-CS C-CO
    T3 のみ  ->  C-BS
```

### 3.5.4 control の内容

**`C-BM`(T1 のみ)** — 次の境界を固定 clock で網羅する。

```
寄付直前 / 寄付ちょうど / 立会中 / 大引け1分前 / 大引けちょうど /
大引け後 / 非営業日 / 連休明け / UTC-JST 日跨ぎ / 真の未来日
```

業務上存在しない境界は N/A としてよいが、**理由を明記する**。無言の省略は不可。

**`C-BS`(T3)** — 実際に分岐する状態のみでよい。**無関係な全境界は不要。**
最も件数が多いのは T3 であり、ここへ全境界を課さないことが本ゲートを軽く保つ要点である。

**`C-CO` / `C-CS`(T4)** — 1 ファイルだけ clock を固定して他モジュールとの整合を
壊さないこと。固定化後、単独実行だけでなく **cohort の組み合わせ実行**で回帰が
無いことを確認する。cohort は `tests/unit/test_time_semantics_guard.py` の
registry に定義されている。

Issue #143 は単独実行と全件 CI では確認したが、**部分集合の組み合わせ実行を
確認しなかった**ため Issue #148 を露出させた。

**`C-EL`(T2)** — 「このコードがそう書いてあるから、実装者はそう理解している
はず」は**根拠にしない**。official docs / installed source / upstream source /
read-only experiment のいずれかで確認し、**version とともに記録する**。

**`C-MD`(T2)** — mock の期待値を判定側 helper から自己参照的に導出した
assertion は、恒真になりうるため provider contract の証拠と認めない。

```
不可  assert snapshot.as_of_date <= latest_plausible_bar_date(now, calendar)
      (mock が helper ちょうどで打ち切る実装なら失敗しえない)

可    局面ごとにリテラルの期待日を固定する
      ("PRE_OPEN_0800", _BUSINESS_DAY, 8, 0, _PREV_BUSINESS_DAY)
```

### 3.5.5 証拠の扱い

```
主証拠  固定 clock の境界マトリクス / mutation guard /
        provider の取得窓分析 / 外部ライブラリ境界の独立根拠
補助    CI がどの時刻(局面)で回ったか
```

**CI が「今の時刻で」green であることを correctness の主証拠にしない。**
Issue #143 では、大引け後に回った CI の green が false green であった。

導入しないもの:

```
CI の複数時刻実行 / 時刻別 matrix job / sleep・wait / 現在時刻依存テスト
```

検出確率を上げるだけで非決定性そのものは残るため、対策として誤りである。
正しい対策はテストを時刻非依存にすることに帰着する。

`full pytest` のローカル必須化も行わない(4節の方針を変えない)。

### 3.5.6 registry

時刻に敏感なテストモジュールは
`tests/unit/test_time_semantics_guard.py` の registry に登録する。
registry 自身の健全性(登録漏れ・削除・化石化した例外)も同ファイルで検証される。

```
FORBIDDEN         wall clock 呼び出しがあれば FAIL
ALLOWED_EXISTING  既存の残存リスク。rationale と owner Issue が必須。
                  負債が解消され呼び出しが 0 件になったら FAIL し、
                  FORBIDDEN への更新を促す
```

**全テストファイルを走査する方式は採らない。** 市場セッションに接触しない
wall clock の使用は正当であり、リスクベースの登録制とする。

### 3.5.7 参照 contract(Issue #52)

日足 bar の妥当性について確定した契約。ドキュメント上の参照であり、
本節が Production の挙動を定めるものではない。

```
寄付前              当日付の bar は存在し得ない        -> fail-close
立会中(未確定)     当日付の bar は存在しうる          -> 正常
大引け後(確定)     当日付の bar は存在する            -> 正常
非営業日            当日付の bar は存在し得ない        -> fail-close
翌日以降の日付      その時点で存在し得ない            -> fail-close
```

### 3.5.8 PR での宣言

`.github/PULL_REQUEST_TEMPLATE.md` に従い、非該当なら
`TIME_SEMANTICS_IMPACT = NO` の1行のみでよい。該当する場合は
`TRIGGERS` / `REQUIRED_CONTROLS` / `EVIDENCE` を記録する。

`NO` の宣言は免罪符ではない。変更内容と矛盾する場合、レビュワーはこれを FAIL とする。
PR 本文を CI で自動解析する仕組みは導入しない。

---

## 4. ローカルテスト方針(D)

原則、ローカルでは次のみを実行する。

```
targeted pytest        変更した箇所を直接covertするテスト
related regression     変更が影響しうる周辺(-k で絞り込む)
ruff check src tests
mypy src
```

**full suite は PR CI(required 7 checks)で実行する。**
理由なく local full suite を毎回実行しない(1 回 20 分規模を要し、着手速度を
大きく損なうため)。

ただし **full suite の local 実行は禁止ではない。** 事故調査・共有コードへの
広範な変更・CI では再現しない環境依存の切り分け等で必要な場合は実行してよい。
その場合は**実行した理由を記録する**。

docs のみの変更では pytest / mypy / ruff を機械的に回す必要はない(11 節)。

---

## 5. GitHub への永続化(E)

次は**会話上の報告だけで完結させず、GitHub へ永続化する**。

- investigation(調査結果・再現・root cause)
- design decision(採用案と不採用案、その理由)
- verification result(Immediate / Natural / Negative-path)
- human decision(承認内容と承認対象の exact 識別子)
- merge / deploy result(SHA・CI 結果・ChangeSet ARN 等)

**会話のみを SSoT にしない。** セッションが変わっても同じ前提で作業を継続できる
ことを要件とする。

永続化先は原則として対象 Issue のコメント。運用ルール自体の変更は本文書へ。

---

## 6. 現況判断と status freshness(F)

Issue の現況は、次の 3 つを**総合して**判断する。

1. 最新の確定 status comment
2. current labels
3. Issue body

**どれか 1 つが常に正しいと決めない。** とくに「labels が Acceptance Criteria より
常に正しい」といった単純な優先順位を置かない。3 者が矛盾する場合は、
勝手に推測して実装を進めず、**どれが最新の確定判断かを確認する**
([CLAUDE.md](../CLAUDE.md) の既存ルール)。

Issue body に旧状態の記載が残ることは許容する。**historical evidence として
保持してよい。**

### close 前の最小同期

close する前に、**現況と矛盾する次の箇所のみ**を同期する。

- status(Issue state / verification 状態 / Production 反映状態)
- Production 状態の記載(旧 SHA・旧 deploy gate 等)
- Acceptance Criteria の checkbox

同期は**訂正の追記**によって行い、旧記載そのものを削除しない。
「上記は当時の状態であり、現況は次のとおり」と分かる形にする。

**Problem / Evidence / Root cause / 旧判断・旧 AC の取り下げ記録 /
accepted residual risk 等の歴史的記録を無闇に削除しない。**

---

## 7. Negative-path verification(G)

negative path(異常系)の検証要求を 2 種へ分類する。

### `MANDATORY_FOR_RELEASE_BLOCKER_REMOVAL`

その検証が完了するまで `release-blocker` を解除できない。
Issue 本文の Production Verification Plan に明示する。

### `OPTIONAL_POST_RELEASE_OBSERVATION`

自然な障害発生を待つことを必須としない。次の**事前に定義された代替証拠**が
揃っていることをもって判断してよい。

- unit / contract tests(mutation 検証で検出力を確認したもの)
- CI required checks
- Immediate Verification(deploy 直後の read-only 確認)
- 正常系の natural evidence(自然実行が正常終端していること)

そのうえで **ChatGPT review PASS + 人間判断**により解除可否を決める。

### 分類を勝手に変えない

**Issue 自身が「自然な negative-path observation」を Acceptance Criteria として
明示している場合、これを勝手に `OPTIONAL` へ変更しない。**
分類の変更には Issue の更新と人間の判断が要る。

### Production failure injection は禁止

Production で `AccessDenied` / provider failure / Lambda failure 等を
**人工的に発生させない**。必要と判断される場合は別途人間の明示承認を得る。

---

## 8. AWS / CloudWatch の pagination(H)

**「存在しない」「件数が少ない」ことを根拠に Production の欠陥や正常性を
主張する場合、pagination の完了を必須とする。**

対象例:

- CloudWatch Logs `filter_log_events`(`nextToken`)
- DynamoDB `Scan` / `Query`(`LastEvaluatedKey`)
- CloudFormation の list 系 API(`NextToken`)
- Lambda の list 系 API(`NextMarker`)

**単一 page を全件と解釈しない。** 最初の 1 ページだけを見て
「0 件」「1 件しかない」と判定してはならない。

件数を報告する場合は、pagination を完了させたうえでの件数であることを示す。
上限を設けて打ち切った場合は、**打ち切った事実と上限値を明記する**
(黙って truncate した結果を全件のように報告しない)。

なお、検索条件そのものの取りこぼしにも注意する。名前・文字列パターンで
絞り込む場合、対象が別表現で記録されている可能性を確認する
(例: CloudFormation の Processed テンプレートでは、テーブル名の実体ではなく
論理 ID で参照されている)。

---

## 9. Production grouped release(I)

複数 Issue をまとめて release する場合、次を**すべて**満たすこと。

```
SCOPE_EXTERNAL_OPEN_RELEASE_BLOCKERS = 0
  (release scope 外に OPEN な release-blocker が無い)
included Issues がすべて merge 済み
各 Issue の Production Verification Plan が既知
先行する mandatory verification に未完了が無い
included Issues が相互に競合しない
release inventory を再構築済み(baseline → target の全 commit を列挙し、
  意図しない Issue 由来の commit が混入していないことを確認)
人間が exact release candidate(SHA)を承認
```

### release scope 内の blocker remediation は release へ含められる

第1条件は「**release scope 外**に OPEN な release-blocker が無いこと」である。
`OPEN な release-blocker = 0` ではない。

`release-blocker` の lifecycle は
[docs/issue_label_policy.md](issue_label_policy.md) §6 のとおり

```
blocker 付与 → 修正の merge → Production deploy → Immediate Verification
  → mandatory verification → ChatGPT review → human approval → blocker 解除
```

であり、**Production deploy が blocker 解除の前提**である。したがって
「OPEN blocker が 1 件でもあれば deploy できない」と適用すると循環し、
Production-target defect を恒久的に remediate できなくなる。

release scope 内の `release-blocker` については、次を**すべて**満たす場合に
release へ含めてよい。

```
今回の release がその blocker の remediation を含む
その修正が merge 済み
その Issue の Production Verification Plan が定義済み
deploy 後も blocker を維持する(deploy では解除しない)
mandatory verification + ChatGPT review + human approval まで解除しない
```

これは「OPEN blocker を無視してよい」という緩和ではない。
**解除条件は一切緩めない。** deploy はあくまで verification の前提であり、
deploy 自体が blocker を解除する根拠にはならない。

release scope 外に OPEN な `release-blocker` がある場合は、その blocker の
block 条件を確認し、今回の release を止めるものかどうかを判断する。
止めるものであれば grouped release へ進まない。

**`release-blocker` label の有無だけで release 可否を判断しない。**
各 blocker の `BLOCKER_MODE` / `BLOCKING_TARGET` / `BLOCKER_SCOPE` を確認する。
これらの記録形式と `DEFECT_BLOCK` / `VERIFICATION_HOLD` の定義、および
`BLOCKER_REMEDIATION_RELEASE_IS_NOT_BLOCKED_BY_ITS_OWN_BLOCKER` は
[docs/issue_label_policy.md](issue_label_policy.md) §6 が正本である。

### release inventory の追跡要件

Production release 監査では、baseline → target の**全 commit** について
次を追跡できること。

```
ISSUE
PR
COMMIT
BLOCKER_MODE
BLOCKING_TARGET
VERIFICATION_STATUS
```

Issue へ辿れない commit が baseline → target に含まれる場合は、
その commit の由来を確定させるまで release へ進まない。

### blocker remediation release への piggyback は禁止

Production が**障害状態**にあり、それを解消するための release
(`BLOCKER_REMEDIATION_RELEASE`)には、**無関係な変更を相乗りさせない**。

この場合の release scope は「その障害の修正のみ」であることを、
baseline → target の commit 列挙で確認する。
`単に main が最新だから OK` としない。

`BLOCKER_REMEDIATION_RELEASE`(Production 障害の解消専用)と、
複数の Production-target blocker をまとめて解消する通常の grouped release は
別物である。前者は scope を障害修正のみに限定する。後者は上記の条件を
満たす限り複数 Issue を含めてよいが、**いずれの場合も
release scope 外の変更を便乗させない**点は共通である。

---

## 9.5 すべての変更を Issue 起点とする

```
NO_BEHAVIOR_OR_OPERATIONAL_CHANGE_WITHOUT_ISSUE
```

理由を問わず、**挙動・構成・運用・契約へ影響する変更は必ず GitHub Issue を
起点とする。**

対象:

```
application code
infrastructure
IAM
config / threshold
schema
CI/CD
operational behavior
Production-affecting docs
governance rules
```

変更理由が次のいずれであっても Issue は必須である。

```
bug fix / design correction / requirement addition / requirement change
refactor / security improvement / operations improvement
```

**「小さい変更だから」「ついでだから」を Issue 省略の理由にしない。**

### Issue 省略を許す唯一の例外

純粋な doc-only であり、次の**両方**を満たすものだけ Issue なしを許容してよい。

```
NO_BEHAVIOR_CHANGE    = YES
NO_OPERATIONAL_CHANGE = YES
```

例: typo 修正 / 表現だけの修正 / 壊れたリンク修正 / Markdown formatting。

この場合も PR 本文へ次を記録する。

```
ISSUE_EXCEPTION       = DOC_ONLY_NON_BEHAVIORAL
NO_BEHAVIOR_CHANGE    = YES
NO_OPERATIONAL_CHANGE = YES
```

**判断が曖昧なら Issue を作る側へ倒す。**
**governance rule の変更はこの例外に含まれない。** 本文書のような workflow 変更は
Issue 必須である。

### Issue Type は既存体系を維持する

新しい Type label を不用意に追加しない。
[docs/issue_label_policy.md](issue_label_policy.md) §3 の

```
bug / design-defect / enhancement / investigation
calibration / tracking / not-a-bug / accepted-risk
```

を維持する。`requirement-change` / `refactor` / `security` / `operations` /
`governance` 等は、現時点では**新しい Type 軸を作らず**、
既存 Type + Issue 本文の Reason / Scope で表現する。

```
新要件            -> enhancement
セキュリティ改善   -> bug / design-defect / enhancement を実態で判定
governance 変更   -> enhancement / tracking 等を実態で判定
```

4軸モデル(Issue Type / Priority / Severity / Release Blocker)を崩さない。

### commit / PR と Issue の追跡

Issue 必須の変更では、**PR が必ず Issue を参照する。**

```
Refs #xx    基本。Production Verification pending の Issue では
            merge 時点で自動 close させないため必ずこちらを使う
Fixes #xx   Issue 全体をその PR で完了させる場合のみ
```

commit 単位でも、どの Issue 由来かを release inventory から追跡可能にする。
**main への直接 push は禁止**(既存方針を維持)。

---

## 10. 人間承認の境界(J)

次の操作には**人間の明示承認が必要**である。Sprint による高速化でこれらを
緩和しない。

```
merge
Production deploy
ChangeSet creation
ChangeSet execution
manual Production Lambda invocation
Production data write
migration / backfill
failure injection
```

補足:

- **ChangeSet の作成も Production 操作として扱う**(execute だけではない)。
- ChangeSet の execute 承認は **exact ChangeSet ARN に対してのみ有効**。
  ChangeSet を再作成した場合、その承認は失効する。新しい ARN を提示して
  再承認を得る。
- **Issue close と release-blocker 解除は別判断**である
  ([docs/issue_label_policy.md](issue_label_policy.md) §6)。
- 承認は**その操作・その対象に限る**。ある文脈での承認を別の文脈へ拡張しない。
- 作業指示の**宛先が異なる**場合(他エージェント宛の承認)、その承認をもって
  操作しない。宛先を確認する。

### 併せて緩和しないもの

```
one Issue / one implementation owner
hidden-write 確認([CLAUDE.md](../CLAUDE.md) / operations_manual 18節)
duplicate Issue 確認(起票前に必ず duplicate search)
release-blocker lifecycle(issue_label_policy §6)
Production failure injection 禁止
```

---

## 11. docs のみの変更(DOC_ONLY_CHANGE)

変更が Markdown / ドキュメントのみの場合:

```
実施する    git diff --check(空白・行末の破損確認)
            PII / secret の混入確認
            相対リンク・パスの整合確認
            Markdown 構造の確認(見出し階層・表・コードフェンス)
実施不要    pytest / mypy(コード変更が無いため)
            ruff(Markdown のみなら対象外)
```

報告では `DOC_ONLY_CHANGE=YES` を明示する。

本節は doc-only 変更の**検証手順**を定めるものであり、**Issue の要否とは別**である。
Issue なしで進められるのは §9.5 の `ISSUE_EXCEPTION=DOC_ONLY_NON_BEHAVIORAL`
(`NO_BEHAVIOR_CHANGE` かつ `NO_OPERATIONAL_CHANGE`)を満たす場合に限る。
**governance rule の変更は doc-only であっても Issue 必須**である。

---

## 変更履歴

| 日付 | 変更概要 |
|---|---|
| 2026-09-02 | 新規作成(Issue #122)。Stabilization Sprint の lane / WIP 制限 / 実装パイプライン / ローカルテスト方針 / GitHub 永続化 / status freshness / negative-path 分類 / AWS pagination / grouped release / 人間承認の境界を正本化した。既存の Human merge approval・Human Production approval・exact ChangeSet approval・release-blocker lifecycle・one Issue / one implementation owner・hidden-write 確認・Production failure injection 禁止・duplicate Issue 確認はいずれも緩和していない |
| 2026-09-03 | §9 の grouped release 第1条件を「OPEN な release-blocker = 0」から「release scope 外に OPEN な release-blocker が無い」へ修正(Issue #122)。release-blocker lifecycle(docs/issue_label_policy.md §6)では Production deploy が blocker 解除の前提であるため、旧記述では Production-target defect の remediation release が循環して実施不能になる不整合があった。あわせて release scope 内 blocker を release へ含めるための条件(remediation を含む / merge 済み / Verification Plan 定義済み / deploy 後も blocker 維持 / mandatory verification + ChatGPT review + human approval まで解除しない)を明文化し、`BLOCKER_REMEDIATION_RELEASE` と通常の grouped release の区別を補足した。**解除条件は一切緩和していない** |
| 2026-09-03 | release-blocker の blocking target semantics を導入(Issue #122)。`BLOCKER_MODE`(`DEFECT_BLOCK` / `VERIFICATION_HOLD`)・`BLOCKING_TARGET` 等の必須記録と `BLOCKER_REMEDIATION_RELEASE_IS_NOT_BLOCKED_BY_ITS_OWN_BLOCKER` は docs/issue_label_policy.md §6 を正本とし、本文書 §9 はそれを参照する。§9 の第1条件を `SCOPE_EXTERNAL_OPEN_RELEASE_BLOCKERS = 0` として明示し、release inventory の追跡要件(ISSUE / PR / COMMIT / BLOCKER_MODE / BLOCKING_TARGET / VERIFICATION_STATUS)を追加した。あわせて §9.5 `NO_BEHAVIOR_OR_OPERATIONAL_CHANGE_WITHOUT_ISSUE`(挙動・構成・運用・契約へ影響する変更は Issue 必須。例外は `NO_BEHAVIOR_CHANGE` かつ `NO_OPERATIONAL_CHANGE` の doc-only のみで、governance 変更は例外に含まない)・既存 Issue Type 体系の維持・Refs/Fixes の使い分け・main 直接 push 禁止の再確認を追加した。**既存の解除条件・piggyback 禁止・人間承認境界・4軸独立性はいずれも緩和していない** |
| 2026-09-03 | §3.5「時間意味論変更ゲート」を新設(Issue #145)。Issue #52 / #143 / #148 の再発を上流で止めるため、時刻・営業日・市場セッション・timezone・外部ライブラリの日付境界に触れる変更へのみ適用するゲートを定めた。トリガ T1-T4 をファイルパスと diff の有無で客観判定し、control(C-BM / C-BS / C-MG / C-CO / C-CS / C-PC / C-EL / C-TZ / C-MD)を決定表で対応づける。**T1-T4 は階層ではなく独立した control 集合**であり、複数該当時は union を要求する(「より強いトリガを1つ選ぶ」方式は control が欠落するため採らない)。最も件数の多い consumer 変更(T3)には分岐する状態のみを課し、全境界マトリクスは共有ヘルパ変更(T1)に限定する。トリガ非該当の PR には追加負担を課さない。外部ライブラリの境界仕様はコードからの推測を根拠とせず version つきの独立根拠を要求し、mock の期待値を判定側 helper から自己参照的に導出した assertion は provider contract の証拠と認めない。CI の実行時刻は補助証拠に留め、**CI の複数時刻実行・時刻別 matrix job・sleep/wait・現在時刻依存テストは導入しない**(検出確率を上げるだけで非決定性が残るため)。full pytest のローカル必須化も行わない(§4 の方針は不変)。あわせて .github/PULL_REQUEST_TEMPLATE.md と tests/unit/test_time_semantics_guard.py(registry と registry 自身の健全性検証)を追加した。**判定ロジック・通知内容・保存データ形式・Production 挙動はいずれも変更していない**(開発運用ゲートの追加のみ) |
