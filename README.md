# Auralyn OBSソース（ローカル試作）

Windows x64 / OBS 32.2.2専用の音声ソース。GPL-3.0-only。本体の加工済みstereo PCMを共有メモリから受け取り、OBSの音声周期で消費する。
本体・プラグイン・通信仕様の版を分け、仕様の正本はAuralynに置く。参照版は `protocol.lock.json`、独立ビルド用スナップショットは `protocol/`。
本体とプラグインはローカルcommitで区切り、実機で使った実行ファイルとソースのSHA-256を一組として確認する。
`auralynBaseCommit` は通信仕様を参照したcommitであり、検証に使った本体実行ファイルや正式な最低対応版を表さない。

このリポジトリにはリモート・公開Release・インストーラーはまだない。通常OBSへの自動導入、設定変更、マイク取得、録画、配信は行わない。
試作の線形補間は音質を受入済みではなく、OBSの他の版との互換性も宣言しない。

この作業ツリーでは `-DAURALYN_SINC48_POC=ON` の検証用ビルドで、48kHz→48kHzだけを128 tap/256相のSinc補間へ切り替えられる。
既定OFF。他のrateは線形補間のまま。係数・scratchはSource生成時に用意し、render中の確保・解放・係数再生成は行わない。
prime時は過去63 frameの履歴も新しい世代から揃え、未来64 frameをqueue内で参照する。これらを物理的な総遅延の実測値とは扱わない。
同じ入力による比較は `build/quality-final/`。実機の1時間合格は新しいDLLで別に判定する。

```powershell
cmake -S . -B build/cmake -G "Visual Studio 17 2022" -A x64 -DAURALYN_SINC48_POC=ON
cmake --build build/cmake --config Release --parallel 1
ctest --test-dir build/cmake -C Release --output-on-failure
```

`tools/native_clock_trial.py` は本体ソース・固定engine・隔離OBS sessionを明示して使い、受信側の `interpolation` が指定方式であることも開始/運転中に照合する。
これは明示実行する実入力録画であり、通常のテストやビルドからは起動しない。通常OBSの設定・物理モニターを変更しない。

## 受信処理だけのオフライン音質測定

実機の長時間録画とは別に、`pcm_tests --quality` でDLLと同じ `Reader` をheap上のデータへ適用できる。
共有メモリIPC・実OBSアプリ・実時間性能の試験ではない。入力はstereo 48kHz f32leの4〜300秒、出力先は未作成のディレクトリに限定する。
raw前後・統計・時系列を残し、解析ではhash、波形/PSD、100/1000/10000/19000Hzの帯域内のレベル比と音量を揃えた試聴WAVを保存する。
帯域幅±80Hzにはclock補正の周波数変動も含める。固定周波数からのズレを歪率やSNRとして扱わない。起動時の待機と入力先頭のskipは別に記録する。

```powershell
cmake --build build/cmake --config Release --target pcm_tests --parallel 1
# build/pcm-quality は自分で新規作成する。既存の測定を上書きしない。
python tools/pcm_quality.py probe --output build/pcm-quality/probe.f32 --seconds 70
build/cmake/Release/pcm_tests.exe --quality build/pcm-quality/probe.f32 build/pcm-quality/tone-37 37
python tools/pcm_quality.py analyze --run build/pcm-quality/tone-37 --output build/pcm-quality/tone-37-analysis --executable build/cmake/Release/pcm_tests.exe --probe build/pcm-quality/probe.f32.json
```

音声素材は同じ入力を各速度差で使う。素材を渡す場合はprobe生成を省き、解析の `--probe` も指定しない。
transportのmodelが健全でも、音質の採用判断や聴感の合格を自動で付けない。入力・出力ファイルの位置差は物理的な総遅延ではない。

## ビルド

前提: Windows 11 x64、Visual Studio 2022 C++ Build Tools、Windows SDK、CMake 3.24以上、Git、Python 3、インストール済みOBS 32.2.2。
PowerShellでリポジトリルートから実行する。OBSのDLLは読み取りだけに使用し、インストール先を書き換えない。

```powershell
git clone --depth 1 --branch 32.2.2 https://github.com/obsproject/obs-studio.git build/obs-studio
# HEADが ba2f32bdf791005443988a4955e963663e16b1ed であることを確認
git -C build/obs-studio rev-parse HEAD
python tools/verify_protocol.py --auralyn ../VSTHost-obs223
cmake -S . -B build/cmake -G "Visual Studio 17 2022" -A x64
cmake --build build/cmake --config Release --parallel 4
ctest --test-dir build/cmake -C Release --output-on-failure
build/cmake/Release/pcm_tests.exe --long
```

