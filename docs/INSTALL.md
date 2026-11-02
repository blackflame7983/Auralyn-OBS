# OBSソース試作のパッケージと隔離portable試験

この手順は未公開のローカル試作を、作業リポジトリの `build/` 配下にある隔離portable OBSへ導入して検証する。
通常OBSの設定やインストール先には使用しない。正式なインストーラー、署名、一般配布の受入は未完了。

## 配布候補を作る

OBS 32.2.2、Windows x64、通信1.0を対象とする。1時間の実機結果の必須チェック、音声パケット長、3点以上の非無音波形の相関、およびDLLとソース3ファイルが記録と一致する場合だけ候補を作る。`analysis.json` の `checks.recordedAudioMatchesReference` も真である必要がある。旧形式の解析結果を使う場合は原本を保持し、実波形測定値を再評価した別の証拠を指定する。
ソースは明示リストから取得し、録画、環境、認証設定、SDK checkout、build成果物をソースZIPへ混ぜない。

```powershell
python tools/package_plugin.py --output build/packages/local-poc-1 --evidence build/再評価した証拠/code-manifest.json
```

出力先は未作成の新しいディレクトリを指定する。既存の候補を上書きしない。
バイナリZIP、ソースZIP、manifest、`SHA256SUMS.txt` を同じディレクトリに生成する。
バイナリZIPにはDLL、LICENSE、NOTICE、ソースZIP名とそのSHA-256、対応版を含む。ソースZIPには独自ソース、ビルド手順、通信スナップショット、試験と操作ツールを含む。

ソースZIPは未コミットの作業ツリーから作成したsnapshotで、公開済みcommitの再現物ではない。本体の試作送信部も未コミットであり、記載した本体の基点commitだけでは動作しない。
実機の記録と照合した対象はDLLと主要3ソースであり、全ビルド入力の来歴を証明するものではない。
OBS SDKは含めず、manifestとNOTICEに固定元 `https://github.com/obsproject/obs-studio` とcommit `ba2f32bdf791005443988a4955e963663e16b1ed` を記録する。
正式公開前には全ビルド入力、各依存の権利と対応ソースへのアクセス、同一commitからの再現性、署名、対応表を別途確認する。

## 隔離portable OBSへ導入する

起動中の対象OBSを終了してから操作する。OBSが起動中のファイル置換・読み込みは、この手順の対象外。
対象rootはこのリポジトリの `build/` 配下に置き、`portable_mode.txt`、`bin/64bit/obs64.exe`、`obs-plugins/64bit/` が存在する隔離コピーを使う。
対象を明確にした上で、root直下に文字列 `Auralyn OBS isolated portable test only` と改行だけを含む `.auralyn-obs-isolated-test` を用意する。
既存の通常OBSや、目的の分からないportable OBSにはマーカーを作らない。

```powershell
# 例のパスを、用意した隔離コピーに置き換える。
$testRoot = (Resolve-Path 'build/isolated-obs/obs-portable').Path
$binaryZip = 'build/packages/local-poc-1/auralyn-obs-0.1.0-poc-windows-x64.zip'
[IO.File]::WriteAllText((Join-Path $testRoot '.auralyn-obs-isolated-test'), "Auralyn OBS isolated portable test only`n", [Text.UTF8Encoding]::new($false))
python tools/manage_install.py install --root $testRoot --package $binaryZip
python tools/manage_install.py install --root $testRoot --package $binaryZip --apply
```

`--apply` なしは読み取り専用のプレビュー。対象root、現在の版・SHA-256、変更後の版・SHA-256を確認し、同じ引数に `--apply` を付けて実行する。
ZIPは展開せず、許可した固定名の内容、DLLのSHA-256、対応版を検証してからコピーする。改変を検出する仕組みであり、署名や配布元の認証ではない。
対象DLLが未管理の既存ファイルなら上書きしない。導入後にDLLが変更された場合も更新・削除を拒否する。他のプラグインやOBS設定は触らない。

導入後は隔離OBSを起動し、目的のシーンで音声ソース「Auralyn PCM（試作）」を追加する。
ソースの接続名にAuralyn側と同じ値を指定し、音声開始後にOBSのメーターと録画で到達を確認する。
同じ音声を別のOBSソースでも取り込んでいる場合は、意図しない二重音声にならないよう構成を確認する。

## 更新・復帰・削除

更新は新しいZIPで同じ `install` を使う。すでに同じ候補が入っていれば変更しない。
更新前のDLLと管理情報はroot内の `.auralyn-obs-backups/` にSHA-256名で残す。
元に戻すときは、バックアップDLL名の64桁のSHA-256を指定する。

```powershell
$previousSha = '前のDLLのSHA-256に置き換える'
python tools/manage_install.py restore --root $testRoot --backup $previousSha
python tools/manage_install.py restore --root $testRoot --backup $previousSha --apply
python tools/manage_install.py remove --root $testRoot
python tools/manage_install.py remove --root $testRoot --apply
```

削除後もバックアップは残す。削除後はまず `install` で再導入し、`restore` は管理対象が導入中の場合に使う。不要と判断した場合は内容を確認して利用者が管理する。
`.auralyn-obs-install.lock` が操作後も残るのは正常であり、稼働中の別操作は拒否する。ロックはOSが保持し、強制終了時も解放されるので、ファイルを手動削除する必要はない。

通常の失敗では操作前のDLLと管理情報へ戻す。中断して `.auralyn-obs-transaction.json` が残った場合は、まず次をプレビューし、内容を確認してから実行する。

```powershell
python tools/manage_install.py recover --root $testRoot
python tools/manage_install.py recover --root $testRoot --apply
```

中断後にDLLや管理情報が別の値へ変更されている場合、復旧も拒否して調査可能な状態を保持する。
シンボリックリンク・junctionを経由する管理対象、root外へのパス、想定外のZIPメンバーは受け付けない。

## OBSの探索先と正式導入の境界

固定したOBS 32.2.2ソースの `libobs/obs-windows.c` は既定の64bit探索先として `../../obs-plugins/64bit` を登録する。
この試作で操作するDLLは、portable rootから見た `obs-plugins/64bit/auralyn-obs.dll`。
同じOBSソースの `frontend/widgets/OBSBasic.cpp` では、非portable Windows版にProgramDataの `obs-studio/plugins/%module%/bin/64bit` を追加する。
portable modeはその追加経路の登録前に戻る。したがって、今回のportable操作は通常版ProgramDataへの導入検証にはならない。
通常版での権限、競合、更新、削除、インストーラーの挙動は別途確認する。
