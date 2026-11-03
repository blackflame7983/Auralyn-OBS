# 88.2／176.4／192kHz入力の追加

2026-11-03。ローカル試作。既存96kHz候補 `a0664f4` を基点とする。
本体とモニターの処理rateを維持し、別プロセスのOBS受信だけを48kHzへ変換する。
384kHz以上、DSD、全デバイス・VST・聴感・公開の受入は含まない。

## 通信と互換性

mapping名v1、196672 bytes、16384 frame、frame tag/atomic/muteの構造は維持する。
正本は本体の `docs/protocols/OBS_PCM_V1.md` と共通fixture。別repoのsnapshotとSHAを同期する。

| 入力rate | 新送信のminor | 新受信の48kHz向け補間 |
| --- | --- | --- |
| 44.1kHz | 0 | 従来の線形補間、品質採用は別判定 |
| 48kHz | 0 | 従来の128tap Sinc |
| 96kHz | 0 | 従来の256tap Sinc |
| 88.2kHz | 1 | 256tap / cutoff 44640/88200 / scratch 4608 frame |
| 176.4kHz | 1 | 512tap / cutoff 44640/176400 / scratch 9216 frame |
| 192kHz | 1 | 512tap / cutoff 44640/192000 / scratch 9216 frame |

旧受信は追加3rateの通信1.1を拒否する。新受信は1.0の旧3rateと1.1の全6rateを受け付け、不正なminor/rate・未知minorを拒否する。
48/96kHzの係数と既存modeは維持。追加bankも制御側で確保し、描画中の確保・解放・待機を増やさない。
192kHzの50ms目標は9600 frame。最大2048出力frame・±2000ppm・履歴/先読みを含めてringと事前作業領域内に収める。
入力rate・出力rate・epochを変えた場合は再prime、描画途中のminor/rate変更ではその描画を無音にする。
既存の `AURALYN_SINC48_POC` は既定OFFのまま。

## 同一入力の周波数測定

70秒、速度差0/±37/±500ppm。新3rateの可聴帯域と帯域外、既存48/96kHzの可聴帯域回帰の40条件全てで転送modelと測定基準を満たした。
100Hz/1kHz/10kHz/19kHz/20kHzの測定点（48kHzのみ20kHzを除く）を±80Hz帯域で比較した。
帯域外は24.5/26/30/40kHzに加え、追加rateのNyquist直前、高いrateでは60/80/90kHzの該当点も測る。

| 入力 | 可聴帯域power差の最大絶対値 | 測定した折り返しpower差の最大値 |
| --- | --- | --- |
| 48kHz回帰 | 0.00008373dB | 今回対象外 |
| 96kHz回帰 | 0.00003236dB | 過去の別測定を参照 |
| 88.2kHz | 0.00004005dB | −118.062dB |
| 176.4kHz | 0.00002306dB | −118.057dB |
| 192kHz | 0.00001902dB | −113.853dB |

基準は可聴帯域±0.05dB、折り返し−80dB以下。全帯域の歪率・SNRや聴感の合格にはしない。
raw前後・probe・SHA・統計・時系列・PSD・可聴帯域用の音量を揃えたWAVは `build/quality-rates-20261103-121733/` に保持する。
これは実Readerのオフライン測定であり、共有メモリ・実時間・機器の受入とは別である。

## 自動確認と後続の結合試験

Sinc ON/OFFのconfigure/build、CTest各2件が成功。minor/rate組合せ、最大block、±2000ppm、履歴tag、有限極値、rate/epoch切替、描画中の割当0を含む。
本体の共有fixture/追加rate/minor選択/送信中の割当解放0はRust 3件成功。
Python 44件成功。Rustfmt、送信Rust 3件、Clippy全target（warnings拒否）、cargo check全target、engineと送信exampleのビルドも成功した。
Rustは本体の制限環境wrapperを使い、既存targetを上書きしないCドライブの専用targetへ出力した。
最終protocol検証も本体 `e17f2b1` の正本・fixtureと一致し、ON/OFF各構成でsnapshotのみを現行版として再確認した。
実OBS試験は新しいRust senderの440Hz逆相stereoを使い、実機のマイク/VSTや高rateドライバーの受入とは区別する。
通常OBS、Voicemeeterの処理rate、既存の未公開96kHz候補は変更しない。
以前のDLL固有の1時間結果は [96kHzの旧記録](SINC96_VALIDATION_20261103.md) に保持し、新DLLへ転用しない。

## 実共有メモリと実OBS録画

新DLLのSHA-256は `c5f3ebeee4d0182071c2207e15d1dd321c17ab6dd6802c351ff5d626d6709aff`。
固定OBS 32.2.2の隔離アプリ、320×180/10fps、PCM16 stereo 48kHz。通常OBSのprofileへ書かず、ソースのモニターは無効にする。
新しい本体の送信exampleから440Hz逆相stereoを送る。実マイク、実VST、高rate ASIOの試験ではない。
`build/native-20261103-123920/synthetic-rate-*/` に、rateごとのraw参照、録画、15ファイルの凍結manifestと結果を保持する。
各rateで実時間60秒以上運転し、14項目全て成功した。録画の開始/終了と制御要求の時差があるため音声frame長は実時間と分ける。

