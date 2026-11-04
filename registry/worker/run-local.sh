#!/bin/sh
# Local-only Worker check. Run INSIDE a throwaway container (glibc image, e.g.
# node:24-bookworm-slim) with wrangler installed under /tool by
#   npm install --before=<14 days ago> --ignore-scripts wrangler
# and this registry folder mounted at /work/registry. Never deploys.
set -e
export PATH=/tool/node_modules/.bin:$PATH WRANGLER_SEND_METRICS=false CI=1
cd /work/registry/worker
sed -i 's/OWNER-MUST-CREATE-AND-FILL-IN/00000000-0000-0000-0000-000000000000/' wrangler.toml
smoke_dir=$(mktemp -d)
smoke_keys="$smoke_dir/keys.json"
node smoke.mjs --generate-keys "$smoke_keys"
alice_public=$(node -e 'const fs=require("fs"); process.stdout.write(JSON.parse(fs.readFileSync(process.argv[1],"utf8")).alice.public_key)' "$smoke_keys")
bob_public=$(node -e 'const fs=require("fs"); process.stdout.write(JSON.parse(fs.readFileSync(process.argv[1],"utf8")).bob.public_key)' "$smoke_keys")
wrangler d1 execute anyuse-map-registry --local --file=schema.sql >/tmp/d1.log 2>&1 || { tail -20 /tmp/d1.log; exit 1; }
wrangler d1 execute anyuse-map-registry --local --command "INSERT INTO contributors(key_id, public_key, created) VALUES ('alice','$alice_public',0),('bob','$bob_public',0)" >>/tmp/d1.log 2>&1 || { tail -20 /tmp/d1.log; exit 1; }
(wrangler dev --local --ip 127.0.0.1 --port 8787 >/tmp/dev.log 2>&1 &)
i=0; until node -e "fetch('http://127.0.0.1:8787/v0/search?q=x').then(()=>process.exit(0)).catch(()=>process.exit(1))"; do i=$((i+1)); [ $i -gt 60 ] && { tail -30 /tmp/dev.log; exit 1; }; sleep 1; done
node vectors.mjs
ANYUSE_REGISTRY_SMOKE_KEYS="$smoke_keys" node smoke.mjs || { echo '--- dev log'; tail -30 /tmp/dev.log; exit 1; }