OBSの場所が違う場合はconfigure時に `-DOBS_BIN_DIR=...` を指定する。
通常のclock試験は各120秒相当、`--long` は各1時間相当の合成列を計算する。実時間の1時間運転とは別の試験。
CMakeは取得ソースの版とcommitを照合し、インストール済み `obs.dll` のexportsからローカルのimport libraryを作る。
ヘッダー・libobs・D3D11等の外部バイナリはGitへ追加しない。
生成DLLは `build/cmake/Release/auralyn-obs.dll`。プラグインは実行時のOBS版も32.2.2に限定して検証する。
ソースの種類IDは `auralyn_pcm_poc_v1`、表示名は「Auralyn PCM（試作）」、設定 `endpoint` は送信側と同じ接続名。

## 本体の送信試作

本体の作業ツリー（今回は `../VSTHost-obs223`）のルートで実行する。有効なASIO SDKを `CPAL_ASIO_DIR` で指定しておく。

```powershell
python scripts/validate-obs-poc.py
src-tauri/target/debug/examples/obs_pcm_sender.exe issue223 10 48000 500
```

これは実入力・音声機器を使わず、440Hz / peak 0.05の逆相stereoを共有メモリだけへ送る。最後の数値は合成の速度差ppm。
物理音声や配信先へ直接再生しない。検証以外のプラグイン導入先で鳴らす場合は、そのOBSのソース設定・モニター・配信状態を確認する。

本体のOBS設定にある「OBSへ直接送信（試用）」を有効にし、表示された接続名をOBSソースへ指定する。
送信は既定OFFで、音声停止中だけ設定を変更できる。本体から送信中でも、OBSで受信できたことを意味しない。OBSメーターと録画で確認する。
通常の `obs-source` 機能は保存設定を使う。旧 `obs-bridge-poc` featureと `AURALYN_OBS_POC_ENDPOINT=issue223` は、単体engineの検証ハーネスの互換用に残している。
試作エンジンを通常の配布用sidecarへ上書きせず、実デバイス確認は隔離したHost/sidecarを用意して行う。
ローカル配布候補の生成、隔離portableへの導入・更新・復帰・削除は [導入手順](docs/INSTALL.md)を参照する。公開Releaseや通常OBSへの自動導入は用意していない。
一般配布に進むための残件は [配布準備](docs/DISTRIBUTION_READINESS.md) を参照する。

## 実共有メモリと実libobsの到達確認

本体のexampleをビルドした後、次を実行する。`--sender` は実際のビルド先に合わせる。

```powershell
python tools/smoke.py --sender ../VSTHost-obs223/src-tauri/target/debug/examples/obs_pcm_sender.exe
python tools/smoke.py --sender ../VSTHost-obs223/src-tauri/target/debug/examples/obs_pcm_sender.exe --crash-first
```

それぞれ約18秒。隔離したlibobsにDLLと32pxのoffscreen viewをロードし、送信プロセスを再起動する。
48kHz→44.1kHzへ変えて再接続し、最終mixの非無音・左右逆相・停止後無音・再接続回数を確認する。
`--crash-first` は最初の合成音送信プロセスだけを終了して異常停止も確認する。
通常OBSの起動・collection変更・プラグイン導入・音声入力・録画・配信はしない。
ログとJSON結果はignoredの `build/` に保存する。GPUはlibobsのoffscreen初期化に使う。
合成音・実libobsの確認は、実マイク/VST・OBSアプリの録画・聴感・1時間の実機合格を表さない。

## 実マイク・実VST・実OBSアプリの隔離検証

`tools/native_acceptance.py` は、この実機試作向けの明示実行用ハーネス。
実際の入力を録画するため、通常のCTestには含めない。Pythonのnumpyと、解析時にはscipy/FFmpegを使う。
`--device` には最新のengine機器一覧と一致する入力DeviceInfoのJSONを指定する。
この確認ではWindowsのcapture endpointプロパティ「USB 2.0 Camera」とDevice IDを照合している。

```powershell
python ../VSTHost-obs223/scripts/validate-obs-poc.py --build-engine-only
python tools/native_acceptance.py --prepare-only
# 作成されたsessionと実機のDeviceInfoを指定する。物理出力はミュートしたclock源。
python tools/native_acceptance.py --session build/native-YYYYMMDD-HHMMSS --device path/to/device.json --output 14T-N72 --duration 90
python tools/native_acceptance.py --session build/native-YYYYMMDD-HHMMSS --device path/to/device.json --output 14T-N72 --duration 3600
```

新規portable OBS、空のprofile/scene、検証専用WebSocket認証を作る。通常OBSの設定・配信は変更しない。
録画と参照PCMはsession内に残す。結果を見ずに自動で合格扱いにしない。
実行中にrun内の `stop.request` ファイルを作ると、録画停止・engine終了を行って途中結果を保存する。
結果には要求時間と実時間を残すため、途中停止を1時間の合格には使わない。
詳細は本体の `docs/plans/OBS_SOURCE_PLUGIN_223_NATIVE_VALIDATION.md`。

