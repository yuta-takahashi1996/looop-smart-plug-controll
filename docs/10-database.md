# データベース設計

初期実装ではSQLiteを採用する。

| テーブル | 用途 |
|---|---|
| forecast_prices | 電力料金予報 |
| control_schedules | 自動制御予定 |
| control_logs | 操作・実行履歴 |
| system_settings | システム設定 |
| system_events | エラー・イベント履歴 |
| daily_summaries | 日次集計・推定節約額 |
| price_adjustments | 請求単価への加算額 |

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
| source_level | 生成元の区分（ADVISORY：注意報 / WARNING：警報） |
| enabled | 有効・無効（有効なスケジュールのみ実行する） |
| enabled_by | 有効・無効を決めた主体（AUTO：生成時の自動判定 / MANUAL：ユーザー） |
| manually_modified | 手動変更済み（有効・無効の切り替えまたは時間変更。再生成で上書きしない） |
| reason | 制御理由 |
| estimated_saving | 推定節約額（電源単価で算出） |
| status | 実行状態（PENDING。実行は Phase 5 以降） |
| updated_at | 更新日時 |

## 10.3 control_logs

| カラム | 説明 |
|---|---|
| id | 主キー |
| executed_at | 操作日時 |
| source | AUTO/MANUAL/SAFETY |
| previous_state | 操作前状態 |
| requested_state | 要求状態 |
| actual_state | 確認結果 |
| result | 成功・失敗・未確認 |
| error_message | エラー内容 |

## 10.4 system_settings

| カラム | 説明 |
|---|---|
| key | 設定キー |
| value | 設定値 |
| updated_at | 更新日時 |

秘匿情報はsystem_settingsへ保存せず、環境変数で管理する。

`price_notices_baseline_at`（お知らせの初回確認日時、docs/06 §6.6）も system_settings に保存する。

## 10.5 system_events

| カラム | 説明 |
|---|---|
| id | 主キー |
| occurred_at | 発生日時 |
| level | INFO / WARNING / ERROR |
| category | FORECAST（予報取得・検証）/ SWITCHBOT（通信）/ PRICE_NOTICE（単価に関わるお知らせ） |
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
