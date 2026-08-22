# Dragon Qwen3.8 local integration

Date: 2026-08-16 New Zealand Standard Time (NZST — UTC+12)

## Scope and safety boundary

This record documents the local CLIProxyAPI and Claudex integration for the
private Dragon Qwen3.8-27B service. It contains no credentials, authorization
headers, raw prompts, raw completions, or raw tool arguments. Validation used
bounded structural tool calls only; no model-generated tool, command, path,
URL, filesystem operation, or network operation was executed.

The service chain is:

```text
Claudex / Claude Code
  -> Anthropic Messages API on 127.0.0.1:8318
  -> Claudex gateway
  -> CLIProxyAPI on 127.0.0.1:8317
  -> OpenAI-compatible chat completions
  -> SSH loopback tunnel on 127.0.0.1:18101
  -> Dragon vLLM on 127.0.0.1:8101
  -> Qwen3.8-27B BF16
```

Every listener in this chain is loopback-only. Dragon port `8100` remains
unused. Protected ports `8083-8084`, `8090-8092`, and `9999` are outside this
integration and must not be changed.

## Pinned Dragon runtime

- Model: official `Qwen/Qwen3.8-27B` revision
  `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0`.
- Format: Brain Floating Point 16 (BF16 — 16-bit floating-point format used for
  model weights and attention cache).
- Runtime image:
  `nvcr.io/nvidia/vllm@sha256:b799f51f31710249a758e02aef48ea81fea35550f7ec051ec1c72f88c5f482b6`.