完了したrunは次のコマンドで集計する。`analysis.json` と `timeline.png` を同じrunに保存する。
matplotlib/scipy/FFmpegも必要になる。ファイル間の波形位置は録画開始の差を含むため、物理経路の総遅延ではない。

```powershell
python tools/analyze_native.py build/native-YYYYMMDD-HHMMSS/runs/YYYYMMDD-HHMMSS
```

### ミュート・再起動・機器スキャン

新規portable OBSで実入力を使う明示試験。物理モニターをミュートし、通常設定を分離する。

```powershell
python tools/native_scenarios.py --scans 5
python tools/native_scenarios.py --analyze build/native-YYYYMMDD-HHMMSS/scenarios-YYYYMMDD-HHMMSS
python tools/scan_stress.py
```

前者は全体/VSTミュート、無音ゲート、停止/再開、検証用engineの異常終了/再起動を録画する。
後者は物理再生しない合成送信と実libobsの統計取得集中に、実機器スキャンを組み合わせる。証拠は毎回新しいbuild配下へ保存する。
録画の無音判定は各操作後500msを除いた2秒。0msでの無音到達や、耳までの総遅延を保証する試験ではない。

### 実HostのVST一体切替

`native_host_session.py --prepare-only` で試験用Tauri設定を作り、その設定で本体をビルドする。
`--root` と `--obs-session` を指定して起動すると、最小webviewのHTTPサーバー、Host、同じビルドのsidecarを隔離して用意する。
返されたPIDとrootを保持し、終了時はHostの正規終了APIと、その起動が所有するサーバーだけを回収する。
検証用Hostから製品画面の操作性を評価したとは扱わない。

```powershell
python tools/native_host_session.py --prepare-only
# 返されたrootの設定を指定する。通常配布用sidecarは置き換えない。
python ../VSTHost-obs223/scripts/validate-obs-poc.py --build-host-config <root>/tauri-config.json
python tools/native_host_session.py --root <root> --obs-session <portable-OBS-session>
python tools/native_host_modes.py --cli <tauri-mcp-cli/dist/index.js> --obs-session <portable-OBS-session> --native-result <scenarios-run>/result.json
python tools/native_scenarios.py --analyze <portable-OBS-session>/host-modes-YYYYMMDD-HHMMSS
```

Tauri MCPのdriver接続を検証し、試験用identifierへ明示的にIPCを送る。実OTTのstateを含む2つのプロファイルを保存して切り替える。
テスト用identifierと保存先の分離が必要。通常のHostや、配布用ビルドへ向けて実行しない。
結果と残る条件は本体の `docs/plans/OBS_SOURCE_PLUGIN_223_BEHAVIOR_VALIDATION.md`。

製品画面で保存・開始した後の長時間録画は `native_ui_soak.py` を明示実行する。
`native_host_session.py --frontend` は最小webviewの代わりに、この本体作業ツリーの製品UIを専用Viteで起動する。
`native_acceptance.py --prepare-only --plugin-package <zip>` は起動前の隔離OBSへ候補を導入する。通常OBSには触れない。
UIの設定保存・再起動保持・接続名・実入力を確認してから `--duration 3600` を指定する。中断時はrun内の `stop.request` を使う。
音声はローカル録画と参照PCMに残る。配信を開始せず、実録画の取得漏れと入力側の不足を別々に評価する。
HDMI音声の消失を避けるため、このハーネスの実行中だけシステム・画面の省電力停止を抑制し、終了時に解除する。永続的な電源設定は変更しない。
録音ソースのミュート・音量・track 1と、開始/中盤/終盤の非無音・参照PCMとの波形一致も必須とする。

## 更新・公開の条件

通信構造・epoch・停止動作を変える場合は、本体の正本から変更し、両側のfixtureと組合せ試験を行う。
仕様互換性と製品版番号を混同しない。OBS更新時はSDK差分と実行時試験を確認する。
本体からの相対時間合わせ・OBS映像同期・他の通話先の出力はこのプラグインへ移さない。

各バイナリReleaseには、同じcommitからの対応ソース、LICENSE/NOTICE、ビルド手順、OBS SDKの正確な由来と必要ソースへのアクセス、チェックサム、対応する本体/OBS/通信版を添付する。
本体のソースZIPで代用しない。配布準備時には追加依存とSDKのソース範囲を改めて確認する。
実機受入・署名・導入/削除・更新経路は未完了。リモート作成・push・PR・公開は明示承認の後に行う。
