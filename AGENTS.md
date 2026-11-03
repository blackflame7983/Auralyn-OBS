# Auralyn OBSソース試作

日本語で説明・コメントする。GPL-3.0-onlyの独自コードを管理する。
今回の通信1.1の本体は隣の `../VSTHost-obs223-rates`、通常のcheckoutは `../VSTHost`。以前の `../VSTHost-obs223` と96kHz候補の証拠を上書きしない。
通信仕様の正本は本体の `docs/protocols/OBS_PCM_V1.md`、全体設計は `docs/plans/OBS_SOURCE_PLUGIN_223.md`。
参照した版は `protocol.lock.json`。`protocol/` は独立ビルド用の固定スナップショットで、独自に仕様変更しない。
通信構造・停止・mute・再接続を変える時は双方の差分と共通fixtureを検証する。
OBSの内部へエンジン・VST・ASIOを持ち込まない。audio_mixの自前処理では確保・解放・I/O・無期限のlock待ちをしない。
他の作業者の変更を保護する。push/PR/公開/通常OBSへの導入は明示承認を得る。
検証: CMakeでビルドしctest。手順はREADME.md。実マイク・OBS録画・1時間実機を自動試験で代替しない。
