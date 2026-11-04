# Per-site web-tier modes

Code: `execution/site_modes.py` (storage, the site-key rule, the one accessor),
`execution/session_import.py` (cookie import), `anyuse sites` and
`anyuse attach --login-replay` in `cli.py`. Tests: `test_site_modes.py`,
`test_session_import.py`.

AnyUse's compiled web tier replays a request the site's own UI already made, over
plain HTTP. By default:

* it identifies itself as `anyuse-compiled-workflow/0.1`;
* it follows `robots.txt` and paces 1 req/s ([TRAFFIC_POLICY.md](TRAFFIC_POLICY.md));
* it does not compile a credential-bound request (the compiler keeps no cookie, and
  `attach.py`/`har_import.py` drop cookie values at ingestion);
* logged-in work goes through the real app/UI instead.

Each of these is a **per-site option** with that default; a site keeps it until you
opt it in.

## The four options

| option | default | other value | what the other value does |
|---|---|---|---|
| `identity` | `honest` | `browser` | the replay sends the **User-Agent captured from the recorded browser request** (template field `browser_ua`) instead of the AnyUse UA. That one header is the only change. A workflow with no captured User-Agent is refused under `browser`; no UA is invented. |
| `robots` | `respect` | `ignore` | `robots.txt` is not consulted for this site's replays. Pacing stays on (1 req/s). Built on the existing robots policy: the executor passes `policy="ignore"` to `api_permission.replay_refusal`, the same code path `ANYUSE_ROBOTS_POLICY` and `acknowledge-host --ignore-robots` use. |
| `login` | `none` | `replay` | a credential-bound request may compile and replay, using a session imported from a browser profile a human picked. |
| `route` | `ui` | `replay` | logged-in work takes the fast HTTP replay instead of the UI path. Requires `login = replay`. |

## Which host an entry covers (the site-key rule)

An entry is stored under its **registrable domain**, and only when that domain is
**certain**. There is no network, so no public-suffix list can be fetched; an
entry whose registrable domain would be a guess is **refused**
(`site_modes.site_key` / `validate_host`). In order:

1. **Shared-hosting / private registries** (`github.io`, `herokuapp.com`,
   `vercel.app`, `netlify.app`, `duckdns.org`, CentralNic's `uk.com`/`us.com`/…,
   the shared-hosting list in `execution/web_scope.py`, plus
   `site_modes.EXTRA_PRIVATE_REGISTRIES`): every subdomain is a different owner,
   so the entry is the **full tenant host** (`me.duckdns.org`, `x.uk.com`). An
   entry for the registry itself (`duckdns.org`, `github.io`) is refused.
2. **Known public second-level registries**: the multi-label list in
   `execution/web_scope.PUBLIC_SUFFIX2` (`co.uk`, `com.au`, `co.jp`, `com.br`, …)
   and, for each flat TLD below, its enumerated second levels
   (`site_modes.TLD_SECOND_LEVELS`: `qc.ca`, `co.me`, `com.co`, `asso.fr`, …). The
   entry is **one label more specific** than the suffix (`x.qc.ca`,
   `example.co.uk`); the suffix itself is refused.
3. **Flat TLDs** (`site_modes.FLAT_TLDS`): com, net, org, info, biz, io, ai, app,
   dev, xyz, tech, online, site, store, cloud, page, blog, shop, tv, cc, fm, me,
   co, to, eu, ws, de, nl, ch, li, at, be, dk, fi, cz, es, fr, ca, ie, lu, is, ee,
   lt, lv, si, sk, pt. Registration is directly under the TLD, so the entry is the
   last two labels: `web.de`, `id.me`, `co.co`, `google.ca`.
4. **Everything else is refused.** TLDs with many geographic or functional second
   levels that this build does not enumerate (`it`, `no`, `pl`, `se`, `jp`, `br`
   outside the listed `com.br` etc., `us`, `uk` outside the listed `co.uk` etc.,
   and every ccTLD not listed) are absent: `shop.adv.br`, `x.mi.it`,
   `foo.tokyo.jp` and `shop.com.pk` are refused rather than stored as their
   suffix.

The host is IDNA-encoded with **UTS46, non-transitional** (the `idna` package,
an httpx dependency), so `straße.de` stays `xn--strae-oqa.de` and is not folded to
`strasse.de`; without the package, a host whose IDNA2003 and NFC forms disagree,
or that contains ß, ς, ZWJ or ZWNJ, is refused. After encoding a host may contain
only letters, digits, hyphens and dots. `*`, wildcards and bare TLDs are refused;
there is **no** environment variable and **no** global default.

