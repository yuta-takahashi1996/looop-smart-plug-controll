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

## 10.1 forecast_prices

| カラム | 説明 |
|---|---|
| id | 主キー |
| area | 電力エリア |
| slot_start | 対象コマ開始日時 |
| slot_end | 対象コマ終了日時 |
| energy_price | 電源料金単価 |
| fetched_at | 取得日時 |
| source | データ取得元 |

## 10.2 control_schedules

| カラム | 説明 |
|---|---|
| id | 主キー |
| schedule_version | スケジュール版数 |
| start_at | 制御開始日時 |
| end_at | 制御終了日時 |
| target_state | ON/OFF |
| reason | 制御理由 |
| estimated_saving | 推定節約額 |
| status | 実行状態 |

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
