# SwitchBot API連携

## 8.1 利用API

SwitchBot OpenAPI v1.1を使用する。

```text
https://api.switch-bot.com/v1.1
```

| 処理 | エンドポイント |
|---|---|
| デバイス一覧取得 | GET /devices |
| 状態取得 | GET /devices/{deviceId}/status |
| 電源制御 | POST /devices/{deviceId}/commands |

ONコマンド例：

```json
{
  "command": "turnOn",
  "parameter": "default",
  "commandType": "command"
}
```

OFFの場合は`turnOff`を使用する。

## 8.2 認証

```env
SWITCHBOT_TOKEN=
SWITCHBOT_SECRET=
SWITCHBOT_DEVICE_ID=
```

認証情報は環境変数で管理し、Gitへ登録しない。

## 8.3 操作後の状態確認

1. 操作前の状態取得
2. 必要な場合のみON/OFFコマンド送信
3. 操作後の状態再取得
4. 期待状態との照合
5. 結果をDBへ記録
6. 失敗時は再試行・安全処理

`toggle`コマンドは使用せず、明示的なON/OFFのみ使用する。

## 8.4 ポータブル電源（Jackery）の残量取得

Jackery は公式 API を公開していないため、Jackery アプリが利用しているクラウド API（`https://iot.jackeryapp.com`）から状態を取得する（`app/clients/jackery.py`）。取得のみを行い、ポータブル電源の操作はしない。

| 処理 | エンドポイント |
|---|---|
| ログイン | POST /v1/auth/login（ログイン情報を AES で暗号化して送信） |
| デバイス一覧 | GET /v1/device/bind/list |
| 状態取得 | GET /v1/device/property?deviceId={deviceId} |

状態の主な項目（`properties`）：

| 項目 | 内容 |
|---|---|
| rb | 残量（%）。制御に使う |
| ip / op | 入力・出力電力（W） |
| ot / it | 残り給電時間・満充電までの時間（1/10 時間。アプリの表示と同じ値。参考値） |
| bt | 電池温度（1/10 ℃） |
| oac | AC 出力の ON/OFF |

- 同じアカウントで同時にログインできるのは1台のみのため、**取得専用のアカウント**を作り、本アカウントからデバイスを共有して使う（取得専用アカウントはスマホでは使わない）。
- 認証情報は環境変数（`JACKERY_ACCOUNT` / `JACKERY_PASSWORD` / `JACKERY_DEVICE_ID`）で管理し、Git や DB に保存しない。
- 非公式 API のため、予告なく使えなくなる可能性がある。その場合は残量不明として扱い、OFF 時間の制限で保護する（docs/07-control-logic.md §7.7）。
- 動作確認は `uv run python scripts/jackery_check.py [devices|status|raw]` で行う（読み取りのみ）。
