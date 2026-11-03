# ProgramData 形式の隔離配置試験

`manage_install.py` の `programdata-sandbox` は、実際の ProgramData や通常の OBS に書き込まず、DLL の配置と導入・更新・復帰・削除・中断復旧を試すためのモードです。入力は既存の portable 用バイナリ ZIP です。配布用の ProgramData 形式 ZIP は `prepare_release.py` が別に作ります。

隔離先は、このリポジトリの `build` 配下にあり、末尾が厳密に `ProgramData/obs-studio/plugins/auralyn-obs` のディレクトリに限ります。`bin/64bit` を作り、root に `.auralyn-obs-programdata-sandbox` を UTF-8、LF 改行、次の内容で置きます。

```text
Auralyn OBS isolated ProgramData layout test only
```

マーカー末尾に改行が必要です。portable 用マーカーまたは `portable_mode.txt` の混在、リンク・junction などの再解析ポイントは拒否します。DLL は `bin/64bit/auralyn-obs.dll` に配置し、管理情報とバックアップは隔離 root 内に保持します。実 ProgramData は `build` 配下ではないため対象外です。

まず `--apply` なしで変更予定を確認します。以下の `ROOT` と `PACKAGE` は、用意した隔離先と portable 用バイナリ ZIP のパスに置き換えてください。

```powershell
python tools/manage_install.py install --root ROOT --layout programdata-sandbox --package PACKAGE
python tools/manage_install.py install --root ROOT --layout programdata-sandbox --package PACKAGE --apply
python tools/manage_install.py restore --root ROOT --layout programdata-sandbox --backup SHA256
python tools/manage_install.py remove --root ROOT --layout programdata-sandbox
python tools/manage_install.py recover --root ROOT --layout programdata-sandbox
```

`restore`、`remove`、`recover` も `--apply` を付けた場合にのみ実行されます。復帰対象の SHA-256 は管理された過去の DLL のものを指定します。外部変更や一致しない管理情報を検出した場合は保護のため中断します。

この試験が示すのはファイル配置とトランザクション処理だけです。通常 OBS への導入、プラグインの読み込み、実機音声の受入を示すものではありません。通常 OBS への適用には別途、対象と影響を示した明示的な承認が必要です。
