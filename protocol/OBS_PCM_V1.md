# Auralyn OBS PCM protocol 1.0（Windows x64試作）

正本はAuralynリポジトリの本書。設計: [Issue #223](../plans/OBS_SOURCE_PLUGIN_223.md)。公開安定APIではない。

## マッピング

名前: `Local\Auralyn.OBS.PoC.v1.<endpoint>`。endpointは1〜48文字のASCII英数字、`_`、`-`だけ。
サイズ196672 bytes = 64 + 16384 * 12。リトルエンディアン、4byte境界、IEEE754 float32 stereo。
全wordを32bit lock-free atomicとして扱う（Rust AtomicU32 / MSVC C++20 atomic_ref<uint32_t>、SeqCst）。
非atomicの構造体コピー・共有ポインタは渡さない。Windows x64以外ではatomic相互運用を再確認する。

## ヘッダー（64bytes）

| word | 値 |
| --- | --- |
| 0 | magic `0x41555043` |
| 1/2 | major=1 / minor=0 |
| 3 | mapping bytes=196672 |
| 4 | capacity frames=16384 |
| 5 | channels=2 |
| 6 | rate（44100 / 48000 / 96000） |
| 7 | producer PID |
| 8 | epoch。quietへの遷移で更新 |
| 9 | state: quiet=0 / running=1 / stopped=2 |
| 10 | 次のwrite frame番号（u32 wrapping） |
| 11〜15 | 0、予約領域 |

初期化後にmagicを最後に公開する。送信者は同名mappingを上書きしない。
受信側は正確なサイズでMapViewOfFileし、全固定値・rateを確認してからslotを読む。
未知のmajor/minorは試作では拒否する。producer終了またはstoppedで接続を閉じる。

## スロット（各12bytes）

frame番号nのslotは `16 + (n & 16383) * 3` wordから始まる。tag、left bits、right bitsの順。未充填tagは `0xffffffff`。
送信はtag=`(n<<1)|1`、左右bits、tag=`n<<1`の順。完了blockだけwrite frameを公開する。
受信は左右の前後でtagを読み、両方 `n<<1` の場合だけ採用する。odd/不一致は無音とする。
上書き中も全wordをatomicで読み、非atomicのデータ競合を避ける。距離はu32 wrapping差、保持はcapacity以内に限る。
NaN/Infは双方で0にする。render前後のstate/epoch変化時は全renderを無音にし、最新の目標待ち量から再充填する。
write位置が100ms進まない場合は無音とし、過去音を繰り返さない。

## 共通fixture

`fixtures/obs-pcm-v1.json` を双方の実装で読み、サイズ・offset・tag・float bitsを確認する。
プラグインに同一fixtureと仕様スナップショットを同梱し、SHA-256をprotocol.lock.jsonへ記録する。
正本を増やす複製ではなく、独立ビルド用の固定入力。仕様変更時は双方を同時に検証する。
