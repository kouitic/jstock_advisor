## 概要

引用内のCloses記法は検出対象外(誤検出しないことを確認する)。

Refs #1

> Closes #1 (過去のレビューコメントの引用例)

## TIME_SEMANTICS_IMPACT

TIME_SEMANTICS_IMPACT = NO

## DoD

1 境界の連続性     = 該当なし
2 単調性           = 該当なし
3 定常でない1回目  = 該当なし
4 単位・スケール   = 該当なし
5 失敗の可視性     = 該当なし

## 同型 sweep

SWEEP_RESULT = 0件

## 確認

- [x] `ruff check src tests`
