# 一般配布に進むための残件

2026-11-03。現在はローカルの試作候補。公開Release・通常OBSへの導入・署名の合格を表さない。
通信仕様1.1（旧rateの1.0互換）、OBS 32.2.2、Windows x64に限定する。他版の対応は宣言しない。

## 版と検証対象を取り違えない

- 本体、プラグイン、通信仕様の版を別に管理する。
- `protocol.lock.json` の `auralynBaseCommit` は通信仕様が存在する参照commit。実機で使った本体版や最低対応版ではない。
- パッケージの `pluginSourceCommit` と `sourceState` は、その時点のローカルGit状態を記録する。変更済み・未追跡あり・Git管理外をcleanと表示しない。
- 実機証拠のmanifest/result/analysisと本体実行ファイルのSHA-256をバイナリに関連付ける。対応ソースのarchive SHA-256はcommitとは別に保持する。
- 本体の入力クロック補正は検証用featureで既定OFF。試作用ビルドの成功を通常版の採用と扱わない。

現行のローカル生成ツールは常に `localOnly: true` とする。公開の最低本体版は、公開する本体の版を決めてから設定する。

## 対応ソースとビルド入力

GPL-3.0-onlyの独自コードと、選択したOBS SDKの告知を維持する。本体のソースZIPでプラグインの対応ソースを代替しない。
[GNUのGPLv3案内](https://www.gnu.org/licenses/quick-guide-gplv3.html)に沿って、必要なソース・依存・ビルド手順を成果物ごとに確認する。

portable候補のソースZIPは明示リストの独自コード・通信snapshot・手順を含む。
`prepare_release.py` の追加候補では固定OBS SDKの必要source/header/license/templateと除外理由も収録し、別ディレクトリからのplugin再ビルドを確認する。
OBS全体の再ビルド入力や正式配布用の対応ソース充足は、これだけで判定しない。
正式候補では次を揃える。

1. cleanなプラグインcommitと、同じ版のバイナリ・ソースZIP・SHA256SUMS。
2. 固定したOBS SDK source commitと、必要な依存・生成設定・告知。SDKが変更されていないことも確認する。
3. compiler/linkerの版・オプション、使用header、import libraryの生成元OBS DLL、成果物のhashを含むビルド記録。
4. ソースZIPからの別ディレクトリでの再ビルド。DLLのhash一致だけに依存せず、版・設定・テスト結果・コードとの対応を確認する。
5. 公開する本体実行ファイルとソースの対応、試験済みの組合せ、双方の配布先。

既存のMSVC `.tlog` に使用headerの記録があり、固定OBS SDK checkoutにtracked変更がないことは確認した。
これだけで全ビルド入力の再現や対応ソースの充足を合格にしない。ASIO SDK・市販VST・認証設定・実機の録音は、このプラグインのソースZIPへ混ぜない。

## 導入と署名

[OBS公式の導入案内](https://obsproject.com/kb/plugins-guide)が推奨するWindowsの配置はProgramData配下のplugin別ディレクトリ。
管理ツールは検証用マーカー付きportable OBSと、`build`内のProgramData形式の隔離配置だけを対象とする。
後者ではDLLの配置・更新・復元・削除・中断復旧を試すが、実際のProgramDataの権限や通常OBSへの導入検証にはならない。
手順は [ProgramData形式の隔離配置](PROGRAMDATA_SANDBOX.md) を参照する。
通常版の手動ZIP構成と退避・復帰は [導入候補の手順](MANUAL_INSTALL_CANDIDATE.md) にまとめる。
同形式の実ZIPを隔離配置で一巡させる `validate_manual_package.py` は、実ProgramDataへ書き込まない。

- 通常OBSの導入先を利用者が確認できるインストーラーまたは手動ZIP構成を決める。
- 既存DLLの競合、OBS起動中、権限不足、更新・旧版への復帰・削除・中断復旧を、その配置で検証する。
- 他のプラグインとOBS設定を保存し、操作前後の一致を確認する。削除後も録音・設定を暗黙に消さない。
- 現在のDLLはAuthenticode未署名。署名済みを名乗らず、証明書・署名方法と検証を正式候補で決める。

通常OBSへの書き込みはまだ実行しない。公開・署名を含む配布操作は、候補の差分・検証・残件を提示して明示承認を得てから行う。

## 公開前の実音声確認

入力側の補正、OBS受信側の補正、最終録画の欠損を別に判定する。
同じデジタル入力の音量を揃えた試聴と波形・周波数特性、実利用するASIO経路、音声と映像の同期は、それぞれ受入条件を残す。
開始/停止/再起動と長時間運転は、公開する組合せで確認する。過去の別commitの実機結果や合成clock試験で置き換えない。

96→48kHz変換は [専用記録](SINC96_VALIDATION_20261103.md) に残す。既存48kHz DLLの1時間を新DLLへ転用しない。
88.2／176.4／192kHzの追加は [追加rateの記録](EXTENDED_RATES_VALIDATION_20261103.md) に分ける。過去の96kHz DLLの1時間や短い合成試験だけで、新DLLの公開ゲートを通さない。
同じ追加rate DLLの実ASIO 96→48kHzは、後続の1時間回帰で14項目成功した。宣言範囲はその条件に限り、追加3rateの実デバイスや通常OBSへの導入を合格にしない。
実ASIOの合成信号・OTT・OBSの成立と、利用者の発声・音楽の試聴・映像同期は区別する。
