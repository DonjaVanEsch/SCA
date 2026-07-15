# SCA — Client-Fingerprinting Context

> Companion to `CONTEXT.md`, which documents the SERVER side of this project (images that expose `GET /`/`GET /version` and answer probes). This file documents the CLIENT side, built on the `client-fingerprinting-python` branch: images that make ONE outbound call to a persistent "fingerprint target" app, so real network traffic FROM a known language/HTTP-client-library/version combination can be captured and later used to identify an unknown caller.

## What is this subsystem?

The server side answers the question "if I probe a target, what is it running?" The client side answers the reverse: "if I *observe* an incoming call, what is the *caller* running?" Both directions reuse the exact same tcpdump-sidecar capture mechanism (`manager.py`) — only which container the sniffer attaches to, and which container drives traffic, are swapped.

```
language version  ×  HTTP-client library version
```

Deliberately a **2D** matrix, not 3D like the server side — a client program has no server-side framework. A crypto-library-driven client (`pyopenssl-raw`, `m2crypto-raw`) is just another entry in the HTTP-client-library axis, not a separate cross-product dimension: it drives its own raw TLS handshake instead of using a normal HTTP-client library, so the crypto library itself (not the language's default `ssl` module) is what shows up in the connection's TLS fingerprint.

---

## Repository layout (additions on top of `CONTEXT.md`)

```
SCA/
├── images_clients/                      # Generated client Docker contexts (do not edit by hand)
│   └── python/
│       └── {lang_version}/
│           └── {http_client_name}/
│               └── {http_client_version}/
│                   ├── Dockerfile
│                   ├── client.py
│                   └── requirements.txt   # omitted for stdlib-only clients (http.client)
│
├── scripts/
│   ├── generate_client_images.py    # Entry point: reads registry's "http_clients" → writes images_clients/
│   ├── lang_python_client.py        # Python client app templates + Dockerfile generator
│   ├── registry python.json         # Same file as the server side -- has an added "http_clients" section
│   └── fingerprint_target/          # The persistent target app (NOT part of the registry-driven matrix)
│       ├── app.py                   # Plain HTTP (9000) + TLS (9443, self-signed) listener, always 200+JSON
│       └── Dockerfile                # Generates the self-signed cert at build time via openssl
│
├── manager.py                       # Same file as the server side -- gained client-specific functions:
│                                     #   _ensure_fingerprint_target(), _capture_client_fingerprint(),
│                                     #   _do_client_fingerprint(), _client_image_tag()
├── db.py                            # Same file -- gained http_clients/http_client_versions/client_images/
│                                     #   client_fingerprints tables (see below)
└── CONTEXT_CLIENTS.md                # This file
```

---

## How it works, end to end

### 1. Registry (`scripts/registry python.json`, `"http_clients"` section)

Same file as the server side, one new sibling section alongside `"frameworks"`/`"cryptography_libs"`:

```jsonc
"http_clients": [
  { "name": "http.client",   "module": null,       "version": "built-in" },
  { "name": "requests",      "module": "requests", "version": [{ "nr": "2.34.2", "compatibility": ["3.8+"] }] },
  { "name": "httpx",         "module": "httpx",    "version": [{ "nr": "0.28.1", "compatibility": ["3.8+"] }] },
  { "name": "urllib3",       "module": "urllib3",  "version": [{ "nr": "2.7.0",  "compatibility": ["3.9+"] }] },
  { "name": "pyopenssl-raw", "module": "pyOpenSSL","version": [{ "nr": "26.3.0", "compatibility": ["3.7+"] }] },
  { "name": "m2crypto-raw",  "module": "M2Crypto", "version": [{ "nr": "0.48",   "compatibility": ["3.6+"] }] }
]
```

The `"nr"` value is the **raw registry bucket** (e.g. `"0.48"`), never a pre-resolved exact patch version — this matters: the directory name, the DB row's `version_nr`, and the Docker tag all use this raw bucket, exactly matching the server side's own convention. Resolution to an exact pip-installable version (`0.48` → `0.48.0`) happens *inside* `make_requirements()`, only for the `requirements.txt` pin — never for naming. Getting this backwards (resolving before naming) was a real bug found while building this: it silently broke DB sync because `load_registry()` stores the raw bucket, and the two must match for FK resolution to succeed.

### 2. `generate_client_images.py`

Mirrors `generate_images.py`'s structure over the simpler 2D matrix. Reuses `generate_images.py`'s own `_included()` compatibility helper unmodified (language-agnostic).

```bash
python scripts/generate_client_images.py --lang python
```

### 3. `lang_python_client.py`

One `app` template function per client type, all producing a one-shot script that:
1. Reads the target URL from the `PQC_TARGET_URL` env var.
2. Makes exactly one outbound call using that specific library.
3. Prints a single JSON line to stdout (`client`, `client_version`, `language_version`, `status_code`, `body`) and exits.

| Client | PyPI package | Needs target's HTTPS port (9443)? | Build-time system deps |
|---|---|---|---|
| `http.client` | — (stdlib) | no | — |
| `requests` | `requests` | no | — |
| `httpx` | `httpx` | no | — |
| `urllib3` | `urllib3` | no | — |
| `pyopenssl-raw` | `pyOpenSSL` | **yes** | — |
| `m2crypto-raw` | `M2Crypto` | **yes** | `build-essential`, `swig`, `libssl-dev` (SWIG-generated C extension) |

**`pyopenssl-raw`/`m2crypto-raw` don't use an HTTP-client library at all** — they open a raw `socket`, wrap it with that library's own `SSL.Connection`, and speak raw HTTP/1.1 over the encrypted stream by hand. Three real, non-obvious bugs were found getting this working (all fixed in the current templates, confirmed via real docker build+run against the actual TLS target — not assumed from docs):
- pyOpenSSL's `do_handshake()`/`recv()`/`send()` can raise `SSL.WantReadError`/`WantWriteError` even on a blocking socket — needs a retry loop (`select.select()` on the socket, then retry), the standard documented pattern, not a one-shot call.
- The target's plain `http.server`-based TLS wrapping doesn't send a clean `close_notify` on its final write — pyOpenSSL sees this as `SSL.SysCallError`/`SSL.ZeroReturnError` on the final `recv()`, which is the *normal* end of a complete response here, not a transport failure. Caught and treated as "done", not re-raised.
- Calling `conn.shutdown()` afterward on a connection the peer already closed abruptly raises `OpenSSL.SSL.Error: []` — wrapped in its own try/except since the response was already fully read by that point regardless.
- M2Crypto does its own hostname verification against the TLS certificate's CN, separate from `verify_mode` — confirmed a mismatch error only appeared when testing against a container name that didn't match the cert's baked-in CN (`pqc-fingerprint-target`), not a real code bug.

### 4. `scripts/fingerprint_target/` — the persistent target app

Unlike every server image (started fresh per test, stopped after), this one **runs continuously** — `manager._ensure_fingerprint_target()` builds and starts it idempotently before every client-fingerprint pass, on its own dedicated Docker network (`pqc-fingerprint-net`). It doesn't inspect or store anything itself: any method/path is accepted and always answered with `200` + a small JSON body, so a client's own success/failure handling never gets in the way of the capture. Listens on both:
- **9000, plain HTTP** — for `http.client`/`requests`/`httpx`/`urllib3`.
- **9443, TLS** (self-signed cert generated at image build time via `openssl req -x509 ...`, CN=`pqc-fingerprint-target`) — for `pyopenssl-raw`/`m2crypto-raw`, since a raw-TLS client needs something that actually speaks TLS to make its own handshake implementation visible at all.

### 5. `manager.py` — capture orchestration

The reverse direction of the server side's `_capture_fingerprint()`/`_capture_traffic()` — reuses `_capture_traffic()` **completely unchanged**, just pointed the other way:

| Server-side fingerprinting | Client-side fingerprinting |
|---|---|
| tcpdump sidecar attaches to the **server** container's netns | tcpdump sidecar attaches to the **target** container's netns |
| "action" = fire an HTTP probe *at* the server | "action" = run the **client** container once, to completion |
| Captures what the server answers | Captures what the target *observes arriving* |

Key functions (`manager.py`):
- `_ensure_fingerprint_target()` → `(container_name | None, error)` — idempotent build+start; returns `None` with a real error message on failure instead of silently proceeding (a real bug found and fixed: the first version swallowed build failures and returned a container name that didn't actually exist).
- `_run_client_container(client_tag, container_name, network, target_url)` — runs the one-shot client to completion, returns `(exit_code, stdout, stderr)`.
- `_capture_client_fingerprint(client_tag, target_container, network, http_client_name="")` — the actual capture; picks the target's HTTPS port automatically when `http_client_name` is in `_TLS_RAW_CLIENTS` (a real bug found and fixed: originally hardcoded the plain-HTTP URL for every client, which made the raw-TLS clients fail with "wrong version number" — a TLS ClientHello arriving at a non-TLS listener).
- `_do_client_fingerprint(entries, log_fn, save_fn, stop_event)` — the full per-image loop, mirroring `_do_fingerprint()`.

### 6. `db.py` — reference + result tables

Deliberately a **separate set of tables**, not the existing `images`/`frameworks` repurposed with a "kind" flag — a client has no server-side framework, so its natural FK target (`http_client_versions`, not `fw_versions`) is a genuinely different table, not an overloaded column.

**Reference tables** (populated by the *same* `load_registry()` function the server side uses, extended to also read `lang_obj.get("http_clients", [])`):

| Table | Contents |
|---|---|
| `http_clients` | HTTP-client libraries per language |
| `http_client_versions` | Versions with release date + compatibility JSON |

**Image table** (synced by `sync_client_images()`, mirroring `sync_images()`):

| Table | Contents |
|---|---|
| `client_images` | One row per client Dockerfile; FKs into `lang_versions` + `http_client_versions` |

**Capture results** (the client-side equivalent of `fingerprints`):

| Table | Contents |
|---|---|
| `client_fingerprints` | One row per captured client run: `client_image_id` FK, `host`, `status_code`, `traffic_raw` (tcpdump `-XX -v` text), `pcap_raw` (base64 binary capture), `error_msg`, `captured_at`, `run_id` |

No `call_type` column here (unlike the server side's `fingerprints` table) — a client only ever makes the one outbound call its generated program makes, there's no success/failure/method-not-allowed/malformed probe matrix on this side.

Key functions:
```python
db.load_registry()                                  # Same function as server side; also loads http_clients now
db.sync_client_images()                             # Walk images_clients/ → upsert client_images rows
db.save_client_fingerprint_results(client_image_id, record, run_id, host)
db.get_client_fingerprint_report(client_fingerprint_id)  # One capture, joined with its ground truth
db.get_client_fingerprints(client_image_id=None)     # List captures, newest first, optionally scoped
```

`get_client_fingerprint_report()`/`get_client_fingerprints()` are what the dashboard's "report" view is built on: since **we** trigger every client run, `client_image_id` is ground truth we already know (language, HTTP-client library, version) — the report simply joins a capture back to that known answer, which is exactly what makes this fleet useful as *labeled* training/reference data for later identifying an *unknown* caller.

---

## Deliberate design decisions (and why)

- **Raw registry bucket names everywhere, never pre-resolved versions** — matches `generate_images.py`'s own convention; getting this backwards broke DB sync silently (found live, fixed).
- **No separate crypto-library dimension** — a crypto-lib-driven client is just another `http_clients` entry (`pyopenssl-raw`/`m2crypto-raw`), keeping the matrix 2D instead of 3D.
- **Separate DB tables, not a `kind` flag on the existing ones** — `http_client_versions` is a different FK target than `fw_versions`; conflating them would need a polymorphic FK, which SQLite doesn't support cleanly.
- **The target app is infrastructure, not part of the generated matrix** — it doesn't vary by language/version, so it isn't registry-driven; it's a single hand-written app, built and kept running by `manager.py` directly.
- **All verification this session was done on scratch/tmp directories over SSH, never via `deploy_to_server.ps1`** — this branch's work never touched the live, shared dashboard deployment; every build/run test used uniquely-named `debug-*` containers cleaned up immediately after.

## Known gaps / not yet built

- Dashboard UI: a Server/Client mode toggle (with a distinct color scheme, not just a text label) plus a client image listing, build/fingerprint actions, and a report view — designed, not yet implemented as of this writing.
- Only Python has a client generator (`lang_python_client.py`) so far — the other four languages (Node, PHP, Java, .NET) don't have one yet.
- No "production vs dev mode" equivalent has been explored for clients (the server side gained a `PQC_DEBUG` toggle for Django specifically; clients don't have an analogous debug-mode concept since they don't serve responses).
