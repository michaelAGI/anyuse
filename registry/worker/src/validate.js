// AnyUse map registry v0 — contract validation for the Cloudflare Worker.
//
// It is a line-for-line port of registry/schema.py and reads the SAME
// registry/contract.json. The shared vectors in registry/contract_vectors.json
// are the parity check (worker/vectors.mjs).

import contract from "../../contract.json" with { type: "json" };

export { contract };

const P = {};
for (const [k, v] of Object.entries(contract.patterns)) P[k] = new RegExp("^(?:" + v + ")$");

export function full(name, value) {
  return typeof value === "string" && P[name].test(value);
}

export class RegistryError extends Error {
  constructor(code, detail = "") {
    super(detail ? `${code}: ${detail}` : code);
    this.code = code;
    this.detail = detail;
  }
}

// ── canonical JSON and hash ────────────────────────────────────────────────
// Sorted keys, no whitespace. Every valid entry is printable ASCII with small
// integers, so this equals Python's json.dumps(sort_keys=True,
// separators=(",", ":"), ensure_ascii=True).
export function canonicalJSON(v) {
  if (v === null || typeof v === "boolean" || typeof v === "number") return JSON.stringify(v);
  if (typeof v === "string") return JSON.stringify(v).replace(/[\u007f-￿]/g,
    (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
  if (Array.isArray(v)) return "[" + v.map(canonicalJSON).join(",") + "]";
  const keys = Object.keys(v).sort();
  return "{" + keys.map((k) => canonicalJSON(k) + ":" + canonicalJSON(v[k])).join(",") + "}";
}

const enc = new TextEncoder();

export async function sha256hex(bytes) {
  const d = await crypto.subtle.digest("SHA-256", typeof bytes === "string" ? enc.encode(bytes) : bytes);
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function contentHash(entry) {
  const body = {};
  for (const [k, v] of Object.entries(entry)) if (k !== "content_hash") body[k] = v;
  return sha256hex(canonicalJSON(body));
}

// ── the walk ───────────────────────────────────────────────────────────────
const FORBIDDEN_KEYS = new Set(
  [...contract.never_travels_snapshot, ...contract.forbidden_keys_extra].map((k) => k.toLowerCase()));

function walk(obj, where, errors, depth = 0) {
  if (depth > 6) { errors.push(`${where}: nested too deep`); return; }
  if (Array.isArray(obj)) { obj.forEach((v, i) => walk(v, `${where}[${i}]`, errors, depth + 1)); return; }
  if (obj !== null && typeof obj === "object") {
    for (const [k, v] of Object.entries(obj)) {
      if (!full("printable_ascii", k)) { errors.push(`${where}: non-ASCII key`); continue; }
      if (FORBIDDEN_KEYS.has(k.toLowerCase())) errors.push(`${where}.${k}: key never travels`);
      walk(v, `${where}.${k}`, errors, depth + 1);
    }
    return;
  }
  if (typeof obj === "string") {
    if (obj.length > contract.limits.max_string_chars) errors.push(`${where}: string too long`);
    if (!full("printable_ascii", obj)) errors.push(`${where}: not printable ASCII`);
    const low = obj.toLowerCase();
    for (const s of contract.forbidden_substrings) {
      if (low.includes(s)) { errors.push(`${where}: contains a forbidden pattern`); break; }
    }
    return;
  }
  if (typeof obj === "boolean" || obj === null) return;
  if (typeof obj === "number") {
    if (!Number.isInteger(obj) || obj < 0 || obj > 1000000) errors.push(`${where}: integer out of range`);
    return;
  }
  errors.push(`${where}: unsupported value type`);
}

function isObj(o) { return o !== null && typeof o === "object" && !Array.isArray(o); }

function keys(obj, where, required, optional, errors) {
  if (!isObj(obj)) { errors.push(`${where}: must be an object`); return false; }
  const have = new Set(Object.keys(obj));
  const missing = [...required].filter((k) => !have.has(k)).sort();
  for (const k of missing) errors.push(`${where}.${k}: missing`);
  for (const k of [...have].filter((k) => !required.includes(k) && !optional.includes(k)).sort())
    errors.push(`${where}.${k}: unknown field`);
  return missing.length === 0;
}

function isInt(v) { return typeof v === "number" && Number.isInteger(v); }

// ── templates ──────────────────────────────────────────────────────────────
const PH = /^\{([a-z][a-z0-9_]{0,31})\}$/;

export function parseTemplate(template, origin) {
  const errs = [], segs = [], pairs = [];
  if (typeof template !== "string" || template.length > contract.limits.max_template_chars)
    return { segs, pairs, errs: ["template missing or too long"] };
  if (!template.startsWith(origin + "/"))
    return { segs, pairs, errs: ["template does not start with the entry origin"] };
  const rest = template.slice(origin.length + 1);
  if (rest.includes("#")) return { segs, pairs, errs: ["template has a fragment"] };
  let path = rest, query = null;
  const qi = rest.indexOf("?");
  if (qi >= 0) {
    path = rest.slice(0, qi); query = rest.slice(qi + 1);
    if (query === "") errs.push("empty query");
  }
  if (path) {
    const parts = path.split("/");
    parts.forEach((part, i) => {
      const last = i === parts.length - 1;
      if (part === "") { if (!last) errs.push("empty path segment"); return; }
      const m = PH.exec(part);
      if (m) segs.push(["ph", m[1]]);
      else if (part === "." || part === ".." || !full("path_literal", part)) errs.push("path segment not allowed");
      else segs.push(["lit", part]);
    });
  }
  if (query) {
    const seen = new Set();
    const forbidden = new Set(contract.forbidden_query_keys.map((k) => k.toLowerCase()));
    for (const raw of query.split("&")) {
      if (raw.split("=").length !== 2) { errs.push("query pair malformed"); continue; }
      const [k, v] = raw.split("=");
      if (!full("query_key", k)) { errs.push("query key not allowed"); continue; }
      if (forbidden.has(k.toLowerCase())) errs.push("query key is a redirect or credential key");
      if (seen.has(k)) errs.push("duplicate query key");
      seen.add(k);
      const m = PH.exec(v);
      if (m) pairs.push([k, "ph", m[1]]);
      else if (full("fixed_value", v)) pairs.push([k, "lit", v]);
      else errs.push("fixed query value not allowed");
    }
  }
  return { segs, pairs, errs };
}

// ── entry ──────────────────────────────────────────────────────────────────
function validateProof(p, where, errors) {
  if (!keys(p, where, ["status", "date", "fresh_inputs", "proof_kind", "anyuse_version"], [], errors)) return;
  const e = contract.enums;
  if (!e.proof_status.includes(p.status)) errors.push(`${where}.status: not allowed`);
  if (!full("date", p.date)) errors.push(`${where}.date: must be YYYY-MM-DD`);
  const fi = p.fresh_inputs;
  if (!isInt(fi) || fi < 0 || fi > contract.limits.max_fresh_inputs)
    errors.push(`${where}.fresh_inputs: must be a small count`);
  if (!e.proof_kind.includes(p.proof_kind)) errors.push(`${where}.proof_kind: not allowed`);
  if (!full("anyuse_version", p.anyuse_version)) errors.push(`${where}.anyuse_version: not a version`);
  if (p.status === "proved" && (p.proof_kind === "none" || !(fi >= 1)))
    errors.push(`${where}: a proved status needs a proof kind and >= 1 fresh input`);
}

function validateParams(params, where, desktop, errors) {
  const out = new Map();
  if (!Array.isArray(params) || params.length > contract.limits.max_params) {
    errors.push(`${where}: must be a list of at most ${contract.limits.max_params}`);
    return out;
  }
  params.forEach((p, i) => {
    const w = `${where}[${i}]`;
    if (!keys(p, w, ["name", "type", "where"], ["key"], errors)) return;
    if (!full("param_name", p.name)) errors.push(`${w}.name: not allowed`);
    if (!contract.enums.param_type.includes(p.type)) errors.push(`${w}.type: not allowed`);
    const allowed = contract.enums[desktop ? "param_where_desktop" : "param_where_web"];
    if (!allowed.includes(p.where)) errors.push(`${w}.where: not allowed`);
    if (p.where === "query") {
      if (!full("query_key", p.key)) errors.push(`${w}.key: a query parameter needs its query key`);
    } else if ("key" in p) errors.push(`${w}.key: only a query parameter has a key`);
    if (out.has(p.name)) errors.push(`${w}.name: duplicate`);
    out.set(p.name, p);
  });
  return out;
}

function validateWebCap(cap, where, origin, errors) {
  if (!contract.enums.web_kinds.includes(cap.kind)) errors.push(`${where}.kind: not a web kind`);
  if ("operator" in cap || !("request" in cap)) {
    errors.push(`${where}: a web capability has a request and no operator`);
    return;
  }
  const params = validateParams(cap.params, `${where}.params`, false, errors);
  const req = cap.request;
  if (!keys(req, `${where}.request`, ["method", "template"], [], errors)) return;
  if (!contract.enums.request_method.includes(req.method)) errors.push(`${where}.request.method: not allowed`);
  const { segs, pairs, errs } = parseTemplate(req.template, origin);
  for (const e of errs) errors.push(`${where}.request.template: ${e}`);
  if (errs.length) return;
  const used = new Map();
  for (const [kind, text] of segs) {
    if (kind !== "ph") continue;
    used.set(text, (used.get(text) || 0) + 1);
    const p = params.get(text);
    if (p && p.where !== "path") errors.push(`${where}.request.template: placeholder location differs from its param`);
  }
  for (const [key, kind, text] of pairs) {
    if (kind !== "ph") continue;
    used.set(text, (used.get(text) || 0) + 1);
    const p = params.get(text);
    if (p && (p.where !== "query" || p.key !== key))
      errors.push(`${where}.request.template: placeholder key differs from its param`);
  }
  const same = used.size === params.size && [...used.keys()].every((k) => params.has(k));
  if (!same || [...used.values()].some((n) => n !== 1))
    errors.push(`${where}.request.template: every param exactly once, no undeclared placeholder`);
}

function validateDesktopCap(cap, where, errors) {
  const e = contract.enums;
  if (!e.desktop_kinds.includes(cap.kind)) { errors.push(`${where}.kind: not a desktop kind`); return; }
  if ("request" in cap || !("operator" in cap)) {
    errors.push(`${where}: a desktop capability has an operator and no request`);
    return;
  }
  const params = validateParams(cap.params, `${where}.params`, true, errors);
  const op = cap.operator, w = `${where}.operator`;
  if (cap.kind === "desktop_class") {
    if (!keys(op, w, ["class"], ["subtype"], errors)) return;
    if (!e.desktop_class.includes(op.class)) errors.push(`${w}.class: not allowed`);
    if ("subtype" in op && !e.desktop_subtype.includes(op.subtype)) errors.push(`${w}.subtype: not allowed`);
    return;
  }
  if (!keys(op, w, ["idnames", "binds"], [], errors)) return;
  const ids = op.idnames;
  if (!Array.isArray(ids) || ids.length < 1 || ids.length > contract.limits.max_idnames ||
      !ids.every((x) => full("idname", x))) {
    errors.push(`${w}.idnames: 1..${contract.limits.max_idnames} operator idnames`);
    return;
  }
  const binds = op.binds;
  if (!Array.isArray(binds) || binds.length > contract.limits.max_binds) {
    errors.push(`${w}.binds: must be a short list`);
    return;
  }
  const bound = new Map();
  binds.forEach((b, i) => {
    const bw = `${w}.binds[${i}]`;
    if (!keys(b, bw, ["param", "op", "kwarg"], [], errors)) return;
    if (!params.has(b.param)) errors.push(`${bw}.param: not a declared param`);
    if (!isInt(b.op) || b.op < 0 || b.op >= ids.length) errors.push(`${bw}.op: not an operator index`);
    if (!full("kwarg", b.kwarg)) errors.push(`${bw}.kwarg: not allowed`);
    bound.set(b.param, (bound.get(b.param) || 0) + 1);
  });
  const same = bound.size === params.size && [...bound.keys()].every((k) => params.has(k));
  if (!same || [...bound.values()].some((n) => n !== 1)) errors.push(`${w}.binds: every param bound exactly once`);
}

export async function entryErrors(entry, checkHash = true) {
  const errors = [];
  if (!keys(entry, "entry", ["schema", "target", "capabilities", "content_hash"], [], errors)) return errors;
  walk(entry, "entry", errors);
  if (entry.schema !== contract.schema) errors.push("entry.schema: unknown schema version");
  const t = entry.target;
  let origin = "", desktop = false;
  if (isObj(t) && t.kind === "web") {
    if (keys(t, "entry.target", ["kind", "origin"], [], errors)) {
      origin = t.origin;
      if (!full("origin", origin)) errors.push("entry.target.origin: must be https://<public dns name>");
    }
  } else if (isObj(t) && t.kind === "desktop") {
    desktop = true;
    if (keys(t, "entry.target", ["kind", "app", "app_id", "versions"], [], errors)) {
      if (!full("app", t.app)) errors.push("entry.target.app: not allowed");
      if (!full("app_id", t.app_id)) errors.push("entry.target.app_id: not a reverse-DNS id");
      const v = t.versions;
      if (keys(v, "entry.target.versions", ["min"], ["max"], errors)) {
        if (!full("version", v.min) || ("max" in v && !full("version", v.max)))
          errors.push("entry.target.versions: digits and dots only");
      }
    }
  } else errors.push("entry.target.kind: must be web or desktop");
  let caps = entry.capabilities;
  if (!Array.isArray(caps) || caps.length < 1 || caps.length > contract.limits.max_capabilities) {
    errors.push(`entry.capabilities: 1..${contract.limits.max_capabilities} capabilities`);
    caps = [];
  }
  const names = new Set();
  caps.forEach((cap, i) => {
    const w = `entry.capabilities[${i}]`;
    if (!keys(cap, w, ["name", "kind", "params", "proof"], ["request", "operator"], errors)) return;
    if (!full("capability_name", cap.name)) errors.push(`${w}.name: not allowed`);
    if (names.has(cap.name)) errors.push(`${w}.name: duplicate`);
    names.add(cap.name);
    if (!contract.enums.capability_kind.includes(cap.kind)) { errors.push(`${w}.kind: not allowed`); return; }
    if (desktop) validateDesktopCap(cap, w, errors);
    else if (origin) validateWebCap(cap, w, origin, errors);
    validateProof(cap.proof, `${w}.proof`, errors);
  });
  if (!full("content_hash", entry.content_hash)) errors.push("entry.content_hash: not a sha256 hex digest");
  else if (checkHash && errors.length === 0 && (await contentHash(entry)) !== entry.content_hash)
    errors.push("entry.content_hash: does not match the canonical content");
  return errors;
}

export const HASH_MISMATCH = "entry.content_hash: does not match the canonical content";

export async function validateEntry(entry) {
  const errs = await entryErrors(entry);
  if (errs.length) {
    const code = errs.length === 1 && errs[0] === HASH_MISMATCH ? "hash_mismatch" : "schema";
    throw new RegistryError(code, errs.slice(0, 8).join("; "));
  }
  return entry;
}

export function proofReportErrors(report, capabilityNames) {
  const errors = [];
  if (!keys(report, "proof", ["capability", "status", "date", "fresh_inputs", "proof_kind", "anyuse_version"], [], errors))
    return errors;
  walk(report, "proof", errors);
  if (capabilityNames && !capabilityNames.includes(report.capability))
    errors.push("proof.capability: not a capability of this map");
  const inner = { ...report };
  delete inner.capability;
  validateProof(inner, "proof", errors);
  if (report.status === "unproved") errors.push("proof.status: a report is proved, discovered or refused");
  return errors;
}

// ── Ed25519 (v0) ───────────────────────────────────────────────────────────
function b64url(bytes) {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
}

function decodeB64url(value, pattern, size) {
  if (!full(pattern, value)) return null;
  try {
    const padded = value.replace(/-/g, "+").replace(/_/g, "/") +
      "=".repeat((4 - value.length % 4) % 4);
    const binary = atob(padded);
    const raw = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    return raw.length === size && b64url(raw) === value ? raw : null;
  } catch { return null; }
}

export async function stringToSign(method, pathWithQuery, timestamp, nonce, bodyBytes) {
  return enc.encode([method.toUpperCase(), pathWithQuery, String(timestamp), nonce,
    await sha256hex(bodyBytes)].join("\n"));
}

export async function verifySignature(publicKey, signature, method, pathWithQuery, timestamp, nonce, bodyBytes) {
  const pub = decodeB64url(publicKey, "public_key", 32);
  const sig = decodeB64url(signature, "signature", 64);
  if (!pub || !sig) return false;
  try {
    const key = await crypto.subtle.importKey("raw", pub, { name: "Ed25519" }, false, ["verify"]);
    return await crypto.subtle.verify({ name: "Ed25519" }, key, sig,
      await stringToSign(method, pathWithQuery, timestamp, nonce, bodyBytes));
  } catch { return false; }
}
