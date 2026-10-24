# Flash Next streaming repair

Prepared 2026-10-22 NZDT (Pacific/Auckland). Status: installed 03:36:31 NZDT; live client progress verified, dedicated smoke deadline recorded.

## Incident and cause

The operator reported API errors in strix-qw and keywatcher-qw, including a
roughly 19-minute failed manual compaction. Read-only inspection identified
strix-qw as terminal_32/PID 2323983 and keywatcher-qw as terminal_53/PID 2740732.
No input was sent to either pane. The new 200000-token compaction settings were
already present in the resumed Keywatcher process; compaction settings do not
repair transport failures.

Strix's persisted error at 02:55:20 NZDT is "The response stopped arriving".
The installed Claude Code 2.1.306 emits this for its stream-idle watchdog.
Gateway logs also show exact five-minute client cancellations, with matching
CLIProxyAPI HTTP 499 responses. Total turn duration is not the idle deadline.
Keywatcher's exact manual-compaction exception was not retained in its main
transcript, so it cannot be attributed conclusively from that record alone.

Two compatibility defects were reproduced:

1. Dragon's installed vLLM 0.1.dev20073+g8e685d198 serializes DeltaMessage with
   `reasoning`, with no serialization alias. CLIProxyAPI 7.3.3 only reads
   `reasoning_content`. Reasoning updates therefore disappear during translation.
   The native client allows only 30 consecutive heartbeat-only updates to reset
   its event watchdog; connection keep-alives cannot substitute indefinitely
   for actual model progress.
2. The installed CLIProxyAPI binary matches the September account-limits build
   exactly, but omits the previously deployed bootstrap-heartbeat repair.
   The handler waits for stream setup/first translated data before starting
   its heartbeat ticker. A slow first result can leave the client without bytes.

Live metrics at 03:26:35 NZDT showed three running requests, one waiting for
capacity and zero preemptions. This is a point-in-time observation, not proof
of every request's behavior. No model restart or new GPU inference occurred.

## Change

The candidate preserves the existing account-limits and transport patches.
It restores bootstrap keep-alives and accepts both reasoning field spellings
for streaming and non-streaming Claude translation. A populated legacy field
takes precedence when both are supplied, avoiding duplicate thinking output.
Timeouts, model context, concurrency, credentials and image-history handling
are unchanged. Tool argument buffering is unchanged; this repair does not
claim to eliminate every possible long-output stall.

- Base: official v7.3.3, commit `7bbfeaf8`.
- Existing baseline overlay: `local/patches/v7.3.3-local-account-limits.patch`
  in the owning repository's main checkout.
- New overlay: `local/patches/v7.3.3-local-flash-stream-2026-10-22.patch`.
- Candidate: `tmp/cli-proxy-api.candidate`.
- Version: `7.3.3-local.account-limits.flash-stream.2`.
- Candidate SHA-256: `56fa56e3daa2ebfc67e6032fa0316b79456a3acb9967e1313b4ca56f6ffc03f3`.
- Runtime preimage SHA-256: `d6c7ebd541fd49c97313ff9a5d34f086711369df2557a1688736adaf94798d3e`.
- Manifest: `tmp/prepared-manifest.json`.

## Verification

Test-Driven Development (TDD — reproduce failure before the repair) confirmed
missing first-output heartbeats and discarded reasoning fields before edits.
After repair, the complete Claude handler and OpenAI-to-Claude translator
packages pass. Focused handler and executor race tests pass; vet, server build,
diff checks and reverse application of the retained patch pass.

The full Go suite reports only
`TestOpenAICompatExecutorToolResultContentByInputModalities`, also reproduced
on the unchanged v7.3.3 baseline. This remains an explicit baseline failure,
not an all-suite PASS. Logs are in `tmp/*test*.log`, `tmp/heartbeat-*.log`,
`tmp/reasoning-*.log` and `tmp/baseline-failure.log`.

Two disposable native Claude Code clients, using a synthetic loopback upstream,
each survived 330 seconds without first model output and completed successfully
at 03:30:36 NZDT. This tested the heartbeat build. The final combined build
then delivered two real thinking-delta events to each of two concurrent native
clients and completed successfully at 03:31:42 NZDT. No live credentials,
conversation payloads, production requests or Dragon GPU capacity were used.
Sanitized receipts: `tmp/native-heartbeat-330.json` and
`tmp/native-heartbeat-2.json`. Reusable fixture:
`local/tests/native_flash_stream_probe.py`.

