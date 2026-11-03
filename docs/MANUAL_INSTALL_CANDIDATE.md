# 通常OBSへ手動導入する候補手順

対象はOBS Studio 32.2.2、Windows x64、OBSの音声設定48kHz。
未公開・未署名の試作候補を、対象と復帰方法の確認後に試すための手順案である。
この文書だけを根拠に通常OBSへ書き込まない。ローカル試験の実績は [配布準備](DISTRIBUTION_READINESS.md) と最新候補の検証記録で確認する。
承認後のr17の新規導入・再起動・120秒録音は [通常OBSの検証記録](NORMAL_OBS_VALIDATION_20261103.md) に記録した。

初回は「導入前の確認」と「導入・更新」を読む。後半の復帰・削除は必要なときに参照する。

## 導入前の確認

1. 候補と同じ場所の `manifest.json`、`SHA256SUMS.txt`、バイナリZIP、対応ソースZIPを揃える。SHA-256はファイル内容の照合値であり、発行者の署名ではない。
2. `SHA256SUMS.txt` と各ファイルの `Get-FileHash -Algorithm SHA256` を照合する。不一致なら導入しない。
3. OBSとAuralynの音声を終了する。OBSのシーンとプロファイルを書き出して退避する。試聴・映像同期などの受入が残るため、すぐ本番配信には使用しない。
4. 次の両方に同名DLLがないか確認する。別の導入方法のDLLがあれば、上書きせず配置元と版を確認する。

```text
%ProgramData%\obs-studio\plugins\auralyn-obs\bin\64bit\auralyn-obs.dll
OBSのインストール先\obs-plugins\64bit\auralyn-obs.dll
```

OBS自身や他のプラグインを更新・削除する必要はない。
既存の `auralyn-obs` フォルダーを退避するときは、OBSのプラグイン探索先の外へ置く。
例えば利用者のDocuments配下に専用の退避先を作り、版と日時を記録する。plugins内へ旧版の別名フォルダーを残すと重複して探索されるおそれがある。

## 導入・更新

バイナリZIPを一度、通常OBSと関係のない新しい作業フォルダーへ展開する。
`auralyn-obs` の中に `bin/64bit/auralyn-obs.dll` と `data/locale/en-US.ini` があることを確認する。
ZIP直下のmanifest、LICENSE、NOTICE、SOURCEと対応ソースZIPは配布記録として保管する。

既存版がある場合は、停止中に `auralyn-obs` フォルダー全体を探索先の外へ退避してから、新しいフォルダーを次へコピーする。
DLLだけを重ねてコピーせず、元の版へ戻せる状態を残す。権限不足や使用中のエラーが出た場合は、変更を進めず原因を確認する。

```text
%ProgramData%\obs-studio\plugins\auralyn-obs\bin\64bit\auralyn-obs.dll
%ProgramData%\obs-studio\plugins\auralyn-obs\data\locale\en-US.ini
```

OBSを起動し、音声ソース「Auralyn PCM（試作）」を選ぶ。
Auralyn側の直接送信と同じ接続名を指定し、メーターと短いローカル録画で到達を確認する。
同じ音声を仮想ケーブルなどから重ねて取り込んでいないかも確認する。
この実操作での読み込み・音声・設定保持・映像同期は、隔離したファイル配置試験とは別に判定する。

## 旧版へ戻す・削除する

OBSとAuralynの音声を終了する。今回配置したフォルダーの内容とDLLのSHA-256が導入時の記録と一致するか確認する。
利用者や別のインストーラーが変更していた場合は、自動的に上書き・削除しない。

旧版へ戻す場合は、新しいフォルダーを探索先の外へ退避し、保存した旧版のフォルダーを元の位置へ戻す。
削除する場合も対象は今回の `auralyn-obs` フォルダーだけとし、OBSの設定、録画、他プラグイン、退避した旧版は消さない。
配置変更の途中で中断したときは、OBSを起動する前に、新旧どちらの一式が所定の位置にあるかを確認する。

## 隔離試験との関係

`validate_manual_package.py` は同じProgramData形式ZIPのフォルダーを `build` 内で導入・更新・退避復帰・削除・再導入する。
`manage_install.py --layout programdata-sandbox` はDLLと管理情報のトランザクションを別に試す。
いずれも実ProgramData、通常のOBS設定、Windowsの権限昇格や署名の受入を代替しない。

不一致や復帰不能があれば、使用した候補版・SHA-256・OBS版と操作直前の状態をIssue #223の検証記録へ残す。録画やOBS接続の認証情報は公開しない。