- Architecture: 64-bit ARM (ARM64 — the Dragon host architecture).
- Runtime: vLLM `0.20.1+7124b12a.dev`, PyTorch
  `2.12.0a0+5aff3928d8.nv26.05`, Compute Unified Device Architecture
  (CUDA — NVIDIA's GPU programming/runtime platform) `13.2`, Triton `3.6.0`.
- Serving contract: BF16 weights, BF16 Key-Value (KV — attention cache) cache,
  tensor parallel size 1, one active sequence, eager execution, text-only,
  thinking disabled, and fixed `gpu-memory-utilization=0.70`.
- Qualified context ceiling: `262144` tokens.
- Tool parser: `--enable-auto-tool-choice --tool-call-parser qwen3_coder`.
- Private endpoint: `127.0.0.1:8101`.

The aggregate runtime evidence remains under:

```text
/home/gr33nl4dy/dev/coder/research/ps-coding-assistant/dragon-cliproxyapi-validation/dragon-qwen3-8-27b-bf16-20260816/
```

## CLIProxyAPI provider configuration

The active credential-bearing configuration remains outside this repository at
`/home/gr33nl4dy/cliproxyapi/config.yaml`. Its contents must not be copied into
logs or documentation. The credential-free provider excerpt is
[`config.example.yaml`](config.example.yaml), SHA-256
`23b58b433da029ae4498fb13597536f4bde397e8a0bbb21b0fb86e688a3c50e6`.

Configured identities:

- provider name: `dragon-qwen3-8-bf16`;
- upstream model: `Qwen3.8-27B`;
- CLIProxyAPI alias: `dragon-qwen3-8-27b-bf16`;
- gateway alias: `claude-gateway-dragon-qwen3-8-27b-bf16`;
- Claudex selection: `claude-gateway-dragon-qwen3-8-27b-bf16[1m]`.

`force-mapping: true` keeps the client-visible alias stable. The configured
`max-context-length: 262144` records the measured ceiling. Input and output
modalities are text-only.

Claudex expresses its highest intent as `xhigh`. The OpenAI-compatible vLLM
endpoint accepts only `none`, `low`, `medium`, or `high`. The explicit
`thinking.levels: [high]` model capability therefore clamps `xhigh` to `high`
before the request reaches vLLM. This is request compatibility only; the
Dragon chat template remains thinking-disabled.

Configuration custody:

- original pre-provider backup:
  `/home/gr33nl4dy/cliproxyapi/config.yaml.backup-20260816-2022`;
- pre-effort-map backup:
  `/home/gr33nl4dy/cliproxyapi/config.yaml.backup-20260816-2037-before-effort-map`;
- original SHA-256:
  `f6020892b5e676463ab6ef9556a54f9d7adfd7ef8260390b175978bb79b668e5`;
- post-provider SHA-256:
  `63363925e120381428e0facacb2b337a86b857b88c5b5a0c145ddb4d78c126d4`;
- final effort-map SHA-256:
  `4ae7d02ae6542379b76bb8edc0c380047198666da0eba937716e7402807a1d21`;
- active mode corrected from `0644` to `0600` and retained at `0600`.

## Loopback tunnel

The installed user unit is:

```text
/home/gr33nl4dy/.config/systemd/user/dragon-qwen3-8-tunnel.service
```

The repository copy is
[`dragon-qwen3-8-tunnel.service`](dragon-qwen3-8-tunnel.service), SHA-256
`f370d92f67459303c177cc772f41db1d242ecc139abc134f678f6ebc6f2e3344`.
It is byte-identical to the installed unit.

The tunnel binds only `127.0.0.1:18101` and forwards to Dragon
`127.0.0.1:8101`. It uses `autossh`, batch authentication, strict host-key
checking, keepalives, and `ExitOnForwardFailure=yes`.

## Gateway alias compatibility

The earlier `API Error: 400 unknown gateway model` was caused by exact alias
lookup when Claude Code selected a model with the optional `[1m]` suffix and by
a catalogue race immediately after gateway restart. The local gateway now:

1. removes `[1m]` for internal alias lookup;
2. refreshes aliases lazily when the first request arrives before catalogue
   discovery;
3. forwards only authorization headers during internal discovery;
4. logs catalogue metadata without credentials.

Those gateway changes and their rollback are documented in
`../upgrade-2026-08-16-v7.2.133.md`. Fresh base and `[1m]` probes are included in
the verification matrix below.

## Tool-call finish-reason compatibility

The Qwen parser emits a valid non-empty OpenAI `message.tool_calls` array, but
this vLLM build reports `finish_reason: "stop"` instead of
`finish_reason: "tool_calls"`. Before the local fix, CLIProxyAPI correctly
translated the content into an Anthropic `tool_use` block but separately mapped
`stop` to `end_turn`. The result was semantically inconsistent:

```text
content: tool_use
stop_reason: end_turn
```

The local fix is applied at the OpenAI-compatible executor boundary rather than
as a standalone translator modification:

- non-streaming responses are inspected immediately before response
  translation;
- only an exact `finish_reason: "stop"` with a non-empty
  `message.tool_calls` array is normalized to `tool_calls`;
- `length`, `content_filter`, missing calls, empty calls, and non-array calls are
  preserved;
- Server-Sent Events (SSE — streamed HTTP events) are unchanged because the
  existing streaming translator already derives `tool_use` from an emitted tool
  block.

Reproducible patch:

```text
local/patches/v7.2.133-local-dragon-qwen3-8-tool-finish-reason.patch
```

Patch SHA-256:
`f26dfb07c59838ac0e16313c87b238ac2de5b61ede9e8d1f289ba7f4a44c7c14`.
The patch applies exactly after
`local/patches/v7.2.133-local-account-limits-daybreak.patch` on official
`v7.2.133` commit `7efe0a7c11adfecb7fe82f2d1128f873a9f4cbf3`.

Installed build:

- version: `7.2.133-local.account-limits.daybreak.3`;
- commit marker:
  `7efe0a7c11adfecb7fe82f2d1128f873a9f4cbf3+local.dragon-tool-finish`;
- built at: `2026-08-16T09:32:56Z` (`2026-08-16 21:32:56 NZST`);
- binary SHA-256:
  `bdc93fb00ef13c27c121635f23eed672877663df8582e172f87a254aa2ddd9b0`;
- binary path: `/home/gr33nl4dy/cliproxyapi/cli-proxy-api`;
- rollback binary:
  `/home/gr33nl4dy/cliproxyapi/backups/cli-proxy-api.pre-dragon-tool-finish.20260816-213318`;
- rollback SHA-256:
  `d5fd473507ab8ad85a33850c01a0494b1cb0196e4b79e21ede052c037848d9cc`.

The source checkout's `main` branch remains on its existing older local line.
Deployment was therefore built from a project-owned isolated tree containing
official `v7.2.133` plus the two recorded local patches, not from the older
checkout state.

## Verification record

Test-Driven Development (TDD — failing regression first, then the minimal
implementation) evidence:

- exact local `v7.2.133` pre-fix regression:
  `stop_reason = "end_turn", want "tool_use"`;
- positive normalization regression: PASS after the fix;
- preservation regression for `finish_reason: "length"`: PASS;
- executor helper package: PASS;
- all `TestOpenAICompatExecutor*` tests: PASS;
- Claude translator package: PASS and unchanged;
- required `go build -o test-output ./cmd/server && rm test-output`: PASS;
- patch dry-run against exact `v7.2.133` plus the existing local patch: PASS;
- independent read-only code review: no high-confidence findings.

A broader executor-package baseline has three pre-existing fingerprint
expectation failures. They expect `X-Stainless-Os = "Linux"` while the current
upstream default emits `"MacOS"`. The implementation and tests are outside this
patch and remain unchanged:

- `TestApplyClaudeHeaders_DisableDeviceProfileStabilization`;
- `TestApplyClaudeHeaders_LegacyModePreservesConfiguredUserAgentOverrideForClaudeClients`;
- `TestClaudeExecutor_NonClaudeRequestUsesClaudeCode220CLIFingerprint`.

Final sanitized end-to-end results are recorded in
[`verification-summary.json`](verification-summary.json). Verdict: **PASS**.

- provider and gateway catalogue entries: present;
- base and `[1m]` gateway aliases: HTTP 200, marker matched, usage present,
  `end_turn`, zero thinking blocks;
- direct OpenAI tool response: HTTP 200, one matching valid structured call,
  normalized `finish_reason: "tool_calls"`;
- direct CLIProxyAPI Anthropic response: HTTP 200, one matching `tool_use`,
  `stop_reason: "tool_use"`, zero thinking blocks;
- gateway Anthropic response: HTTP 200, one matching `tool_use`,
  `stop_reason: "tool_use"`, zero thinking blocks;
- streamed gateway tool response: one tool start, one input fragment, one
  message stop, terminal `tool_use`, zero thinking starts;
- synthetic tool-result continuation: HTTP 200, marker matched, zero additional
  tool calls, `end_turn`, zero thinking blocks;
- isolated Claudex smoke from `/home/gr33nl4dy/dev/coder`: exit `0`, success,
  one completed turn, marker and usage present, active model
  `claude-gateway-dragon-qwen3-8-27b-bf16[1m]`, zero initialized tools, and zero
  connected Model Context Protocol servers;
- raw material stored: false;
- tool execution: false.

The tool aggregate was written at `2026-08-16 21:40:59 NZST`; the Claudex
aggregate was written at `2026-08-16 21:41:24 NZST`. They were observed at
`2026-08-16 21:52:01 NZST`, about eleven minutes later, so their file stamps
were stale by the five-minute console-reporting threshold even though both were
created by this deployment run.

## Service state after cutover

At `2026-08-16 21:54:48 NZST`:

- `cliproxyapi.service`: active/running, PID `825435`, restart count `0`, source
  start `2026-08-16 21:33:44 NZST`;
- `claudex-gateway.service`: active/running, PID `825462`, restart count `0`,
  source start `2026-08-16 21:33:44 NZST`;
- `dragon-qwen3-8-tunnel.service`: active/running, PID `601129`, restart count
  `0`, source start `2026-08-16 20:22:03 NZST`;
- one listener each on `127.0.0.1:8317`, `127.0.0.1:8318`, and
  `127.0.0.1:18101` was observed live.

The proxy/gateway service-start stamps were about 21 minutes older than the
observation and therefore stale as freshness indicators, although their active
state and zero restart counts were observed live. The tunnel start stamp was
about 93 minutes old and likewise stale; its active state, zero restart count,
and listener were observed live.

## Rollback

Binary rollback:

```bash
install -m 755 \
  /home/gr33nl4dy/cliproxyapi/backups/cli-proxy-api.pre-dragon-tool-finish.20260816-213318 \
  /home/gr33nl4dy/cliproxyapi/cli-proxy-api
systemctl --user restart cliproxyapi.service claudex-gateway.service
```

Configuration rollback must preserve mode `0600` and should use the narrowest
relevant backup:

- remove only the effort map with
  `config.yaml.backup-20260816-2037-before-effort-map`;
- remove the complete Dragon provider with `config.yaml.backup-20260816-2022`.

After any configuration rollback, verify the SHA-256 and mode before restarting
services. Do not print the credential-bearing file.

Tunnel rollback:

```bash
systemctl --user disable --now dragon-qwen3-8-tunnel.service
```

Use tunnel rollback only when intentionally removing Dragon access; it is not
required for a binary-only rollback.

## Dragon runtime recovery note

`/home/gr33nl4dy/bin/drop-cache` reclaims Linux page cache but does not release a
retained CUDA allocation. The proven recovery was a full NVIDIA module cycle,
with exact holder validation and restoration:

- unload: `nvidia_uvm`, `nvidia_drm`, `nvidia_modeset`, `nvidia`;
- reload: `nvidia`, `nvidia_modeset`, `nvidia_drm`, `nvidia_uvm`.

All Dragon GPU work and any future module cycle remain assigned to
`codex@gpu`. Never use broad process kills, Docker-wide cleanup, wildcard model
deletion, or inferred holder substitution.
