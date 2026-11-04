-- AnyUse map registry v0 — D1 schema for your own Cloudflare D1 database.
-- Identical to server.py's
-- SCHEMA_SQL, which is the executed specification.
CREATE TABLE IF NOT EXISTS contributors (
  key_id TEXT PRIMARY KEY, public_key TEXT NOT NULL, created INTEGER NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS maps (
  id TEXT PRIMARY KEY, content_hash TEXT NOT NULL UNIQUE, target_key TEXT NOT NULL,
  body TEXT NOT NULL, contributor TEXT NOT NULL, created INTEGER NOT NULL,
  installs INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS maps_target ON maps(target_key);
CREATE TABLE IF NOT EXISTS caps (
  map_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
  PRIMARY KEY (map_id, name));
CREATE TABLE IF NOT EXISTS proofs (
  map_id TEXT NOT NULL, capability TEXT NOT NULL, contributor TEXT NOT NULL,
  status TEXT NOT NULL, date TEXT NOT NULL, fresh_inputs INTEGER NOT NULL,
  proof_kind TEXT NOT NULL, anyuse_version TEXT NOT NULL, created INTEGER NOT NULL,
  PRIMARY KEY (map_id, capability, contributor));
CREATE TABLE IF NOT EXISTS writes (contributor TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS writes_by ON writes(contributor, ts);
CREATE TABLE IF NOT EXISTS nonces (
  contributor TEXT NOT NULL, nonce TEXT NOT NULL, ts INTEGER NOT NULL,
  PRIMARY KEY (contributor, nonce));