The Dragon package aggregate static gate is not applicable to this proxy-only
source change; no Dragon deployment package source was edited. Production
Flash inference and recovery of the two reported sessions remain unverified
until deployment. The deployment section below records the subsequent authorized restart.

## Deployment and rollback

At 03:31:41 NZDT, the shared gateway still had established client sockets owned
by both affected tabs. Socket presence alone does not establish active request
count, but it does not establish a safe quiet window either.

Before installation, recheck service identities, live clients and the runtime
preimage hash. Obtain a quiet window or operator authorization to interrupt
active requests. Back up the runtime binary with its hash; atomically install
the candidate and restart only `cliproxyapi.service`. Preserve configuration,
the separately patched gateway binary, all tabs and Dragon's model service.
Verify loopback listeners, model discovery, account-limits compatibility and
bounded real Flash streaming before retrying compaction. Existing clients can
use the server repair on their next request without being relaunched.

If validation fails, atomically restore the backed-up binary and restart only
the same service within the authorized window. Do not commit, merge or push
without operator instruction.


## Authorized installation

The operator explicitly instructed installation after being told that in-flight
requests would be interrupted. At 2026-10-22 03:36:30 NZDT, the candidate was
installed atomically after checking the exact preimage and candidate hashes.
The requested `systemctl --user restart cliproxyapi.service` completed at
03:36:31 NZDT. The service manager also restarted its dependent gateway;
`claudex-gateway.service` has `Requires=cliproxyapi.service`. Proxy PID became
2825524 and gateway PID became 2825525. No tab input or model restart occurred.

Rollback custody:
`/home/gr33nl4dy/dev/cliproxyapi/runtime/backups/flash-stream-20261022-033629+1300/`.
The previous binary and a hash manifest are saved there. The final runtime path
is the existing persistent service executable; both user services remain enabled.
At 03:38:24 NZDT, the running executable hash matched the candidate. Hashes of
config.yaml, claudex-gateway, claude-codex and claudex-settings.json were unchanged.
Dragon's container PID 1127667 and start time 2026-10-20T12:42:31.40919017Z
were unchanged. Both listeners remained loopback-only on 8317 and 8318.

At 03:37:09 NZDT, both model catalogs returned HTTP 200 with the expected raw
Flash model and gateway alias. The account-limits endpoint returned HTTP 200
and schema version 1. A disposable tab named `flash-stream-install-check`
(tab ID 32) runs two bounded concurrent checks against the actual gateway and
Flash model. It closes automatically on exit. Its receipt is
`tmp/live-install-verification.json`; it contains counts, status and timings,
not conversation content or credentials.


### Post-install result and test cleanup

The dedicated two-request smoke ended at 03:42:02 NZDT. Both requests received
HTTP 200 headers after approximately 15 seconds and 15 heartbeat frames, but
neither received model output before the 240-second qualification deadline.
This is **not** a completed-inference PASS. At 03:40:11 NZDT, fresh model
metrics showed three running requests and six waiting. No timeout override or
model concurrency change was made to force the test through.

At 03:42:20 NZDT, a fresh Zellij pane listing confirmed the disposable
`flash-stream-install-check` tab had closed and no matching pane remained.
No existing user tab was closed or received input.

Independent read-only verification of the actual user transcripts at
03:42:53 NZDT established successful post-install progress:

- strix-qw: a thinking block at 03:39:31 NZDT and tool-use responses through
  03:42:20 NZDT, with no API-error flag in these records.
- keywatcher-qw: a thinking block at 03:40:45 NZDT and tool-use responses through
  03:42:10 NZDT, with no API-error flag in these records.

Both retain the expected Flash gateway identity. This verifies real client
receipt of reasoning and subsequent tool-use responses after installation;
it does not certify the completion of their tasks or a new long compaction.
The final deployment manifest records the dedicated smoke limit separately
from observed live-client recovery. The installed server repair requires no
client relaunch. A fresh retry is sufficient for a turn left at an error prompt.
