# Dragon runtime operations note

This note records the operational distinction that matters for the Dragon
Qwen3.8-27B deployment.

## `nvtop` is display-only

`nvtop` is the operator's GPU-activity display command. It is not a training,
evaluation, model-serving, or competing CUDA workload. It may still appear in
`/dev/nvidia*` holder checks because the display reads NVIDIA device files.

When a bounded NVIDIA module or GPU-reset cycle is explicitly authorized:

1. Validate the exact `nvtop` PID, owner, and executable (`/usr/bin/nvtop`).
2. Temporarily stop only that exact display process if its open device handles
   prevent the authorized cycle.
3. Stop only the separately named, authorized camera/monitoring services needed
   for the cycle; never infer additional holders from a name alone.
4. Perform the exact recovery and restore the named services.
5. Relaunch `nvtop` in its original operator pane and verify the holder state.

Never use broad `pkill`, `killall`, wildcard process matching, Docker-wide
cleanup, or an inferred process substitution. Preserve protected services and
ports (`8083`, `8084`, `8090`, `8091`, `8092`, and `9999`). The model endpoint
remains private at `127.0.0.1:8101`.

## Memory and cache distinction

`/home/gr33nl4dy/bin/drop-cache` performs `sync` and a Linux page-cache drop.
It can reclaim host page-cache memory, but it does not release a CUDA context,
NVIDIA driver allocation, or a stopped model's device allocation. A CUDA-visible
memory probe and a post-stop canary/restart check are required to distinguish
those cases.

## Pinned deployment facts

- Model: official `Qwen/Qwen3.8-27B` BF16 Safetensors.
- Runtime: pinned ARM64 NVIDIA vLLM image
  `sha256:b799f51f31710249a758e02aef48ea81fea35550f7ec051ec1c72f88c5f482b6`.
- Runtime settings: BF16 weights, BF16 KV cache, tensor parallel 1, one active
  sequence, eager execution, text-only, thinking disabled, and fixed
  `gpu-memory-utilization=0.70`.
- Verified context ceiling on 2026-08-16: `262144` tokens. The model was left
  loaded on the private endpoint after the final health and long-input checks.
- No CLIProxyAPI route, tunnel, public listener, or raw prompt/completion
  material is part of this validation.

## Structural tool parser

The pinned runtime accepts `qwen3_coder`, and the exact mounted Qwen3.8
tokenizer contains the parser's tool-call, function, and parameter markers.
The persisted private container therefore uses
`--enable-auto-tool-choice --tool-call-parser qwen3_coder`. The auto-tool flag
is required by this vLLM build for the selected parser to be instantiated; it
does not change the model, precision, context ceiling, sequence limit, or
network boundary. Keep the complete container command and the two tmpfs mounts
intact when recreating the service. See `PARSER-VALIDATION.md` for the
aggregate structural checks and restart lessons.

This note is operational guidance, not permission to mutate the host. A fresh
inventory and exact scoped authorization remain required before any future
module, service, or process change.
