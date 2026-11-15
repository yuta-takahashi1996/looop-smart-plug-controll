# FOSSiBOT F2400 の調査

調査日：2026-11-15。対象は FOSSiBOT F2400（SYDPOWER N052 と同じハードウェア。公称 2400W / 2048Wh）。
これは調査メモである。仕様は docs/17-multi-device.md §17.9 を正とする。

## 結論（案 A：クラウド API）

**案 A は採用しない。** 確認用スクリプトも作らない。

- 2026-02 下旬から、SYDPOWER がアプリのログインを模した非公式なクラウド接続（ha-fossibot 1.x の方式）を遮断している。
  MQTT の接続が多すぎることを理由に、メーカーから利用者へ直接連絡があり、**接続元の IP がブロックされた例**がある（後述の #18）。
  ブロックされると BrightEMS アプリ自体も使えなくなった（「Token does not exist on server」）。
- メーカーは代わりに、アプリ（BrightEMS 1.6.0 以降）で**ローカル MQTT ブローカーを公式に設定できるように**した。
  ha-fossibot も 2.0.0（2026-03-10）でクラウド方式を廃止し、この方式に移った。
- 案 A を試すと、本番の Raspberry Pi（自宅の回線の IP）がブロックされ、スマホのアプリも使えなくなるおそれがある。
  そのため、実際に送信して確かめることもしない。

次は案 B（自前の MQTT ブローカー）を確かめる。下の「案 B について分かったこと」を参照。

## 案 A の方式（参考。ha-fossibot v1.0.15 のソースから）

本システムでは使わないが、遮断の経緯を判断できるように記録する。

| 段階 | 内容 |
|---|---|
| 1 | `https://api.next.bspapp.com/client`（uniCloud のサーバーレス関数）に `serverless.auth.user.anonymousAuthorize` を送り、匿名のトークンを得る（HMAC-MD5 の署名付き） |
| 2 | `user/pub/login` にアカウント・パスワードを送り、アクセストークンを得る。リクエストには Android 端末を装った `clientInfo` を付ける |
| 3 | `common/emqx.getAccessToken` で MQTT のトークンと接続先（`mqtt.sydpower.com` など）を得る |
| 4 | `client/device/kh/getList` で機器一覧（`device_id`＝MAC アドレス、`productInfo.modbus_address` / `modbus_count`）を得る |
| 5 | MQTT over WebSocket（ポート 8083）で接続し、`{MAC}/device/response/...` を購読、`{MAC}/client/request/data` に Modbus のフレームを送って値を読む・書く |

アプリになりすます方式であり、同時ログインの制限や利用規約の面でも不安定だった。

## 経緯（ha-fossibot の Issue）

