#!/bin/sh
# Pack everything the public service needs into one tarball; unpack it on the server and
# `docker compose up`.
#
#   sh okx/service/pack.sh            → okx-service-<date>.tar.gz (in the repository root)
#
# ★ It packs only: okx/engine, okx/agent (without out/), okx/sql, okx/service (without .env),
#   dune/lib and .dockerignore. No credentials, no collected data, no archives. After packing it
#   scans the result and fails outright if anything looks like a key.
set -eu
cd "$(dirname "$0")/../.."
out="okx-service-$(date -u +%Y%m%d).tar.gz"
tar --exclude='okx/agent/out' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='okx/service/.env' --exclude='okx/service/pack.sh' \
    -czf "$out" .dockerignore okx/engine okx/agent okx/sql okx/service dune/lib
# Scan: no .env / .db / .pem filenames, and no common key prefixes in the contents
if tar -tzf "$out" | grep -E '(^|/)\.env$|\.db$|\.pem$|/out/' ; then
    echo "FAILED: the archive contains files it should not (listed above)"; rm -f "$out"; exit 1
fi
if tar -xzOf "$out" | grep -aE 'sk-or-v1-[A-Za-z0-9]{20}|sk-ant-api|DUNE_API_KEY=[A-Za-z0-9]{10}|OKX_SECRET_KEY=[A-Za-z0-9]{10}' ; then
    echo "FAILED: something that looks like a credential was found in the archive"; rm -f "$out"; exit 1
fi
echo "OK  $out  $(du -h "$out" | cut -f1)  $(tar -tzf "$out" | wc -l) files, no credentials found"
