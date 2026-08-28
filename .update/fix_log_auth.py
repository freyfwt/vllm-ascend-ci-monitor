#!/usr/bin/env python3
from pathlib import Path
p = Path('scripts/collect.py')
s = p.read_text()
old = "return self.request(ROOT + path, fallback_404=False).decode('utf-8', errors='replace')"
new = "return self.request(ROOT + path, use_auth=False, fallback_404=False).decode('utf-8', errors='replace')"
if old not in s:
    raise SystemExit('target text() implementation not found')
p.write_text(s.replace(old, new, 1))
print('job logs now use anonymous public access without demoting metadata auth')
