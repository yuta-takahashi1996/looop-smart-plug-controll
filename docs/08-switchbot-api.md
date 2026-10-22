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
