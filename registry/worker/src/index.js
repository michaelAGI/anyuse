// AnyUse map registry v0 — Cloudflare Worker (module syntax) over D1.
//
// Not deployed by default: deploying needs a Cloudflare account, wrangler and a
// D1 database of your own. This file implements the same contract as the
// executed Python reference server (registry/server.py): same routes, same
// validation (./validate.js reads ../../contract.json), same status codes,
// same "verified" rule. Where they differ, the Python server is the
// specification.
//
// Routes: POST /v0/maps, GET /v0/maps?target=, GET /v0/maps/{id},
//         POST /v0/maps/{id}/proofs, GET /v0/search?q=
// Bindings (wrangler.toml): DB (D1).

import {
  contract, full, RegistryError, validateEntry, proofReportErrors, verifySignature, canonicalJSON,
} from "./validate.js";

const HEADERS = {
  "content-type": "application/json",
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
};

function json(status, obj) {
  return new Response(JSON.stringify(obj), { status, headers: HEADERS });
}

function errorResponse(e) {
  const status = contract.errors[e.code] || 400;
  return json(status, { error: e.code, detail: String(e.detail || "").slice(0, 600) });
}

function now() { return Math.floor(Date.now() / 1000); }

function targetKey(t) { return t.kind === "web" ? t.origin : "app:" + t.app; }

async function readBody(request) {
  const cap = contract.limits.max_body_bytes;
  const declared = Number(request.headers.get("content-length") || "0");
  if (!Number.isFinite(declared) || declared < 0 || declared > cap)
    throw new RegistryError("too_large", `body over ${cap} bytes`);
  const buf = new Uint8Array(await request.arrayBuffer());
  if (buf.length > cap) throw new RegistryError("too_large", `body over ${cap} bytes`);
  return buf;
}

function parseJSON(bytes) {
  try { return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)); }
  catch { throw new RegistryError("bad_json", "body is not JSON"); }
}

async function authenticate(env, request, url, body) {
  const a = contract.auth;
  const key = request.headers.get(a.header_key) || "";
  const ts = request.headers.get(a.header_timestamp) || "";
  const nonce = request.headers.get(a.header_nonce) || "";
  const sig = request.headers.get(a.header_signature) || "";
  const alg = request.headers.get(a.header_alg) || "";
  if (!(alg === a.alg && full("key_id", key) && full("nonce", nonce) && /^[0-9]+$/.test(ts)))
    throw new RegistryError("unauthorized", "missing or malformed auth headers");
  const t = now();
  if (Math.abs(t - Number(ts)) > a.max_skew_s) throw new RegistryError("unauthorized", "timestamp outside the window");
  const row = await env.DB.prepare(
    "SELECT public_key, disabled FROM contributors WHERE key_id=?").bind(key).first();
  if (!row || row.disabled) throw new RegistryError("unauthorized", "unknown key");
  const pathWithQuery = url.pathname + url.search;
  if (!(await verifySignature(row.public_key, sig, request.method, pathWithQuery, ts, nonce, body)))
    throw new RegistryError("unauthorized", "bad signature");
  await env.DB.prepare("DELETE FROM nonces WHERE ts < ?").bind(t - 2 * a.max_skew_s).run();
  try {
    await env.DB.prepare("INSERT INTO nonces(contributor, nonce, ts) VALUES (?,?,?)").bind(key, nonce, t).run();
  } catch {
    throw new RegistryError("replay", "nonce already used");
  }
  return key;
}

async function rate(env, key) {
  const t = now();
  const cap = contract.rate_limits.writes_per_key_per_hour;
  await env.DB.prepare("DELETE FROM writes WHERE ts < ?").bind(t - 3600).run();
  const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM writes WHERE contributor=? AND ts >= ?")
    .bind(key, t - 3600).first();
  if ((row?.n || 0) >= cap) throw new RegistryError("rate_limited", "write limit for this key");
  await env.DB.prepare("INSERT INTO writes(contributor, ts) VALUES (?,?)").bind(key, t).run();
}

async function meta(env, id) {
  const t = contract.trust;
  const { results } = await env.DB.prepare(
    "SELECT capability, contributor, status, date, fresh_inputs, proof_kind, anyuse_version " +
    "FROM proofs WHERE map_id=? ORDER BY capability, contributor").bind(id).all();
  const by = new Map();
  for (const r of results) {
    if (r.status !== t.verified_counts_status) continue;
    if (!by.has(r.capability)) by.set(r.capability, new Set());
    by.get(r.capability).add(r.contributor);
  }
  const verified = [...by.entries()].filter(([, who]) => who.size >= t.verified_min_distinct_contributors)
    .map(([c]) => c).sort();
  const m = await env.DB.prepare("SELECT contributor, created, installs FROM maps WHERE id=?").bind(id).first();
  return {
    contributor: m.contributor,
    created_day: new Date(m.created * 1000).toISOString().slice(0, 10),
    installs: m.installs, proof_reports: results, verified_capabilities: verified,
    verified: verified.length > 0,
    trust_note: "metadata only: clients land every capability as unproved",
  };
}

