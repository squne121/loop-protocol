# cloud_pilot_success_result/v1 フィクスチャ（負例）

この fixture は payload の code fence が JSON でなく YAML である不正を検証するための負例であり、checker が拒否することを確認するためのものである。以下のマーカー行はチェッカーが参照する契約でありバイト単位で変更しない。
<!-- CLOUD_PILOT_SUCCESS_RESULT_V1 repo=squne121/loop-protocol target=issue:1153 parent_issue=1153 result_id=cloud-pilot-success-result-yaml-rejected -->

```yaml
schema: cloud_pilot_success_result/v1
result_id: cloud-pilot-success-result-yaml-rejected
anchor: &a value
alias: *a
```

この digest 値は fixture の内容から算出された既存の値であり、意図的な不正フィールドを含んだまま保持している。
<!-- CLOUD_PILOT_SUCCESS_RESULT_DIGEST_V1 sha256=ad4f29bf889c966d22697deafd031c8f61de581962e77170cc74813c07c37b7c -->