At use time `effective_modes` re-validates the key, so a hand-edited entry that is
not itself a valid site key (`[sites."co.ug"]`, `[sites."duckdns.org"]`) never
applies.

**Note on `go.to`:** it passes the site-key rule, but with `ANYUSE_REFUSE_GOV_HOSTS`
on the government rule (`execution/gov_hosts.py`, the `go.<cc>` pattern) refuses it —
see below.

## Storage and file trust

One file, `<data dir>/site_modes.toml` (`$ANYUSE_DATA_DIR/site_modes.toml`,
default `~/.anyuse/site_modes.toml`), written atomically with owner-only (0600)
permissions — the `settings.py` pattern. Every control character in a stored
string (and in the table key) is escaped as `\uXXXX`; `anyuse sites login` also
refuses a control character in `--db`, `--profile`, `--browser` and
`--cookie-host`.

The file gives **no modes** (and the reason is logged) unless: its parent
directory is owned by this user and not group/world-writable; it opens with
`O_NOFOLLOW` (a symlink is refused); and the **open descriptor** is `fstat`'ed as a
regular file owned by this user and not group/world-writable. A writer
(`sites set`/`reset`/`login`) refuses to replace a file it cannot parse or does
not trust, so a corrupt file is not silently rewritten with one entry.

For `login = replay` the file stores only a **reference** to the imported
session — the browser, the profile directory name, the cookie host, the chosen
cookie-database path and the cookie **names**. A cookie **value** is never written
to the file, a log, a compiled template or any saved session.

## The CLI (the only writer)

```console
anyuse sites                                   # same as: sites list
anyuse sites show <host>
anyuse sites set <host> --identity browser --robots ignore --login replay --route replay
anyuse sites login <host> --browser chrome --profile "Profile 1" --db <cookies-db> [--cookie-host <host>]
anyuse sites reset <host>
anyuse attach --cdp http://127.0.0.1:PORT --login-replay <host> [--input q=VALUE]
```

Enabling any non-default value, and `sites login` (which enables
`login = replay`), asks for the same typed-code confirmation in the terminal as the
security-relevant settings (`execution/human_gate.TtyGate`), so it needs a TTY.

`anyuse sites login` reads only the cookie **names** the host has in the chosen
database (plaintext; no decryption, no Keychain prompt) and records the
reference, including that exact database path, so replay reads the profile the
human picked. The cookie host must be under the site, must not be on the
never-contact list and, with `ANYUSE_REFUSE_GOV_HOSTS` on, must not be a
government host.

## Cookie import (`execution/session_import.py`)

* **Chrome family on macOS** (Chrome, Arc, Brave, Edge, Chromium): the `Cookies`
  SQLite file. Values are `v10`-prefixed AES-128-CBC, 16-space IV, key
  `PBKDF2-HMAC-SHA1(password, b"saltysalt", 1003, 16)`, password from the
  Keychain item `"<Browser> Safe Storage"`. Every PKCS7 pad byte is verified;
  for DB `meta` version ≥ 24 the 32-byte plaintext prefix must equal
  `SHA-256(host_key)` and is stripped — a mismatch is a wrong key and is refused.
  Below version 24 Chromium's `v10` format carries **no MAC**: a wrong key yields
  valid-looking padding about once in 255 attempts, and nothing can detect that.
  A `vNN` prefix other than `v10` is refused. Expired cookies are dropped.
* **Firefox**: `cookies.sqlite` / `moz_cookies`, plaintext.
* **In memory only.** The database is read into memory and opened with
  `sqlite3.connect(":memory:").deserialize(bytes)`; it is never copied to disk or
  to a temp directory. A **WAL-mode** database (header bytes 18/19 = 2, as Firefox
  uses) has its committed `-wal` frames merged into the in-memory copy (salts and
  checksums verified, an unfinished transaction ignored) and bytes 18/19 set to 1
  on that copy; a cookie that exists only in the WAL is therefore read. Nothing
  is written to the user's database or its `-wal`/`-shm` files.
* **Per-hop send rules.** Cookies are records (name, value, domain, host-only,
  path, Secure). The `Cookie` header is rebuilt for **each hop** and carries a
  cookie only when its domain, path and Secure attribute match that hop (an
  https-only cookie never over http; a host-only `__Host-` cookie only to its
  exact host). For a repeated name the most specific wins (longest path, then
  host-only, then longest domain).
