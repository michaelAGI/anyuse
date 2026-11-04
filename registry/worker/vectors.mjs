// Parity check: run the shared contract vectors through the Worker's validator.
// NOT EXECUTED (no node on this machine). Once node >= 20 is approved:
//     node --experimental-json-modules registry/worker/vectors.mjs
// Must print "ALL OK" — the same validation verdicts and Python-generated
// Ed25519 signature vectors that test_registry.py asserts.
import vectors from "../contract_vectors.json" with { type: "json" };
import { entryErrors, HASH_MISMATCH, verifySignature } from "./src/validate.js";

let bad = 0;
for (const v of vectors.vectors) {
  const errs = await entryErrors(v.entry);
  const code = errs.length === 0 ? "ok" : (errs.length === 1 && errs[0] === HASH_MISMATCH ? "hash_mismatch" : "schema");
  if (code !== v.expect) { bad++; console.log("MISMATCH", v.name, code, errs.slice(0, 3)); }
}
const signatureVectors = vectors.signature_vectors;
let verified = 0;
for (const v of signatureVectors.vectors) {
  const ok = await verifySignature(signatureVectors.public_key_base64url, v.signature_base64url,
    v.method, v.path_with_query, v.timestamp, v.nonce, new TextEncoder().encode(v.body_utf8));
  if (ok) verified++;
  else { bad++; console.log("SIGNATURE MISMATCH", v.name); }
}
console.log(`ed25519 ${verified}/${signatureVectors.vectors.length}`);
console.log(bad ? `${bad} MISMATCHES` : "ALL OK");
process.exit(bad ? 1 : 0);
