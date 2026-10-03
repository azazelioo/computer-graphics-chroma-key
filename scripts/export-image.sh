#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
platform="${1:-linux/amd64}"
case "$platform" in linux/amd64|linux/arm64) ;; *) echo 'Use linux/amd64 or linux/arm64' >&2; exit 2;; esac
arch="${platform#linux/}"
mkdir -p outputs
docker buildx build --platform "$platform" --load -t kg-chroma-key:submission-20261003 .
docker image save kg-chroma-key:submission-20261003 | gzip > "outputs/chroma-key-${arch}.tar.gz"
python3 - "$arch" <<'PY'
from pathlib import Path
import hashlib,sys
p=Path('outputs')/f'chroma-key-{sys.argv[1]}.tar.gz'
h=hashlib.file_digest(p.open('rb'),'sha256').hexdigest()
(p.with_name(p.name+'.sha256')).write_text(f'{h}  {p.name}\n')
print(p)
PY
