# Qwen3.8 structural tool-parser validation

Observed on 2026-08-16 in New Zealand Standard Time (NZST). This is
aggregate-only evidence: raw prompts, completions, headers, credentials, and
tool arguments were not retained, and no tool was executed.

## Selected parser

- Pinned vLLM runtime registry: `qwen3_coder` accepted.
- Resolved class: `Qwen3CoderToolParser`.
- Exact Qwen3.8 tokenizer compatibility check: required tool-call and XML
  function/parameter markers present; parser construction passed against the
  mounted tokenizer.
- The persisted service uses the required parser feature pair:
  `--enable-auto-tool-choice --tool-call-parser qwen3_coder`.
- All other serving arguments remain exact: official Qwen3.8-27B revision,
  bfloat16 (BF16 — 16-bit floating-point format) weights and KV cache, tensor
  parallel size 1, one sequence, eager mode, text-only, thinking disabled,
  262144-token limit, fixed 0.70 utilization, and `127.0.0.1:8101`.

## Aggregate checks

- Health and exact served identity: PASS, HTTP 200.
- Non-stream text completion: PASS, HTTP 200, non-empty content, no tool call.
- Stream text completion: PASS, HTTP 200, four chunks, done marker, non-empty
  content, no tool deltas.
- Required named tool structure: PASS, HTTP 200, one matching call, valid JSON
  arguments, no prose content.
- Auto parser streaming: PASS, HTTP 200, five tool deltas and five argument
  fragments assembled into one valid JSON argument object.
- Synthetic tool-result continuation: PASS, HTTP 200, non-empty continuation,
  zero additional tool calls; no tool execution.
- Post-restart parser smoke: PASS, HTTP 200, completed stream, tool deltas,
  fragmented arguments, matching call, and valid assembled JSON.
- Privacy and exposure: PASS; Docker logging driver `none`, request/output
  logging disabled, one loopback listener, protected ports clear, no tunnel or
  public/LAN listener.

## Restart lessons retained for future runs

- The first parser container recreation omitted the working container's exact
  `/tmp` and `/root/.cache` tmpfs mounts. It exited before model initialization.
  The service definition now preserves both mounts (`rw,nosuid,nodev,size=4g`).
- A subsequent start and the first post-test controlled restart hit the known
  retained CUDA allocation after model stop. The exact authorized NVIDIA
  module unload/reload recovered headroom; only exact named holders were
  quiesced and `nvargus-daemon`, `jtop`, `statd`, and the original `nvtop`
  pane were restored.
- The final controlled restart passed: service start requested at
  21:00:07 NZST and health reached HTTP 200 at 21:01:25 NZST.
- The stopped exact rollback container
  `dragon-qwen3-8-context-ceiling-20260816-pre-parser-20260816` is retained;
  the failed temporary parser container record was removed after verification.

The service unit starts the pre-created container; the container's persisted
`Config.Cmd` is authoritative for the parser flags. Do not recreate it without
preserving the exact image, mounts, tmpfs settings, and parser feature pair.
