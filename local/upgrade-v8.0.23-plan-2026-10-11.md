# Controlled CLIProxyAPI v8.0.23 upgrade

Approved scope: the operator's 2026-10-11 "proceed" authorizes preparing,
qualifying, and safely deploying the patched upgrade discussed in this thread.
The follow-up explicitly authorizes downloading the official source archive.
It does not authorize deploying an untested binary or interrupting live work.

## Constraints

- Preserve production version `7.3.3-local.account-limits.1` until qualification.
- Preserve credentials, model defaults, 200K compaction, aliases, and live tabs.
- Preserve unrelated existing dirty/untracked work.
- Use a separate source/candidate directory; do not overwrite the current checkout.
- Preserve `/v0/local/codex/account-limits`, its loopback/browser isolation,
  redirect suppression, and explicit-proxy failure safeguards.
- Do not share or rotate native Codex credentials for candidate testing.
- Keep the installed gateway independent of the core upgrade.
- Record blockers and real test results; never substitute source review for a
  successful candidate build or real inference.

### Task 1: Stage and qualify dual-layout launcher configuration

**Interfaces:** Produces a staged launcher and a small `claudex-config` reader
binary, consuming the existing config path and supporting legacy `api-keys`
and v8 `access.api-keys`. It must never read upstream provider API keys as
local client credentials. A present v8 client-key field wins over legacy data.

1. Copy the gateway/launcher module into an isolated task directory; no real
   config or credentials are copied.
2. Add behavioral tests for legacy and v8 YAML, precedence, malformed input,
   empty/invalid keys, provider-key isolation, and non-disclosure on errors.
3. Observe the v8 launcher fixture failing before implementation.
4. Implement the smallest proper-YAML reader and wire the staged launcher.
5. Build and run reader tests plus existing launcher, alias/resume, and gateway
   tests. Keep malformed configuration fail-closed and avoid secret logging.
6. Independently review the staged changes before promotion.

**Expected:** Tests and helper/gateway builds pass. Production file hashes
remain unchanged. No candidate runtime credential acquisition occurs.

### Task 2: Acquire v8.0.23 and port local functionality

**Interfaces:** Consumes the official source archive and existing local
account-limits source/tests/patch; produces a version-labelled patched core
binary and complete reproducible patch.

1. Download the pinned official archive; validate tag/commit provenance and
   source identity before applying changes.
2. Run baseline tests and required server build in isolated source.
3. Port the local endpoint and security guards, updating `/v7` imports to `/v8`.
4. Reproduce missing endpoint on baseline, then run the ported tests and full
   suite; document unrelated baseline failures instead of hiding them.
5. Build candidate, record size/SHA-256, and retain source and test evidence.

**Expected:** The exact candidate compiles and all scoped regression gates pass.

### Task 3: Real protocol qualification and controlled cutover

**Interfaces:** Consumes the candidate core and launcher/helper; produces
live qualification evidence and a reversible deployment record.

1. Use host-side access to qualify separately from production; avoid concurrent
   refresh of copied rotating credentials.
2. Check Astra and GPT-6.1 Sol discovery, streaming, tool round trips,
   compaction/resume, plain/qualified alias identity, and usage-cache freshness.
3. Validate legacy config and v8-save migration with the launcher.
4. Verify idle state and rollback backups before an atomic cutover.
5. Restart only what is required; preserve user tabs and verify deployed hashes,
   actual inference, local limits endpoint, and statusline freshness.

**Expected:** No active work is interrupted; live checks pass on installed bytes.

## Review focus

Client/provider key confusion; new-field precedence including empty/null data;
malformed or ambiguous YAML; credential leakage in diagnostics; unsafe fallback;
new runtime dependency packaging; default/model/compaction drift; partial upgrade
deployment; copied-refresh-token races; loss of local-route isolation or transport
safeguards; accidental claims of live qualification from mock tests.
