# 96kHz入力から48kHz OBS録画への変換

2026-11-03。Issue #223のローカル試作。実装・オフライン測定・実機試験を区別する。
本体の96kHz処理を維持し、別プロセスのOBS受信だけで48kHzへ変換する。通常版採用・聴感・公開の合格ではない。

## 実装

`AURALYN_SINC48_POC=ON` の同じスイッチに96→48kHzの専用bankを追加した。既定OFF。
256tap/256相、入力Nyquistに対するcutoff 0.465、過去127/未来128 frame、scratch 4608 frame。
48→48kHzの128tap/0.93とその他のrateの線形補間は維持した。
入力rate・出力rate・epoch変更時に再primeし、描画後にもepoch・state・rateを再確認する。
係数・scratchは制御側で確保し、音声描画中の確保・解放・待機を追加しない。

検証対象DLLのSHA-256は `da20884097699bb72d2afe4d0613e1c8122769d92475e61022046f3c40f99a08`。
本体engineは `70cfb47255031353ec5b8217504f367a88a006613fa8a21f01d1e6b91d97973e`。
後者の入力clock補正featureは有効ビルドだが、ASIOでは補正は動作せず `inputClock: null`。

## 同じデジタル入力の測定

70秒の同じprobeを速度差0/±37/±500ppmでReaderへ与えた。15条件全てで転送modelと測定基準を満たした。

| 測定 | 結果 |
| --- | --- |
| 48kHz、100Hz/1kHz/10kHz/19kHz | 帯域内power差の絶対値0.000084dB未満 |
| 96→48kHz、上記と20kHz | 帯域内power差の絶対値0.000033dB未満 |
| 96→48kHz、24.5/26/30/40kHzの折り返し | 最大−113.589dB、基準−80dB以下 |

数値は各tone±80Hzのpower差。全帯域の歪率・SNR、全機器、聴感の受入へ拡張しない。
入力raw・出力raw・SHA・PSD・統計・時系列を `build/quality96-20261103-095106/` に保持する。
可聴帯域用は音量を揃えた試聴WAVも保存し、alias用は抑圧音を増幅しない。

## 実機試験の方法

合成の帯域制限noise → 明示WASAPI AUX → Voicemeeter Insert Virtual ASIO 96kHz → 本体OTT → 共有メモリ → 隔離OBS 32.2.2/48kHz録画。
NRはOFF、OTTは1 instance。隔離OBSは320×180/10fps、音声PCM16bitで保存する。通常配信の映像・encoder・GPU高負荷は別条件である。
WASAPI AUXのWindows mixは48kHzで、VoicemeeterがASIOの実動作96kHzへ変換する。96kHzの収録素材の試聴や実マイク音声ではない。
preferred値だけでなく `Bus[0].device.sr` と本体・共有メモリの実測rateを確認する。
通常のVoicemeeter周波数・機器・音量は変えず、未使用AUXのInsert 14/15とmuteだけを試験用にする。
元のInsert 6/7等はjournalへ先行記録し、試験信号停止・所有engine終了後に比較して復元する。外部の編集値は上書きしない。
物理モニターはミュート、通常OBSの保存内容はhash比較する。

波形照合は録画と96kHz post-OTT参照を独立したオフラインFIRで48kHzに揃える。
最初は整数sample位置の±1sampleだけを細分化したが、1時間の3000秒窓では固定比率の相関が0.89698になった。
独立診断で約332ppmの相対速度差、150ms内で約2.39出力sampleの伸縮を確認した。周辺3窓の位置差も約330ppmの連続した進行に整合した。
既知の受信clock補正範囲±2000ppm内の単一affineで比較すると同じ波形が一致する。nominal rateが物理的に完全一致した証拠ではない。
新しい `tools/analyze_native_clock.py` は150ms内の時間位置・一定の相対速度差の2変数だけを合わせ、同じfitの6×25ms区間全てにも非無音・相関0.95以上を要求する。
6区間は同一fitでの局所整合であり、独立した試験ではない。任意のwarpや音量加工はしない。
固定比率の相関、推定ppm、探索上限も保存する。既知遅延・半sample・±2000/500ppm、上限外5000ppm、別音声、無音、64frameの飛び（中心/25ms境界）、25ms無音をfixtureで確認した。
単一sampleの欠損や全時間の波形無欠陥を保証する判定ではない。
ファイルの位置差・queue量は物理的な総遅延ではない。

## 現在の到達点

既存USBマイク→OTT→48kHz経路の新DLL60秒回帰は全12項目成功。
`build/native-20261103-100913/runs/clock-on-20261103-101302/` に保存した。
96→48kHzの最終60秒は `clock-off-20261103-101430/`、全14項目成功。
相関最小0.9999945、欠損・追加バッファ0、元のVoicemeeter設定への復元を確認した。
整数sampleだけの予備解析が位相差で基準を下回った試行は予備記録として保持し、最終合格へ転用しない。
1時間 `build/native-20261103-100913/runs/clock-off-20261103-101630/` は実時間3600.078秒、録画の映像時間3600.000秒で完了。
音声は172802048 frame / 168752 packet / 3600.0427秒、時刻欠損0。参照は96kHz・345619968 frame、取りこぼし0。
入力不足callback・ゼロ充填・破棄・回復、受信underrun/overrun/invalid/stale/reset/接続変更/lock失敗/clock再初期化の録音中増分は全て0。
OBS追加バッファ0ms、受信queue標本44.6354〜55.3438ms。
事後clock解析の全14項目が成功し、8窓の相関最小0.9999915、同一fitの48区間の相関最小0.9999796。
原23ファイルは録音後もhash不変。DSPや収録コードは変更しておらず、録画を取り直したとは表示しない。
元の固定比率判定は `analysis-fraction-only.json`、再評価は `analysis.json` に保持した。
新しい事後解析コードは原23ファイルとは別に、tool/base解析/原manifest/result/初回解析/参照/録画のSHAを記録し、候補生成時も一致を必須検査する。
入力・出力raw、元の不合格判定や補正前の数値を消して合格に置き換える方式ではない。
通常OBS設定は不変、試験信号の再生不足0、終了後のVoicemeeterは実動作96kHz・元のInsert 6/7 ON・AUX mute OFFへ復元済み。

新DLLの48kHz操作回帰は `build/native-20261103-112541/scenarios-20261103-112733/`。
15区間全て成功、66.24秒、ミュート・モード無音保持/復帰・Stop/Start・所有engine強制終了/再起動・42機器スキャンを確認した。
録画packetの時刻欠損・OBS追加バッファ・スキャン時のエラー増分0、物理モニターはミュート、通常OBS設定も不変。
既存sessionを使う初回の操作試験はsource名競合で開始前に中断したため、新しい隔離sessionでやり直した。

ON/OFF各ビルドのCTest 2件、Python 38件が成功した。
最大block/clock補正境界、tag履歴、rate/epoch切替、描画中の割当0、帯域外成分を検出できる負例を含む。
独立レビューの設定所有権に関する2指摘は修正し、準備中の外部編集やjournal保存失敗を保持する負例を追加した。

試験プロセスの強制終了や停電では `finally` による復元は保証できない。
その場合は録音・試験信号・所有engineの終了を確認し、`voicemeeter-journal.json` のoriginalと現値を比較してから復旧する。
Remote APIに不可分な比較更新機能はないため、最後の確認とAPI書込みの極短い間の同時編集を完全には排他しない。

利用者の比較試聴と声/拍手による映像同期、通常OBS導入、署名・公開は未確認。
