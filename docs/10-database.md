# データベース設計

初期実装ではSQLiteを採用する。

| テーブル | 用途 |
|---|---|
| forecast_prices | 電力料金予報 |
| control_schedules | 自動制御予定 |
| control_logs | 操作・実行履歴 |
| system_settings | システム設定 |
| system_events | エラー・イベント履歴 |
| daily_summaries | 日次集計・推定節約額（前日分を毎日集計する。当日分は画面で都度集計する） |
| price_adjustments | 請求単価への加算額 |
| schedule_rules | 任意スケジュールのルール |

日時はすべて UTC で保存し、表示時に `TZ` のタイムゾーンへ変換する。

## 10.1 forecast_prices

| カラム | 説明 |
|---|---|
| id | 主キー |
| area | 電力エリア |
| slot_start | 対象コマ開始日時 |
| slot_end | 対象コマ終了日時 |
| energy_price | 電源料金単価（取得値。請求単価は price_adjustments から都度算出） |
| level | でんき予報の区分（GOOD：でんき日和 / NORMAL / ADVISORY：注意報 / WARNING：警報） |
| fetched_at | 取得日時 |
| source | データ取得元 |

(area, slot_start) で一意とし、再取得時は上書きする。

## 10.2 control_schedules

| カラム | 説明 |
|---|---|
| id | 主キー |
| schedule_date | 対象日（表示用のタイムゾーンの日付） |
| schedule_version | スケジュール版数（対象日ごとの生成回数） |
| start_at | 制御開始日時 |
| end_at | 制御終了日時 |
| target_state | ON/OFF |
| source_level | 生成元の区分（ADVISORY：注意報 / WARNING：警報 / CUSTOM：任意スケジュール） |
| rule_id | 任意スケジュールから展開した場合、そのルール（schedule_rules.id。ルール削除時は NULL） |
| enabled | 有効・無効（有効なスケジュールのみ実行する） |
| enabled_by | 有効・無効を決めた主体（AUTO：生成時の自動判定 / MANUAL：ユーザー） |
| manually_modified | 手動変更済み（有効・無効の切り替えまたは時間変更。再生成で上書きしない） |
| reason | 制御理由 |
| estimated_saving | 推定節約額（電源単価で算出） |
| status | 実行状態（PENDING / RUNNING / DONE / ABORTED / SKIPPED、docs/07 §7.8） |
| executed_start_at / executed_end_at | 実際に OFF にした（Dry Run では OFF にするはずだった）時間帯 |
| executed_saving | 実行した時間帯の推定節約額（推定節約額の検証用） |
| status_reason | 中断・未実行の理由 |
| battery_start_percent / battery_end_percent | 実行開始時・終了時のポータブル電源の残量（取得できた場合） |
| measured_power_w | 実行中に取得した消費電力（出力W）の平均（取得できた場合） |
| updated_at | 更新日時 |

## 10.3 control_logs

| カラム | 説明 |
|---|---|
| id | 主キー |
| executed_at | 操作日時 |
| source | AUTO/MANUAL/SAFETY |
| previous_state | 操作前状態 |
| requested_state | 要求状態（ON / OFF） |
| actual_state | 確認結果（操作後に取得したプラグの状態。Dry Run では NULL） |
| result | SUCCESS（成功）/ FAILED（失敗）/ UNCONFIRMED（未確認）/ DRY_RUN（Dry Run のため送信していない） |
| reason | 制御理由 |
| error_message | エラー内容 |
| dry_run | Dry Run での記録か |
| schedule_id | スケジュールによる操作の場合、そのスケジュール |

## 10.4 system_settings

| カラム | 説明 |
|---|---|
| key | 設定キー |
| value | 設定値 |
| updated_at | 更新日時 |

秘匿情報はsystem_settingsへ保存せず、環境変数で管理する。

`price_notices_baseline_at`（お知らせの初回確認日時、docs/06 §6.6）、`control_mode`（AUTO / MANUAL）、`real_control_enabled`（実制御の確認操作の有無、docs/14 §14.3）も system_settings に保存する。

## 10.5 system_events

| カラム | 説明 |
|---|---|
| id | 主キー |
| occurred_at | 発生日時 |
| level | INFO / WARNING / ERROR |
| category | FORECAST（予報取得・検証）/ SWITCHBOT（通信・コマンドの失敗）/ BATTERY（ポータブル電源の残量取得）/ PRICE_NOTICE（単価に関わるお知らせ） |
| message | 内容 |
| reference | 重複検知用のキー（お知らせのURLなど） |
| resolved_at | 解決日時（未解決は NULL） |

未解決のイベントはダッシュボードに表示する。エラーは原因が解消したとき（取得・通信の成功時）に自動で解決し、お知らせは画面から確認済みにする。

## 10.6 price_adjustments

| カラム | 説明 |
|---|---|
| id | 主キー |
| category | SERVICE / TRANSMISSION / RENEWABLE_LEVY / SUBSIDY / OTHER |
| amount | 金額（円/kWh、税込。値引きは負の値） |
| effective_from | 適用開始日（使用日） |
| effective_to | 適用終了日（終了日を含む。NULL は期限なし） |
| note | メモ |
| updated_at | 更新日時 |

## 10.7 daily_summaries

| カラム | 説明 |
|---|---|
| summary_date | 対象日（主キー） |
| off_minutes | 実行した OFF 時間の合計（分） |
| switch_count | 状態が変わった操作の回数 |
| schedules_done / schedules_aborted / schedules_skipped | 完了・中断・未実行のスケジュールの件数 |
| estimated_saving | 実行分の推定節約額の合計（円） |
| battery_used_percent | 実測できた OFF の残量減少の合計（%） |
| measured_power_w | 実測できた OFF の平均消費電力（W、実行時間で加重平均） |
| failed_operations | 失敗・未確認の操作の件数 |
| dry_run | Dry Run の記録を含むか |
| updated_at | 更新日時 |

毎日 0:05 に前日分を集計する。起動時は直近7日の未集計の日も集計する（記録のない日は作らない）。前日分は毎回集計し直す。

## 10.8 保持期間とバックアップ

| データ | 保持期間 |
|---|---|
| control_logs・control_schedules・forecast_prices | `DATA_RETENTION_DAYS`（初期値 400日。前年との比較のため1年強） |
| system_events（解決済み） | 90日（単価に関わるお知らせは重複検知に使うため削除しない） |
| daily_summaries・system_settings・price_adjustments・schedule_rules | 削除しない（schedule_rules は画面から削除する） |

毎日 3:30 に、SQLite のオンラインバックアップを `data/backups/app-YYYYMMDD.db` に作成し（`BACKUP_GENERATIONS` 世代、初期値14）、保持期間を過ぎたデータを削除して WAL の反映と `PRAGMA optimize` を行う（`app/services/maintenance.py`）。

## 10.9 schedule_rules

| カラム | 説明 |
|---|---|
| id | 主キー |
| name | 名前 |
| target_state | ON / OFF |
| repeat_type | ONCE（1回のみ）/ DAILY（毎日）/ WEEKLY（曜日指定） |
| target_date | 対象日（ONCE のみ。表示用のタイムゾーンの日付） |
| weekdays | 対象の曜日（WEEKLY のみ。月〜日の組） |
| start_time / end_time | 開始・終了時刻（表示用のタイムゾーンの時刻。30分単位） |
| enabled | 有効・無効（無効なルールは展開しない） |
| created_at / updated_at | 作成・更新日時 |

当日・翌日分を control_schedules に展開する（docs/07 §7.9）。