| Issue | 時期 | 内容 |
|---|---|---|
| [#16](https://github.com/iamslan/ha-fossibot/issues/16)・[#17](https://github.com/iamslan/ha-fossibot/issues/17)・[#19](https://github.com/iamslan/ha-fossibot/issues/19) | 2026-02 下旬〜03 | ログインに失敗する（`Login failed - no token in response`、`Failed to get MQTT token`）、MQTT が切断を繰り返す。作者の回答は「Sydpower が MQTT への接続を遮断している」 |
| [#18](https://github.com/iamslan/ha-fossibot/issues/18) | 2026-03-02 | MQTT の接続が多すぎるとして IP をブロックされた。メーカーはローカル MQTT サーバーへの公式対応と、期限のない API トークンの提供を予告した |
| v2.0.0 | 2026-03-10 | クラウド方式を廃止し、ローカル MQTT ブローカー＋公式の API トークンに移行 |

## 案 B について分かったこと（次の調査の前提）

ha-fossibot v3.0.1（2026-09-04）と docs/LOCAL_MQTT.md から。まだ確かめていない。

- **設定**：BrightEMS アプリ（1.6.0 以降）の「Me → Settings → Local MQTT Broker Settings」でブローカーの IP アドレスを入力する。
  同じ画面で **API トークン**（期限なし）を確認できる。設定できるのは**マスターアカウント**（最初に機器を登録したアカウント）だけ。
  設定後は、機器の MQTT の通信がクラウドではなくローカルのブローカーに向かう。
- **公式 API**（API トークンを使う。インターネット接続が必要）：
  - 機器一覧：`POST https://api.app.sydpower.com/http/router/client/device/saas.pub_getDeviceList`（`{"api_token": ...}`）
  - オンライン状態の同期：`saas.pub_updateMqttState`
  - アカウント・パスワードによるログインは不要。**同時ログインの制限（§17.9.3 #2）の問題はなくなる**見込み。
- **ブローカー**：TCP 1883。機器の**ファームウェアは認証なし（匿名）の接続しかできない**（[#28](https://github.com/iamslan/ha-fossibot/issues/28)・[#30](https://github.com/iamslan/ha-fossibot/issues/30)）。
  Home Assistant のアドオン版 Mosquitto は匿名を許可できず、つながらない。EMQX、または匿名を許可した通常の Mosquitto を使う。
  宅内 LAN 限定のブローカーとし、外部に公開しない。
- **トピック**：`{MAC}/device/response/state`（オンライン状態）、`{MAC}/device/response/client/04`（入力レジスタ＝計測値）、
  `{MAC}/device/response/client/data`（保持レジスタ＝設定値）を購読し、`{MAC}/client/request/data` に Modbus のフレームを送る。
- **値**（SYDPOWER から開発者に提供された公式のプロトコル仕様に基づく。docs/PROTOCOL_AUDIT.md）：
  - 残量：入力レジスタ 56（0.1% 単位）。拡張バッテリーは入力レジスタ 53 / 55（0 は未接続）。
  - AC 静音充電：保持レジスタ 57（下位バイトが 1 なら ON）。書き込み後に読み直して照合できる。
- **F2400 固有の注意**：
  - AC・DC 出力（保持レジスタ 25 / 26）は、0 / 1 のどちらを書いても**トグル動作**になるファームウェアの不具合がある（[#40](https://github.com/iamslan/ha-fossibot/issues/40)）。本システムでは出力の ON／OFF は使わないが、書き込みは AC 静音充電に限る。
  - 物理ノブのある機種は、AC 充電速度のレジスタへの書き込みを無視する（[#38](https://github.com/iamslan/ha-fossibot/issues/38)）。AC 充電速度は扱わない仕様（§17.9.4）なので影響はない。AC 静音充電が同様に無視されないかは実機で確かめる。
  - 拡張バッテリー（F2400-B）の容量の値が 10 倍に見える報告がある（[#41](https://github.com/iamslan/ha-fossibot/issues/41)）。残量（%）の判定には影響しないが、容量の値は種別マスターの公称値を使う。
- **未確認**：ローカルに切り替えた後も BrightEMS アプリで遠隔の監視・操作ができるか。

## 案 B の確認結果（2026-11-15）

BrightEMS 1.7.7（マスターアカウント）で確認した。

### 準備

- Raspberry Pi 5 に Mosquitto 2.0.20 を試験用に立てた（`~/fossibot-mqtt/`、アプリとは別の Compose。`listener 1883 0.0.0.0`・`allow_anonymous true`）。
  Pi の有線 LAN の IP はルーターで DHCP 予約した。
- アプリの設定画面は「ローカルMQTT Broker設定」。入力は「MQTT ブローカーホスト名」と「ユーザー名」。
  同じ画面に「デバイス接続ポート：1883」「API TOKEN」の表示と、MQTT で扱う機器の選択（登録済みの機器が初期値）がある。
  ホスト名に Pi の LAN の IP を入れ、Enable にした。

### 結果

| 確認すること | 結果 |
|---|---|
| ブローカーへの接続 | Enable にしてから約1分後に、F2400 が `device_{MAC}` というクライアント ID で接続した（MQTT 3.1.1、keepalive 60秒）。本体の再起動は不要だった |
| 計測値（入力レジスタ） | 要求を送らなくても、F2400 が約1分ごとに `{MAC}/device/response/client/04` へ送ってくる（168 バイト：ヘッダー6バイト＋81ワード。先頭は `11 04 00 00 00 50`） |
| 残量 | 入力レジスタ 56 が 942（＝94.2%）。拡張バッテリーは未接続で 53 / 55 は 0 |
| 設定値（保持レジスタ） | `{MAC}/client/request/data` に読み出し要求（`11 03 00 00 00 50` ＋ CRC）を送ると、数秒で `{MAC}/device/response/client/data` に返ってきた |
| AC 静音充電 | 保持レジスタ 57 が 0（OFF＝高速充電）。放電の下限（66）は 0、充電の上限（67）は 1000（100%） |
| AC 静音充電の変更 | `scripts/fossibot_check.py silent on --confirm` で 57 に 1 を書き込むと、すぐに書き込みの応答（`11 06 00 39 00 01` ＋ CRC。送ったフレームと同じ内容）が返り、読み直した値も ON になった。照合できる |

- 機器の一覧（MAC アドレス）はトピック名から分かるため、**値の取得だけならインターネットも API トークンも要らない**。
- 残量・AC 静音充電の値は、アプリの表示と一致した。
- **Enable にした後のアプリは Bluetooth で機器から直接取得している**。Bluetooth を切ると、アプリは F2400 の情報を取得できない。
  つまり、外出先からアプリで監視・操作することはできなくなる（本システムの画面で残量を確認する）。
- 本体の液晶には Wi-Fi 接続のアイコンが出るが、アプリでは「未接続」と表示される。
  クラウド側は、機器がクラウドの MQTT につながっているかでオンラインを判定しているためと考えられる。
  ha-fossibot は公式 API の `saas.pub_updateMqttState`（API トークンを使う）でオンライン状態をクラウドに伝えている。
  本システムの動作には影響しない表示上の問題なので、送信するかは採用時に決める（未検証）。
- 読み出し要求を前の要求の直後に送ると、応答しないことがあった。1秒空けて再送すると、安定して取得できた。
- AC 充電の段階は、本体のダイヤル（300〜1100W の5段階）の位置がレジスタに出る。確認時はダイヤルが最大で、
  入力レジスタ 2（実際の段階）＝5、保持レジスタ 13（段階の設定）＝5、保持レジスタ 14（AC 充電の最大電力）＝1100。
  AC 静音充電（保持 57）を書き換えても 13・14 は変わらなかった。ダイヤルのある機種は 13 への書き込みを無視する（[#38](https://github.com/iamslan/ha-fossibot/issues/38)）。
- AC 静音充電で充電電力がどれだけ下がるか、ダイヤルの段階とどう組み合わさるかは、公開情報では分からない
  （ha-fossibot の説明は「Quieter, slower mains charging」のみ）。AC で充電中に、OFF / ON で入力レジスタ 3（AC 入力 W）を比べて確かめる。
- 未確認：出力（W）が 0 以外のときの値、AC 静音充電の実際の充電電力、長時間の安定性。

### 本システムへの影響

- 本番は Raspberry Pi 5 に移ったため、§17.9.2 の「WSL2 の Docker は localhost にだけ公開している」という短所はなくなった。
  Pi の docker-compose にブローカーを追加し、1883 を LAN に公開すれば、F2400 から届く。
- 匿名の MQTT を LAN に公開することになるため、接続元の制限（ファイアウォールで宅内 LAN と F2400 のみ許可するなど）を検討する。
