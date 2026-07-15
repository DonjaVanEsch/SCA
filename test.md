# Testing the dashboard end-to-end

A manual walkthrough for verifying both fingerprinting subsystems actually
work through the dashboard UI: server-side (probe a running framework/library
container) and client-side (drive traffic at a persistent target from a
one-shot client container). See [CONTEXT.md](CONTEXT.md) and
[CONTEXT_CLIENTS.md](CONTEXT_CLIENTS.md) for the underlying architecture --
this file is just "what to click, in what order, to prove it works."

## Prerequisites

- Docker must be reachable (local engine, or a remote host configured under
  **Settings → Docker host**).
- Fingerprinting is gated behind an environment variable, not a UI toggle:
  launch the dashboard with `PQC_ENABLE_FINGERPRINT=true` set, e.g.
  ```
  PQC_ENABLE_FINGERPRINT=true python dashboard.py
  ```
  If it's off, the 🫆 Fingerprint checkboxes/actions are hidden and the
  `/api/*/fingerprint` routes reject with an error.
- Open **Settings** (gear icon) once and turn on **Accessibility mode** --
  every check below becomes easier to eyeball (✓/✕ next to badges instead of
  relying on color alone), and it's also a quick way to confirm the "settings
  save refreshes the current view" fix actually works: the currently open tab
  should visibly update the moment you hit Save, with no manual reload.

---

## Part 1 -- Server-side (framework/library) fingerprinting

This is the original subsystem: probe a *running* container from the
outside (4 calls: success / 404 / wrong-method / malformed) and capture what
tcpdump sees on its own network namespace.

1. Default view is **Server mode**, **Sources** tab.
2. Narrow the list with the filter sidebar so the run stays short -- e.g.
   Language = `python`, Framework = `flask`.
3. Select a handful of rows (checkbox per row, or **Select ▾ → Current
   page**).
4. In the **🔨 Build & Test** action group, check **Build**, **Test**, and
   **🫆** (Fingerprint). The tooltip on 🫆 explains: fingerprint reuses the
   same container start as Test, so checking both together costs one
   container lifecycle, not two.
5. Click **▶ Execute**. Watch the log panel; it should show each image
   building, then starting a container, then the four fingerprint probes.
6. Back in the table, confirm each row now shows Build ✓ and Test ✓ status
   circles.
7. Click the 🫆 icon in a row's rightmost column (only present once that row
   has a captured fingerprint) -- the **Details side panel** opens with a
   "🫆 Fingerprint" tab showing the raw tcpdump output for the successful
   call and the unsuccessful call separately, each with its own status code
   and a "captured at" timestamp.
8. Sanity check in the **Reports** tab: the same test run should show up
   there too, independent of the fingerprint capture.

**What would indicate a real problem:** Build/Test succeed but the 🫆 icon
never appears (fingerprint silently didn't run), or the traffic panes are
empty/"no packets captured" for a call that returned a real status code.

---

## Part 2 -- Client-side (client-fingerprinting) subsystem

This is the new subsystem: a persistent **fingerprint-target** app stays up,
and short-lived **client** containers (one per HTTP-client-library) each fire
a single outbound call at it while tcpdump sniffs the *target's* side.

1. Click **⇄ Switch to client mode** (top right of the header) -- the Clients
   tab replaces Sources, and the stats bar switches to "client images / built
   / fingerprinted".
2. Use the filter sidebar to narrow down, e.g. **HTTP client = m2crypto-raw**
   (one of the two raw-TLS clients -- see step 6, this is the one you want
   for the baseline/match test).
3. Select the row(s) you want (checkbox).
4. In the **🔨 Build & Fingerprint** group, check **Build** and **🫆
   Fingerprint**, then **▶ Execute**.
   - First run for any client image: this also builds and starts the
     persistent `pqc-fingerprint-target` container if it isn't already up
     (idempotent -- safe to leave running between test sessions).
   - Watch the log: `[i/n] <image-tag>` → `CAPTURED status=200
     client_output={...}` per image. `SKIP (not built)` means the image
     wasn't actually built yet -- go back and check **Build** too.
5. Click the row's **⋯ Details** button (always present, rightmost column)
   -- the same side-panel drawer as server mode opens, showing:
   - **Ground truth vs. self-reported**: language/version and
     client/version, each with its own ✓/✕, plus what the client's own JSON
     stdout claimed about itself.
   - **Network-observed (independent)**: for a plain-HTTP client
     (`requests`/`httpx`/`urllib3`), the actual **User-Agent header** seen on
     the wire, with its own match icon against ground truth. For a raw-TLS
     client (`pyopenssl-raw`/`m2crypto-raw`), the **JA3 TLS fingerprint**
     instead (see step 6 for what to expect there). For `http.client`,
     expect an honest "no independent signal available" note -- it sends no
     User-Agent and uses no TLS, so there's genuinely nothing to observe.
6. **To actually exercise the JA3 baseline mechanism, fingerprint the same
   image twice:**
   - The **first** capture of any given client image (unique per
     language + version + http-client + version) has nothing to compare its
     JA3 hash against, so it becomes that image's own reference baseline --
     the Details panel labels it **"baseline"**, not a match/mismatch.
   - Select that *same* image again and run **🫆 Fingerprint** a second
     time. Open Details on this new capture: the JA3 hash should now show a
     real **✓** (matches the baseline) -- or **✕** if something about the
     TLS stack actually changed (e.g. the base image's OpenSSL got
     upgraded between the two runs).
   - Optional but instructive: do the same two-capture test for
     `pyopenssl-raw` at the same language version, then compare its JA3 hash
     against `m2crypto-raw`'s -- they should differ, which is the actual
     point of having two separate raw-TLS clients: proof the two crypto
     libraries are distinguishable purely from the TLS handshake.
7. Use the **Client images / Fingerprint log** toggle above the table to
   switch to the **Fingerprint log** view -- this lists every capture ever
   made (not just the latest per image) with Ground truth | Self-reported |
   Match | Status | Captured columns, each row's **⋯** opening the same
   Details drawer.

**What would indicate a real problem:**
- A plain-HTTP client's Network-observed section stays empty (User-Agent
  should always be present for `requests`/`httpx`/`urllib3`).
- `observed_client_match` is ✕ when the versions clearly agree (would point
  at a User-Agent-parsing bug, not a real version mismatch).
- The second capture of the same raw-TLS image is *also* labelled
  "baseline" instead of comparing against the first (would mean the
  reference table isn't being read/written correctly).
- Ground truth vs. self-reported shows a real ✕ for `client_version_match`
  or `language_version_match` -- this would mean pip actually resolved a
  different version than the registry says it should have, a genuine bug
  worth chasing down, not a UI issue.

---

## Quick reference: badges you'll see

| Badge | Meaning |
|---|---|
| 🫆 in color | This image has at least one captured fingerprint |
| 🫆 grayed out | Not fingerprinted yet |
| ✓ / ✕ next to a badge | Only shown with Accessibility mode on |
| "baseline" label (JA3) | This was the first-ever JA3 capture for this exact image |
| "–" (dash) | Nothing to compare (e.g. no self-report, or no network signal available for this client type) |