| 入力 | stream minor | 録音音声の長さ | 結果 |
| --- | --- | --- | --- |
| 88.2kHz | 1 | 59.968秒 | PCM16/48kHz、14/14成功 |
| 176.4kHz | 1 | 59.968秒 | PCM16/48kHz、14/14成功 |
| 192kHz | 1 | 59.8827秒 | PCM16/48kHz、14/14成功 |

非無音440Hz・逆相保持・finite、実packetのtime_base=1/48000と時刻欠損0、参照の取りこぼし0、補間modeの維持を確認した。
録音中の受信underrun/overrun/invalid/stale/reset/lock失敗/clock再初期化/接続変更の増分は0。OBS追加バッファ0ms。
各15ファイルのhashと通常OBS設定は不変。コーデックを明示検査し、AAC/FLACをf32へdecodeした結果をPCM録画と誤認しない。
独立レビューで指摘された後始末の例外隔離と元コーデックの確認は修正し、失敗経路の負例も通過した。

## 既存経路の回帰と異常停止

Voicemeeter Insert Virtual ASIO 96kHz → 新しい本体engine/OTT → 新DLL → OBS48kHzを、合成帯域制限noiseで60.078秒運転した。
`build/native-20261103-123920/runs/clock-off-20261103-124658/` は全14項目成功、音声60.0107秒、波形相関の最小0.9999864。
入力/受信のエラー増分・参照取りこぼし・packet時刻欠損・OBS追加バッファ0、試験信号の再生不足0。
Voicemeeterの実rate96kHz、元のInsert 6/7 ON、AUX mute OFFへの復元、通常OBS設定の不変も確認した。
追加依存の `protocol_contract.py` と本体 `obs_bridge.rs` は別の補足manifestでも前後一致を確認した。

実libobsの最終mixでは、192kHz/minor1の送信を強制終了し、48kHz/minor0へ再接続する統計負荷試験も成功。
`build/smoke-rates-crash-192-48/smoke-crash-stress.json` は接続2、非無音416 block、逆相不正0、受信不足/上書き/不正0、OBS追加バッファ0。
負荷による読取lock取得失敗27回を含むが、出力時刻は進み、異常停止後の無音tailを確認した。これは通常の60秒録画中のエラー増分とは別の試験である。

候補生成は引き続き、そのDLL・補間方式・sourceに対応する1時間の実機証拠を必須とする。短い結合試験を1時間の証拠として受理しない。
新3rateの実デバイス・ASIO・マイク/VST、利用者の試聴、通常OBS導入と公開は未確認。

## 同じ新DLLの96kHz実ASIO・1時間回帰

追加rateの短時間試験で使用した同一のDLL `c5f3ebee…6709aff` を、別worktree `obs223-readiness-20261103/Auralyn-OBS` に固定して再使用した。
既存の `build/cmake` には元のビルド記録を取り込み、新しいビルドを行ったとは扱わない。取込元とhashは `build/frozen-import.json` に保持する。
上記の周波数・短時間試験のrawは従来の `Auralyn-OBS-rates` worktreeに保持し、新しい1時間の証拠と混同しない。

Voicemeeter Insert Virtual ASIOを96kHzで維持し、合成帯域制限noise→WASAPI AUX→実ASIO/OTT→共有メモリ→隔離OBS 32.2.2/48kHzを3600.015秒運転した。
本体engineのSHA-256は `a61a416fb6d77792ab8764fb80632b668f3aa0e630315505fc4ce46d727b3151`。
試験用の物理モニターは無音とし、発声・音楽の聴感や映像同期の受入とは分ける。

| 確認 | 結果 |
| --- | --- |
| 録音音声 | 3599.9147秒、172,795,904 frame、PCM16 stereo 48kHz |
| 音声packet | 168,746件、時刻欠損0 |
| 入力側 | underflow・zero fill・drop・回復処理の増分0 |
| OBS受信 | underrun/overrun/invalid/stale/reset/lock失敗/clock再初期化/接続変更の増分0 |
| OBS追加バッファ | 0ms。共有ringの待ち量や物理経路の総遅延が0という意味ではない |
| 参照・試験信号 | 参照取りこぼし0、再生不足0 |
| 波形 | 初回の固定比率・小数sample位置補間で9窓すべて一致、最小相関0.9993721。事後affine解析は未使用 |
| 保存と復元 | 凍結23ファイルは収録終了まで不変、補足7ファイルも途中の観測値から終了まで不変。通常OBS設定を保持し、Voicemeeterの元のpatch/muteへ復元、cleanup errorなし |

解析は14/14項目成功。全時間の波形の完全一致、実配信のGPU/encoder負荷、他rateの実機1時間を保証しない。
証拠は当該worktreeの `build/native-20261103-190908/runs/clock-off-20261103-190922/` に保持する。
補足manifestは運転途中の追加観測であり、元の開始時manifestへ書き戻していない。
後続のCargo検証でtargetを再使用する前に、凍結一致のreceiptを保存し、試験済みengineを別の `build/frozen-runtime-for-acceptance/` に同一hashで保持した。
後の再ビルドexeや別条件へ、この1時間の合格を転用しない。
