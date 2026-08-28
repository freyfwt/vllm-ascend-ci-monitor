#!/usr/bin/env python3
import base64, gzip
from pathlib import Path

FILES = {
    Path('.update/collect.gz.b64'): Path('scripts/collect.py'),
    Path('.update/index.gz.b64'): Path('index.html'),
    Path('.update/readme.gz.b64'): Path('README.md'),
}
for source, target in FILES.items():
    payload = ''.join(source.read_text().split())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(gzip.decompress(base64.b64decode(payload)))
    print(f'updated {target}')
