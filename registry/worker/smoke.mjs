// End-to-end smoke test for the Worker under `wrangler dev --local`
// (workerd + local D1), run inside a throwaway Apple container: see
// registry/worker/run-local.sh. Never points at a deployed registry.
import { readFile, writeFile } from "node:fs/promises";
import vectors from "../contract_vectors.json" with { type: "json" };
import { stringToSign } from "./src/validate.js";

const enc = new TextEncoder();
const b64url = (bytes) => Buffer.from(bytes).toString("base64url");
const unb64url = (text) => new Uint8Array(Buffer.from(text, "base64url"));

async function generateKeys(path) {
  const out = {};
  for (const name of ["alice", "bob"]) {
    const pair = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
    out[name] = {
      public_key: b64url(await crypto.subtle.exportKey("raw", pair.publicKey)),
      private_pkcs8: b64url(await crypto.subtle.exportKey("pkcs8", pair.privateKey)),
    };
  }
  await writeFile(path, JSON.stringify(out), { encoding: "utf8", mode: 0o600, flag: "wx" });
}

if (process.argv[2] === "--generate-keys") {
  if (!process.argv[3]) throw new Error("--generate-keys needs an output path");
  await generateKeys(process.argv[3]);
  console.log("generated ephemeral Ed25519 smoke keys");
  process.exit(0);
}

const keyFile = process.env.ANYUSE_REGISTRY_SMOKE_KEYS;
if (!keyFile) throw new Error("ANYUSE_REGISTRY_SMOKE_KEYS is required");
const keyData = JSON.parse(await readFile(keyFile, "utf8"));
const signers = {};
for (const name of ["alice", "bob"]) {
  signers[name] = await crypto.subtle.importKey("pkcs8", unb64url(keyData[name].private_pkcs8),
    { name: "Ed25519" }, false, ["sign"]);
}

async function sign(privateKey, method, path, timestamp, nonce, body) {
  const bytes = await stringToSign(method, path, timestamp, nonce, body);
  return b64url(await crypto.subtle.sign({ name: "Ed25519" }, privateKey, bytes));
}

const BASE = "http://127.0.0.1:8787";
let fail = 0;
const check = (name, cond, extra = "") => { console.log((cond ? "PASS " : "FAIL ") + name, extra); if (!cond) fail++; };
async function call(method, path, bodyObj, key = "alice", tamper = false) {
  const body = bodyObj === undefined ? new Uint8Array() : enc.encode(JSON.stringify(bodyObj));
  const ts = String(Math.floor(Date.now() / 1000)), nonce = crypto.randomUUID().replace(/-/g, "");
  const headers = { "content-type": "application/json" };
  if (key) {
    let sig = await sign(signers[key] || signers.alice, method, path, ts, nonce, body);
    if (tamper) sig = (sig[0] === "A" ? "B" : "A") + sig.slice(1);
    Object.assign(headers, { "X-AnyUse-Sig-Alg": "ed25519", "X-AnyUse-Key": key,
      "X-AnyUse-Timestamp": ts, "X-AnyUse-Nonce": nonce, "X-AnyUse-Signature": sig });
  }
  const r = await fetch(BASE + path, { method, headers, body: method === "GET" ? undefined : body });
  let j = null; try { j = await r.json(); } catch {}
  return { status: r.status, j };
}

const ok = vectors.vectors.find((v) => v.name === "ok_web").entry;
const bad = vectors.vectors.filter((v) => v.expect !== "ok");
let r = await call("POST", "/v0/maps", ok);
check("push valid map", r.status === 201 || r.status === 200, JSON.stringify(r.j).slice(0, 200));
const id = r.j && (r.j.id || r.j.map_id);
r = await call("POST", "/v0/maps", ok, "alice", true);
check("tampered signature refused", r.status === 401 || r.status === 403, r.status);
r = await call("POST", "/v0/maps", ok, "mallory");
check("unknown contributor refused", r.status === 401 || r.status === 403, r.status);
let refused = 0; const n = Math.min(12, bad.length);
for (const v of bad.slice(0, n)) { const x = await call("POST", "/v0/maps", v.entry, "bob"); if (x.status >= 400 && x.status < 500) refused++; }
check(`invalid entries refused ${refused}/${n}`, refused === n);
const cap = ok.capabilities[0].name;
const rep = { capability: cap, status: "proved", date: "2026-11-03", fresh_inputs: 2, proof_kind: "learn", anyuse_version: "0.4.0" };
r = await call("POST", `/v0/maps/${id}/proofs`, rep);
check("proof from contributor 1 accepted", r.status === 201);
check("one contributor: not verified", r.j && r.j.verified === false && r.j.verified_capabilities.length === 0);
r = await call("POST", `/v0/maps/${id}/proofs`, rep);
check("same contributor twice: still not verified", r.status === 201 && r.j.verified === false);
r = await call("POST", `/v0/maps/${id}/proofs`, rep, "bob");
check("proof from contributor 2 accepted", r.status === 201);
check("two distinct contributors: verified", r.j.verified === true && r.j.verified_capabilities.includes(cap));
check("trust note says clients land it unproved", /unproved/.test(r.j.trust_note || ""));
r = await call("GET", `/v0/maps/${id}`, undefined, null);
check("fetch map", r.status === 200, JSON.stringify(r.j).slice(0, 160));
r = await call("GET", `/v0/search?q=${encodeURIComponent(cap)}`, undefined, null);
check("search", r.status === 200, JSON.stringify(r.j).slice(0, 160));
console.log(fail ? `${fail} FAILED` : "SMOKE ALL PASS");
process.exit(fail ? 1 : 0);
