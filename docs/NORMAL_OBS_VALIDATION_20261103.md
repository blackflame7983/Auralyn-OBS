# 通常OBSへのr17導入と録音確認

2026-11-03。利用者の導入承認後、インストール済みのOBS Studio 32.2.2 x64へr17を配置し、
専用プロファイルで再起動・音声到達・録画を確認した。今回の16項目はすべて成功した。
公開、署名、通常版Auralynへの組み込み完了を意味しない。

## 対象と配置

- 配置先: `%ProgramData%\obs-studio\plugins\auralyn-obs`。
- DLL: `bin/64bit/auralyn-obs.dll`。
- DLL SHA-256: `c5f3ebeee4d0182071c2207e15d1dd321c17ab6dd6802c351ff5d626d6709aff`。
- 候補: `build/packages/release-ready-r17-20261103`。プラグインソースは `4b359fdb46d7d3d670262b0a1172a5fb19619e24`。
- 本体エンジンSHA-256: `a61a416fb6d77792ab8764fb80632b668f3aa0e630315505fc4ce46d727b3151`。
  同じDLLの既存1時間試験で使用した実行ファイルを保持して使った。

通常の `C:\Program Files\obs-studio\bin\64bit\obs64.exe` をportable指定なしで起動した。
OBS自身のログの `Portable mode: false` とモジュール読み込み、WebSocket APIのソース種別で確認した。
同名DLLの競合は導入前になく、新規配置したDLLは検証後も残している。

## 実機で確認した範囲

経路は合成の帯域制限ノイズ → WASAPIのVoicemeeter AUX → 実際のVoicemeeter Insert Virtual ASIO 96kHz
→ 本体エンジンのOTT → 導入済みDLL → 通常OBS 48kHz stereo。
物理モニターは消音し、本体エンジンには隔離した設定保存先を使った。発声や音楽の録音ではない。

専用プロファイルとシーンコレクションはともに `Auralyn223-r17`。
ソース `Auralyn PCM #223` を保存してOBSを正常終了し、再起動後の保持を確認してから録画した。
録画時は検証専用の接続名 `normal223-r17` を使い、終了前に利用用の `auralyn` へ戻して保存した。

| 確認 | 結果 |
| --- | --- |
| 運転時間 | 120.031秒 |
| 録画音声 | PCM 16bit、48kHz、stereo、119.979秒 |
| 音声パケット / フレーム | 5,624 / 5,758,976 |
| パケット時刻の不連続 | 0 |
| 本体入力キューのunderflow / zero-fill / drop / recovery増分 | すべて0 |
| 受信側underrun / overrun / invalid / stale / reset等の増分 | すべて0 |
| OBSログの追加バッファ | 0ms |
| 参照波形との正規化相関 | 3区間、最小0.999882 |
| 試験中の対象コード・実行ファイルの変更 | なし |
| 音声解析の受入項目 | 16 / 16成功 |

OBSの追加バッファ0msは、経路全体の遅延0msを表さない。
波形照合のファイル位置差約94msも、物理入力から出力までの遅延や映像同期の測定値ではない。
本体の汎用 `glitch_count` は音声欠落の直接測定ではないため、欠落判定には入力キュー、受信側統計、
録画サンプルとパケットの連続性を使った。

## 元の設定の保全

OBSは検証で起動したプロセスだけを正常終了した。Voicemeeterは96kHzを維持し、検証用の一時設定を復元した。
通常OBSの既存657ファイルはすべて事前SHA-256と一致し、元のプロファイル・シーン選択も復元した。
専用プロファイルの `basic.ini` とシーンJSON・バックアップJSONの3ファイルだけを追加状態で残した。
検証で生成したログ・profilerの6ファイルは証跡ディレクトリへ移して保持した。

通常設定の論理パスと `Path.resolve()` の実体パスには差があった。
原因は確定していない。ルートの一括移動は行わず、停止中に差分ファイルだけを復元し、全既存ファイルを再照合した。
元のOBS接続認証設定、Auralynの通常設定、他のシーン・プラグインは変更していない。

## 検証用起動環境の修正

最初の制限環境による起動では、通常OBSがProgramDataのプラグインを探索できなかった。
同じ子プロセス環境で `SHGetFolderPathW(CSIDL_COMMON_APPDATA)` を調べると、`SYSTEMDRIVE` なしでは失敗し、
これを許可リストへ加えると成功した。`tools/native_support.py` を修正し、実際のShell APIを使う回帰試験を追加した。
Python全53試験が成功した。製品DLLやコンパイル対象4ファイル、固定済みr17 ZIPは変更していない。

## 証跡と残る確認

ローカル証跡の起点は `build/normal-obs-r17-20261103/`。
`runs/normal96-120s/{result,analysis,code-manifest}.json`、`normal-obs-run.log`、録画と参照音声、
`restoration.json`、`python-regression.log` を保持する。元設定のバックアップや接続認証情報は公開ソースへ含めない。
既存の同じDLLの1時間試験は別証跡であり、今回通常OBSを1時間動かしたとは扱わない。

試聴による音質判断、実マイク・音楽、声と拍手による映像同期、追加rateの実デバイス受入、
通常版Auralynの画面からの一連の操作、署名と公開は未確認。
画面操作ツールが起動できなかったため、今回はOBS API・実ログ・録画解析による確認であり、画面の目視確認は含めない。

利用時はOBSのプロファイルとシーンコレクションを `Auralyn223-r17` に切り替える。
接続名は `auralyn`。本体の送信側と一致させる。現在は元のOBS選択へ戻し、検証プロセスを終了している。