* The **Keychain read** and **every path** are injected; the real Keychain reader
  (`session_import.keychain_password`) exists for production and is never called
  by the tests. AES uses the `cryptography` package; it is not hand-rolled.

## Capture, compile and replay with `login = replay`

* **Capture.** `anyuse attach --login-replay <host>` (the site must already be
  `login = replay`) keeps, **for that site only**, a request whose only credential
  is a session cookie, and records its session cookie **names**. Only the
  signed-in **JSON data responses** the compile needs are stored; a signed-in
  document's body is not stored, and a page loaded signed in has its text and DOM
  **withheld** from the session file. Requests to any other host, or with an
  Authorization/API-key header or a signed query/body, are refused as before.
* **Compile and proof.** Because the signed-in pages are withheld, the ground
  truth is the signed-in data response: the JSON list path that changes with the
  input, chosen on the fitting sessions only and read from every validation
  session at that same path. The template carries `cookie_refs` (names) where the
  values were. The proof replays with the imported session through an executor
  that reads the site's modes via the one accessor; that executor is built only
  for sessions captured with `--login-replay`, which no bench records. Signed-in
  items are never echoed in the report and never stored in the saved record.
* **Replay.** Only for a site set `login = replay` **and** `route = replay`, the
  executor imports the referenced cookies into memory for one call, sends them
  with the per-hop rules above, and discards them.
* **Signed-in results are never persisted.** A run of a template with
  `cookie_refs` stores no drift anchor or last-good answer
  (`step_repair.note_success` is skipped), and compiling one stores no drift
  anchor either (`api_step.signed_in_template`).
* **Stale session vs bot wall.** Every response goes through `detect_blocked`.
  A **non-challenge** auth failure — a 401, a 403 the detector filed as plain
  `forbidden`, or a login wall where data was expected — goes to **one**
  re-import with the freshly read cookies, then (if it still fails) to the auth
  outcome with the next step `anyuse sites login <host>`. It never becomes a
  sticky do-not-retry block. If the re-import itself fails, the call stops; it
  never retries with the old cookies. A **challenge** (CAPTCHA, interstitial,
  rate limit) is recorded as a sticky block as before and is never retried.

## Capturing the User-Agent (`identity = browser`)

A request's User-Agent is captured into a session or HAR import **only** for a
site set `identity = browser`: the product `anyuse attach` and `--from-har` pass
`site_modes.browser_identity_for` (a view of the one accessor) as the capture
predicate. A bench passes none, and a site not set `browser` is never captured, so
a default session, the tool compiled from it, and its template digest are
**byte-identical** to before. The UA is applied only on hops whose site key is the
entry and that the government rule does not refuse (`ANYUSE_REFUSE_GOV_HOSTS`); a
redirect off the site falls back to the honest UA.

## Benchmarks run without the modes

Benchmarks run with the defaults (AnyUse UA, robots respected, 1 req/s, no
cookies, UI route). Modes are read through **one accessor on the product path**,
`site_modes.effective_modes(host)` (and its thin view
`browser_identity_for`), and only by an `ApiStepExecutor` constructed with
`apply_site_modes=True` (`run_compiled`, `execution/executor.py`, and the proof
of a `--login-replay` compile) or by the product recorders' capture predicate.
The guarded benchmark browser (`execution/cdp_guard.GuardedSession`) and the
bench drivers never construct the executor that way, never pass the predicate and
never import `site_modes` (`test_site_modes.BenchmarksNeverSeeModes`).

## Government and public bodies

The never-contact list, and `execution/gov_hosts.py` when `ANYUSE_REFUSE_GOV_HOSTS`
is on, gate both writing and using a mode. `anyuse sites set`/`login` refuse such a
host, and at use time a mode for one — even one hand-edited into the file — is
**ignored and logged**. With the setting off (the default), a government host takes
modes like any other site.

## Limits

These modes change how a request a person already made is replayed:

* a challenge still goes to a person ([HUMAN_INBOX.md](HUMAN_INBOX.md)) and the
  host is held on do-not-retry, and a host recorded as browser-only is still not
  tried with the HTTP client;
* `identity = browser` changes the User-Agent header and nothing else (no TLS
  fingerprint change);
* **no public-suffix list**: hosts under TLDs this build cannot decide are
  refused, and an unlisted private sub-registry under a flat TLD would be stored
  as its parent (the private-registry list is curated, not complete).
