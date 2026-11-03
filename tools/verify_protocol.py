"""正本は増やさず、固定スナップショットと対応する入力の同一性を確認する。"""
import argparse
import hashlib
import json
from pathlib import Path


def digest(path):
    # Git checkoutの改行変換に影響されないUTF-8/LFのハッシュ。
    return hashlib.sha256(path.read_text(encoding='utf-8-sig').encode('utf-8')).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--auralyn', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    lock = json.loads((root / 'protocol.lock.json').read_text(encoding='utf-8'))
    pairs = [('protocol/OBS_PCM_V1.md', 'snapshotSha256', 'docs/protocols/OBS_PCM_V1.md'),
             ('protocol/obs-pcm-v1.json', 'fixtureSha256', 'docs/protocols/fixtures/obs-pcm-v1.json')]
    for local, key, canonical in pairs:
        if digest(root / local) != lock[key]:
            raise RuntimeError(f'Snapshot differs from protocol.lock.json: {local}')
        if args.auralyn and digest(args.auralyn / canonical) != lock[key]:
            raise RuntimeError(f'Auralyn canonical source differs: {canonical}')
    print(f'Protocol {lock["major"]}.{lock["minor"]} snapshots and fixtures match' + (' the Auralyn canonical source.' if args.auralyn else '.'))


if __name__ == '__main__':
    main()
