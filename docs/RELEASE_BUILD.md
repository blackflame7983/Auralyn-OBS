# 未公開の配布候補と別ディレクトリでのビルド

この候補はOBS 32.2.2 / Windows x64、48 kHz Sinc試作に限定する。未署名・未公開・公開未承認である。48 kHz以外は線形補間となる。実機1時間検証は指定した検証済みパッケージのDLLに限り、再ビルドしたDLLには適用しない。

## 候補生成

以下は作業環境内の例。指定先は未作成の `build/` 配下とする。指定した検証済みパッケージと実機証拠は書き換えない。

```powershell
python tools/prepare_release.py --original build/packages/検証済みパッケージ --evidence build/実機検証/code-manifest.json --output build/packages/release-local-candidate
```

指定パッケージのclean commitと元ソース、4つの実機検証対象ソース、DLL、通信規約、Sinc方式を照合する。checkout全体がcleanで、準備用の3ファイルを含むすべての収録pluginソースがtrackedかつHEADと一致することを確認する。SDKは `--sdk` で指定でき、省略時は `build/cmake/CMakeCache.txt` の `OBS_SOURCE_DIR` を使う。指定時も同設定と一致させる。固定commitのtracked sourceから取得し、submodule、バイナリ資産などを除外する。除外パスと理由はmanifestに記載する。これはOBS全体やlibobs自体の完全なビルド用ソースとは主張しない。対応ソースの充足、各依存の条件、正式公開の可否は別途確認する。

バイナリZIPの `auralyn-obs/bin/64bit/auralyn-obs.dll` はOBS公式の通常版ProgramDataプラグイン探索構成に合わせた相対パスである。候補のmanifest、LICENSE、NOTICE、SOURCEはZIP直下にある。OBS公式の [Plugins Guide](https://obsproject.com/kb/plugins-guide) に記載された構成に合わせ、`auralyn-obs/data/locale/en-US.ini` も同梱する。現在のDLLは表示文字列をコンパイルしており、このlocaleファイルを参照しない。生成内容は `tools/prepare_release.py` の定数で管理し、manifestにSHA-256を記録する。通常OBSへの導入、権限、更新・削除や実機動作は未検証であり、このツールはProgramDataに書き込まない。

manifestのcompiler、linker、生成config、import元OBS DLL等のSHAは候補生成時の観測値であり、指定したDLLのビルド時に同一であった証明ではない。パスや環境変数は記載しない。

## ソースZIPから別ディレクトリへ再ビルド

Windows x64、Visual Studio 2022 C++ toolchain、CMake 3.24以降、Python、OBS 32.2.2の `obs.dll` が必要。OBSのDLLはZIPに含まれない。未作成の作業フォルダーへsource ZIPを展開して以下を実行する。既存の `build/cmake` は指定しない。

```powershell
$sourceRoot = (Resolve-Path '展開先').Path
$obsBin = 'C:/Program Files/obs-studio/bin/64bit'
cmake -S "$sourceRoot/plugin" -B "$sourceRoot/rebuild" -G 'Visual Studio 17 2022' -A x64 -DAURALYN_SINC48_POC=ON -DOBS_SOURCE_DIR="$sourceRoot/obs-sdk" -DOBS_BIN_DIR="$obsBin"
cmake --build "$sourceRoot/rebuild" --config Release
ctest --test-dir "$sourceRoot/rebuild" -C Release --output-on-failure
```

この手順はpluginのビルド入力確認であり、同一バイナリの再現、再ビルドDLLの音声品質、連続1時間運転を保証しない。欠落した入力や外部依存は追加確認が必要。
