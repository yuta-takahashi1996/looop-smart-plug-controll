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

デバイス一覧（`GET /devices`）の `deviceList` の各機器は `deviceId`・`deviceName`・`deviceType`・`enableCloudService`・`hubDeviceId` を持つ。制御デバイス設定画面（docs/17 §17.5）で一覧を表示し、`deviceType` を種別マスター（`device_types`）のコードとして使う。ON／OFF の対象にできるのはプラグ・リレースイッチ（`Plug Mini (JP)` など、docs/17 §17.4.2）でクラウドサービスが有効な機器に限る。呼び出し上限は1日10,000回のため、一覧の取得は画面を開いたとき（前回から24時間以上）と再取得の操作時だけにする。

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
```

認証情報は環境変数で管理し、Gitへ登録しない。制御する機器は画面で選んで DB に登録する（`SWITCHBOT_DEVICE_ID` は既存の1台を移行するときだけ使う。docs/17 §17.4.7）。

## 8.3 操作後の状態確認

1. 操作前の状態取得
2. 必要な場合のみON/OFFコマンド送信
3. 操作後の状態再取得
4. 期待状態との照合
5. 結果をDBへ記録
6. 失敗時は再試行・安全処理

`toggle`コマンドは使用せず、明示的なON/OFFのみ使用する。

- 送信後は3秒ごとに最大5回状態を取得し、期待状態になれば成功（SUCCESS）とする。ならなければ1回だけ送り直す。
- それでも期待状態にならない場合は失敗（FAILED。状態を取得できない場合は未確認 UNCONFIRMED）として記録し、成功扱いにしない。異常として表示する。
- 失敗した要求は5分間繰り返さない（API の呼び出し回数の上限のため）。照合できなかった場合はプラグの状態を「不明」とし、次のサイクルで ON への初期化から行う。
- 以上は機器ごとに行う。複数の機器は並行して処理し（同時に最大5台）、1台の失敗で他の機器を止めない。
- 操作前の状態がすでに要求状態であれば、コマンドは送信せず成功として記録する。

## 8.4 ポータブル電源（Jackery）の残量取得・充電モードの変更

Jackery は公式 API を公開していないため、Jackery アプリが利用しているクラウド API（`https://iot.jackeryapp.com`）から状態を取得する（`app/clients/jackery.py`）。操作は充電モード（高速充電／静かな充電）の変更のみとし、超急速充電・出力の ON/OFF などは扱わない（docs/07-control-logic.md §7.10）。

| 処理 | エンドポイント |
|---|---|
| ログイン | POST /v1/auth/login（ログイン情報を AES で暗号化して送信） |
| デバイス一覧 | GET /v1/device/bind/list |
| 状態取得 | GET /v1/device/property?deviceId={deviceId} |

状態の主な項目（`properties`）：

| 項目 | 内容 |
|---|---|
| rb | 残量（%）。制御に使う |
| op | 出力電力（W）。家電の消費電力として、残量の減り方の予測に使う |
| ip | 入力電力（W） |
| ot / it | 残り給電時間・満充電までの時間（1/10 時間。アプリの表示と同じ値。参考値） |
| bt | 電池温度（1/10 ℃） |
| oac | AC 出力の ON/OFF |
| cs | 充電モード（0=高速充電、1=静かな充電。それ以外は不明として扱う） |
| sfc | 超急速充電の有効・無効（表示・変更しない） |

- 同じアカウントで同時にログインできるのは1台のみのため、**取得専用のアカウント**を作り、本アカウントからデバイスを共有して使う（取得専用アカウントはスマホでは使わない）。
- 認証情報は環境変数（`JACKERY_ACCOUNT` / `JACKERY_PASSWORD`）で管理し、Git や DB に保存しない。複数台は同じ取得専用アカウントに共有し、設定画面で機器一覧から選んで登録する（`JACKERY_DEVICE_ID` は既存の1台を移行するときだけ使う。docs/17 §17.8）。
- 非公式 API のため、予告なく使えなくなる可能性がある。その場合は残量不明として扱い、OFF 時間の制限で保護する（docs/07-control-logic.md §7.7）。
- 動作確認は `uv run python scripts/jackery_check.py [devices|status|raw]` で行う（読み取りのみ）。

### 充電モードの変更

充電モードは HTTP では変更できず、Jackery アプリと同じく MQTT でコマンドを送る（調査の経緯は [jackery-charge-mode-research.md](jackery-charge-mode-research.md)）。

| 項目 | 値 |
|---|---|
| ブローカー | `emqx.jackeryapp.com:8883`（MQTT 5、TLS。CA は Jackery アプリ同梱の自己署名証明書 `app/clients/jackery-ca.crt`） |
| クライアントID | `{userId}@APP` |
| ユーザー名 | `{userId}@{ログイン時の端末ID}` |
| パスワード | ログイン応答の `mqttPassWord`（AES-256 鍵）でユーザー名を AES/CBC 暗号化した値（IV は鍵の先頭16バイト） |
| 送信先 | `hb/app/{userId}/command`（QoS 1） |
| 通知 | `hb/app/{userId}/device` |
| コマンド | `messageType=DevicePropertyChange`、`actionId=10`、`body={"cs": "0"}`（高速充電。文字列）／`{"cs": 1}`（静かな充電）、`deviceSn` は状態取得の `device.deviceSn` |

- MQTT 3.1.1 では接続・購読はできるが、送信したコマンドがエラーなく破棄され機器に届かない（2026-11 に確認）。必ず MQTT 5 で接続する。
- 接続した直後（特にログインし直した直後）に送ると、受領応答は成功でもコマンドが機器に届かないことがある。接続・購読してから15秒待って送信し、機器からの通知がなければ同じ接続で1回だけ送り直す。
- 高速充電は文字列の `"0"`、静かな充電は数値の `1` で送る（どちらも実機で反映を確認した形。機器からの通知・HTTP の値は数値）。
- 変更前に HTTP で状態を取得し、すでに目的のモードなら送信しない。
- 送信後は機器からの通知を最大10秒待ち（送り直した場合はさらに10秒）、通知の有無によらず HTTP で再取得した `cs` が期待値になった場合のみ成功とする（3秒間隔で最大5回）。照合できなければ成功扱いにしない（UNCONFIRMED）。
- MQTT の認証に失敗した場合は、`mqttPassWord` を更新するため1回だけログインし直して再送する。
- 共有の権限が読み取り専用（`level=3`）の場合は変更できない。取得専用アカウントへの共有は操作可能な権限で行う。
- 実機での確認は `uv run python scripts/jackery_check.py charge-mode fast|silent --confirm` で行う（DRY_RUN によらず送信する）。

## 8.5 ポータブル電源（FOSSiBOT F2400）

FOSSiBOT（BrightEMS アプリ、メーカーは SYDPOWER）は公式 API を公開していない。非公式の方式（クラウド API、または自前の MQTT ブローカー）で残量の取得と AC 静音充電の切り替えができるかを調査し、採用の条件を満たした場合のみ実装する（docs/17-multi-device.md §17.9）。充電モードは、静かな充電＝AC 静音充電 ON、高速充電＝AC 静音充電 OFF として扱う。エンドポイント・認証・環境変数は調査の結果で本節に追記する。