async function summaries(env, ids) {
  const out = [];
  for (const id of ids) {
    const row = await env.DB.prepare("SELECT target_key FROM maps WHERE id=?").bind(id).first();
    const { results: caps } = await env.DB.prepare("SELECT name, kind FROM caps WHERE map_id=? ORDER BY name")
      .bind(id).all();
    const m = await meta(env, id);
    out.push({ id, target: row.target_key, capabilities: caps, verified: m.verified,
      verified_capabilities: m.verified_capabilities, installs: m.installs, reports: m.proof_reports.length });
  }
  out.sort((a, b) => (a.verified === b.verified ? 0 : a.verified ? -1 : 1) ||
    b.reports - a.reports || b.installs - a.installs || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  return out.slice(0, contract.limits.max_list_results);
}

async function route(request, env) {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method;
  const segs = path.split("/");

  if (path === "/v0/maps" && method === "GET") {
    const all = url.searchParams.getAll("target");
    if (all.length !== 1) throw new RegistryError("bad_query", "exactly one target");
    const target = all[0];
    if (!(full("origin", target) || (target.startsWith("app:") && full("app", target.slice(4)))))
      throw new RegistryError("bad_query", "target must be https://host or app:<name>");
    const { results } = await env.DB.prepare(
      "SELECT id FROM maps WHERE target_key=? ORDER BY created DESC LIMIT 500").bind(target).all();
    return json(200, { maps: await summaries(env, results.map((r) => r.id)) });
  }
  if (path === "/v0/search" && method === "GET") {
    const all = url.searchParams.getAll("q");
    if (all.length !== 1) throw new RegistryError("bad_query", "exactly one q");
    const q = all[0];
    if (!q || q.length > contract.limits.max_search_query_chars || !full("printable_ascii", q))
      throw new RegistryError("bad_query", "q must be 1..64 printable ASCII characters");
    const like = "%" + q.toLowerCase().replace(/\\/g, "\\\\").replace(/%/g, "\\%").replace(/_/g, "\\_") + "%";
    const { results } = await env.DB.prepare(
      "SELECT DISTINCT m.id FROM maps m LEFT JOIN caps c ON c.map_id = m.id " +
      "WHERE lower(m.target_key) LIKE ?1 ESCAPE '\\' OR lower(c.name) LIKE ?1 ESCAPE '\\' " +
      "OR lower(c.kind) LIKE ?1 ESCAPE '\\' ORDER BY m.created DESC LIMIT 500").bind(like).all();
    return json(200, { maps: await summaries(env, results.map((r) => r.id)) });
  }
  if (path === "/v0/maps" && method === "POST") {
    const body = await readBody(request);
    const key = await authenticate(env, request, url, body);
    const entry = parseJSON(body);
    await rate(env, key);  // same order as server.py: auth, JSON, rate, validate
    await validateEntry(entry);
    const id = entry.content_hash.slice(0, 32);
    const exists = await env.DB.prepare("SELECT 1 AS x FROM maps WHERE id=?").bind(id).first();
    if (exists) return json(200, { id, content_hash: entry.content_hash });
    const stmts = [env.DB.prepare(
      "INSERT INTO maps(id, content_hash, target_key, body, contributor, created) VALUES (?,?,?,?,?,?)")
      .bind(id, entry.content_hash, targetKey(entry.target), canonicalJSON(entry), key, now())];
    for (const c of entry.capabilities)
      stmts.push(env.DB.prepare("INSERT INTO caps(map_id, name, kind) VALUES (?,?,?)").bind(id, c.name, c.kind));
    await env.DB.batch(stmts);
    return json(201, { id, content_hash: entry.content_hash });
  }
  if (segs.length === 4 && segs[1] === "v0" && segs[2] === "maps" && method === "GET") {
    const id = segs[3];
    if (!full("map_id", id)) throw new RegistryError("not_found", "");
    const row = await env.DB.prepare("SELECT body FROM maps WHERE id=?").bind(id).first();
    if (!row) throw new RegistryError("not_found", "");
    await env.DB.prepare("UPDATE maps SET installs = installs + 1 WHERE id=?").bind(id).run();
    return json(200, { id, entry: JSON.parse(row.body), meta: await meta(env, id) });
  }
  if (segs.length === 5 && segs[1] === "v0" && segs[2] === "maps" && segs[4] === "proofs" && method === "POST") {
    const body = await readBody(request);
    const key = await authenticate(env, request, url, body);
    const report = parseJSON(body);
    await rate(env, key);
    const id = segs[3];
    const row = full("map_id", id)
      ? await env.DB.prepare("SELECT body FROM maps WHERE id=?").bind(id).first() : null;
    if (!row) throw new RegistryError("not_found", "");
    const names = JSON.parse(row.body).capabilities.map((c) => c.name);
    const errs = proofReportErrors(report, names);
    if (errs.length) throw new RegistryError("schema", errs.slice(0, 8).join("; "));
    await env.DB.prepare(
      "INSERT OR REPLACE INTO proofs(map_id, capability, contributor, status, date, fresh_inputs, " +
      "proof_kind, anyuse_version, created) VALUES (?,?,?,?,?,?,?,?,?)")
      .bind(id, report.capability, key, report.status, report.date, report.fresh_inputs,
        report.proof_kind, report.anyuse_version, now()).run();
    return json(201, await meta(env, id));
  }
  const known = path === "/v0/maps" || path === "/v0/search" ||
    ((segs.length === 4 || segs.length === 5) && segs[1] === "v0" && segs[2] === "maps");
  throw new RegistryError(known ? "method_not_allowed" : "not_found", "");
}

export default {
  async fetch(request, env) {
    try {
      return await route(request, env);
    } catch (e) {
      if (e instanceof RegistryError) return errorResponse(e);
      return json(500, { error: "internal" });  // never leak the request or a stack
    }
  },
};
