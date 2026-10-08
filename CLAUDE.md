# CLAUDE.md

## 0. この文書の読み方

本ファイルは、このリポジトリで作業するすべての AI が読み込む。

```
ファイル名はツール側の自動読込規約であり、改称の対象ではない。
本ファイルの**内容**は特定の生成AI製品に依存しない。
役割は権限と責務で定義し、製品名・個人名では定義しない。
```

節ごとに読み手を示す。自分の役割の節と §2 を読むこと。

```
§2  すべての役割に適用される
§3  開発者に適用される
§4  デプロイ権限を持つ開発者のみに適用される
§5  管理者に適用される

REVIEWER は §2 と docs/user_manager_collaboration_protocol.md 1節 / 3.9〜3.14節を読む
```

**規則の本文は本ファイルへ複製しない。** 各正本への入口だけを置く。
ルールを変更する場合は正本の文書を更新する。

---

## 1. 役割

```
役割                       識別子
利用者・承認者             USER
管理者                     MANAGER
レビュワー                 REVIEWER
開発者(デプロイ権限あり)   DEVELOPER_WITH_DEPLOY
開発者(デプロイ権限なし)   DEVELOPER
```

各役割の権限・責務・禁止事項は
[docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
1節が正本である。

**担当は変わりうるため、恒久文書へ焼き込まない。**
現在の担当(誰がどの役割か)を確認する必要がある場合は、`ROLE_ASSIGNMENT_SSOT`
(同文書1節が正本)が指す最新のdurableな体制記録をfreshに読むこと。

---

## 2. すべての役割に適用される規則

- **開発の進め方・レビュー・release governanceは
  [docs/development_workflow.md](docs/development_workflow.md) に従うこと。**
  lane / WIP制限 / **指示プロトコル(INSTRUCTION_ID)** / 実装パイプライン /
  ローカルテスト方針 / GitHubへの永続化 / 現況判断 / negative-path検証 /
  AWS pagination / grouped release /
  **Issue起点の原則(挙動・構成・運用・契約へ影響する変更はIssue必須)** /
  人間承認の境界は同文書が正本である。

  **機能領域・機能・共通部品の一覧は
  [docs/functional_domains.md](docs/functional_domains.md) が正本である。**
  同文書を用いた領域ベースのWIP運用ルール(`DOMAIN_WIP_RULE_V1`)は
  development_workflow.md 2.6節が正本であり、**既に発効している**
  (`CURRENT_WIP_RULE = DOMAIN_WIP_RULE_V1`)。
  発効状態は変わりうるため、確認が必要な場合はIssue #177の最新のdurableな
  activation記録をfreshに読むこと(静的な文書を唯一の根拠にしない)。

- **作業報告・Human Gate提示・Instructionの許可範囲(`AUTHORIZED_PHASES`)・
  確認質問といった「メッセージの形式」は
  [docs/ai_operation_message_contract.md](docs/ai_operation_message_contract.md)
  が正本である。** 同文書は形式のみを定め、承認の要否・作業の可否・WIP・labelの
  規則はいずれも他文書が正本である(本ファイルへも同文書へも複製しない)。
  **同文書は既に発効している**(`NEW_CONTRACT_ACTIVE = YES`)。
  発効状態は変わりうるため、確認が必要な場合はIssue #184の最新のdurableな
  activation記録をfreshに読むこと。

- **Issueの現況は、作業の前に読み直し、作業で変えたら書き戻すこと。**
  詳細ルール(着手前の確認・完了の条件・handoffとの関係・`ISSUE_STATE_SNAPSHOT`の
  書き戻し)の正本は
  [docs/development_workflow.md](docs/development_workflow.md) 6.5節であり、
  **本ファイルへ複製しない**。
  記憶・会話要約・古いIssue本文だけを根拠に実装を始めない。stale・矛盾・
  snapshot不在のいずれかなら、実装せずまずread-onlyのstatus reconciliationを行う。
  **現在有効な規則を読むときはorigin/mainを明示refで読む**(手元の作業branchを
  正本にしない)。**実装・テスト・push・報告だけでは完了ではない**
  (stateを変えたら、snapshotの書き戻しまでが完了)。

- **MANAGER と REVIEWER は別の役割である。** 役割の定義と review lifecycle は
  [docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
  1節 / 3.9〜3.14節が正本であり、**本ファイルへ規則本文を複製しない**。
  発効状態は変わりうるため、確認が必要な場合はIssue #353の最新のdurableな
  activation記録をfreshに読むこと(静的な文書を唯一の根拠にしない)。

- **利用者と管理者の間の協働ルール(役割分担・Human Gate・レビュー判定・
  指示の対応付け・セッション開始時のbootstrap)は
  [docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
  が正本である。** 特に「管理者が推奨すること」と「利用者が承認したこと」は
  別であり、`PASS_WITH_CONDITIONS`はHuman Gate通過を意味しない。
  `INSUFFICIENT_EVIDENCE`は不合格ではなく証拠不足であり、推測でPASSにしない。

- **恒久規則の制定・変更権限およびAI memoryの扱いは
  `POLICY_AUTHORITY = HUMAN_ONLY` /
  `MEMORY_POLICY_AUTHORITY = NONE` とし、
  [docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
  §8を正本とする。詳細は同節を参照し、本ファイルへ規則本文を複製しない。**

- **GitHub Issueを作成・調査・更新・closeする場合は、
  [docs/issue_label_policy.md](docs/issue_label_policy.md) を必ず読み、
  そのルールに従うこと。** labelの4軸(Issue Type / Priority / Release Blocker /
  Progress Status)の判定、Severity軸の廃止、Progress Statusは1 Issue = 1 lifecycle
  (`ONE_ISSUE_ONE_PROGRESS_LIFECYCLE`)であること、判定基準・分割手続きは
  同文書が正本であり、**本ファイルへ複製しない**。
  Issue本文・最新コメント・labelsが矛盾する場合は、勝手に推測して実装を進めず、
  どれが最新の確定判断かを確認すること。

- **Priorityは「利用者の投資運用に対して、そのIssueをどの順番で直すべきか」で決める。**
  subsystem名だけで決めてはならない。投資影響だけでなく、非機能影響
  (Security / Privacy / Cost / Reliability 等)も独立に評価し、高い方をIssueの
  Priorityとする。判定基準(各段の判定質問・代表例・`PRODUCTION_REACHABILITY`の分類・
  複数findingを持つIssueの扱い・再評価トリガー)の正本は
  [docs/issue_label_policy.md](docs/issue_label_policy.md) 4節であり、
  **本ファイルへ複製しない**。新しい証拠が判明したらPriorityを再評価し、
  変更した場合は根拠をGitHubへ書き戻すこと。

- **実在人物の個人情報を、Git管理対象にも公開面にも含めない。** 氏名・家族名・
  個人メールアドレス・住所・電話番号等を、ソースコード、テストデータ、fixture、
  コメント、ドキュメント、サンプルへ記録してはならない。
  **Productionのログ出力も同じ範囲である**(実行時に書き出す先も「記録」であり、
  CloudWatch Logsを読めるprincipalへ露出する。Issue #135)。運用・調査には
  識別子(`owner-a`等)で足りる場合が多く、実名を出さずに目的を達成できるかを
  先に検討する。所有者等を
  例示する場合は「所有者A」「owner-a」等の架空値を使用する。個人特定情報を
  テスト・ドキュメントへ転記しない。
  **禁止するのは、個人特定情報と、個人特定情報と結び付いた資産情報であり、保有銘柄の名称・
  証券コード・数量・取得単価・金額は、それだけでは一律禁止ではない**(必要性のない実値は、
  架空値・丸めた値・割合を推奨する。定義と判断基準は
  [docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
  11節が正本)。
  一回限りの移行スクリプト等が個人特定情報(または、それと結び付いた資産情報)を必要とする
  場合は、それらをGit管理対象外のローカルファイル(`.gitignore`で除外)から実行時に読み込む
  設計とし、ソースコードには埋め込まないこと。

  **本リポジトリはPUBLICであり、禁止範囲はGit管理ファイルに限らない。**
  commit message、Issue / PR の本文とタイトル、コメント、label、branch名も
  そのまま公開される。**公開面へ書く前の遵守事項は
  [docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
  11節が正本であり、本ファイルへ複製しない。**

  混入はCIの3つの検出(`pii-scan` / `pii-scan-commit-messages` / `pii-metadata-audit`)が
  事後に検知する。検出経路の詳細は
  [docs/operations_manual.md](docs/operations_manual.md) 21.1節が正本。
  **検出は事後の網であって事前防止の代わりにはならない**(denylist方式であり、
  全てのPIIを検出できる保証はない。上記ルールの遵守が前提)。
  **公開面はいったん露出すると、本文を直しても編集履歴・通知メール・外部cacheが
  残る。** 検出時の是正手順・Human escalationの境界・GitHub Supportへの削除依頼の
  要否は [docs/operations_manual.md](docs/operations_manual.md) 21節。

---

## 3. 開発者に適用される規則

対象: `DEVELOPER_WITH_DEPLOY` / `DEVELOPER`

- 判定ロジック・通知内容・データ管理機能など、システムの仕様に変わる変更を行った場合は、
  必ず [docs/functional_spec.md](docs/functional_spec.md)(非技術者向けの機能仕様書)を
  合わせて更新し、末尾の変更履歴に日付と概要を追記すること。

- **新しい機能・共通部品を追加・変更した場合のカタログの維持は
  [docs/functional_domains.md](docs/functional_domains.md) M節が正本である。**
  領域の追加・分割・統合は人間承認が必要である。

- **作業指示に `INSTRUCTION_ID` が付いている場合、回答の冒頭に同じIDを必ず記載すること。**
  IDが無い回答・別IDの回答・撤回済みIDへの回答は、次工程の根拠として扱われない。
  指示キューの扱いを含む詳細は
  [docs/development_workflow.md](docs/development_workflow.md) 2.5節が正本。

- **メソッド名だけを根拠にread-onlyと判断してはならない。**
  `get` / `list` / `find` / `read` / `check` / `health` 等の名称は副作用の有無を
  保証しない。Productionのread-only観測・health check・validation・verification・
  IAM least-privilege設計を行う場合は、**呼び出し先を含むcall graphを確認**し、
  repositoryのsave/update/delete、DynamoDB/S3 write、queue publish、Lambda invoke、
  LINE送信等の外部状態変更が無いことを確かめること。read-onlyと定義した処理に
  hidden writeを持たせない。writeを伴う場合は、API契約・名称・IAM・テストから
  その事実が判別できなければならない。
  (背景と具体的な確認手順は
  [docs/operations_manual.md](docs/operations_manual.md) 18節。
  2026-09-02に、read名のAPIが内部で書き込みを行い、読み取り専用IAMのLambdaで
  AccessDeniedとなって日次バッチ全体が停止するProduction障害が発生している。)

---

## 4. デプロイ権限を持つ開発者のみに適用される規則

対象: `DEVELOPER_WITH_DEPLOY`

- **Production deploy 実作業の担当と範囲は
  [docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
  1.5節が正本である**(`PRODUCTION_DEPLOYMENT_EXECUTOR = DEVELOPER_WITH_DEPLOY`)。
  具体的な手順は [docs/operations_manual.md](docs/operations_manual.md) が正本。
  `DEVELOPER`(デプロイ権限なし)へのdeploy操作の委譲は、既定では禁止されている
  (`DEPLOY_OPERATION_DELEGATION`。値は同節が正本)。

  **担当が1体へ集約されていることは、人間承認なしに実行してよいという意味ではない。**
  ChangeSet の CREATE と EXECUTE は別のHuman Gateであり、承認は exact ARN に対して
  のみ有効である。

---

## 5. 管理者に適用される規則

対象: `MANAGER`

- **`INSTRUCTION_ID` の連番の採番規則(作業者ごと・日本時間の日付ごと)の正本は
  [docs/user_manager_collaboration_protocol.md](docs/user_manager_collaboration_protocol.md)
  4.1節である。** 採番するのは指示側。

- **利用者向けの説明の水準(前提としてよい知識・言い換えの要否・背景と因果関係の添え方)の正本は、
  同文書1.6節である。** 開発者からの完了報告は機械可読形式でよい
  ([docs/ai_operation_message_contract.md](docs/ai_operation_message_contract.md)
  が別のcontractとして定める)。

- **開発者の実装レビューでは、宣言された領域と lock の妥当性を確認する。**
  確認の観点の正本は、同文書3.8節である。**確認された lock omission は合格にしない。**
