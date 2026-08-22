# Dragon Qwen3.8-27B context ceiling

Verdict: READY_FOR_LOCAL.

## Current verified run: 2026-08-16

The prior retained-CUDA-allocation hold was cleared by the explicitly scoped
full NVIDIA module unload/reload after a fresh no-workload inventory. The
operator display command \`nvtop\` was treated correctly as display-only: it was
validated by exact PID/owner/executable, temporarily quiesced only around each
authorized module cycle, and restored in its original tmux pane. No training,
evaluation, model, unrelated process, protected service, or protected port was
stopped.

The exact pinned Qwen3.8-27B official BF16 Safetensors runtime remained
unchanged: pinned ARM64 vLLM image, BF16 weights and KV cache, TP1, one active
sequence, eager mode, text-only, thinking disabled, fixed 0.70 utilization, and
private \`127.0.0.1:8101\`. The controlled 32768 service-restart gate passed
after recovery. The context staircase then passed at 32768, 49152, 65536,
98304, 131072, 163840, 196608, 229376, 245760, and 262144 tokens. Every rung
reached health 200, accepted the sanitized long-input probe, remained healthy,
and reported no container out-of-memory condition. The final 262144-token
candidate is still loaded.

The first 98304 attempt was discarded as an infrastructure-status false
negative: Docker returned a nonzero create status while the exact candidate
was running; a direct retry returned success and passed the rung. It is not a
context failure. The authorized maximum tested by this run is 262144 tokens.

The durable operational rule is in \`DRAGON-RUNTIME-NOTES.md\`: \`nvtop\` is a
display client, not a competing workload, while still being an exact device
holder that may need temporary PID-specific quiescence for an authorized
driver cycle. The \`drop-cache\` script remains classified as Linux page-cache
reclamation only; it does not release retained CUDA allocations.

The operator-reported Dragon `drop-cache` procedure was verified as a Linux page-cache drop. The fresh post-recovery baseline at 2026-08-16 17:49:21 NZST found no training, evaluation, or co-resident model; protected ports were unchanged; 8100 and 8101 were clear; and approximately 127.3 GiB of 127.5 GiB CUDA memory plus 128.8 GB host memory were available. The exact BF16 model artifact and deployment manifest remained retained.

The pinned runtime microgate passed for NVIDIA Thor, compute capability 11.0, CUDA 13.2, vLLM 0.20.1+7124b12a.dev, Triton 3.6.0, and the required CUDA target. A corrected disposable 32768-token candidate then passed health, exact identity, non-streaming, streaming, cancellation recovery, bounded invalid/malformed/oversized requests, and privacy checks with request logging disabled. The only launch correction was an executable candidate-only Triton cache mount after a non-executable tmpfs cache could not map Triton's compiled shared object.

The required controlled user-service restart failed at 2026-08-16 17:59:47 NZST after the manual canary stopped. vLLM reported 34.28/122.82 GiB free while the fixed 0.70 GPU-memory-utilization setting required 85.98 GiB. The page-cache drop restored host memory before the canary but did not release this retained CUDA allocation after model shutdown. READY_FOR_LOCAL was not established, so no context-rung request or final qualification was attempted.

At 2026-08-16 18:08:55 NZST, the earlier narrow GPU-0 reset was attempted after a fresh no-workload check and was refused because other device clients remained. The later authorization explicitly named `statd`, the three `jtop` monitors, `nvtop`, and `nvargus-daemon` for a bounded reversible window. A fresh pre-state at 18:41:37 NZST confirmed no training, evaluation, model process, running container, or protected listener.

The first named-holder window at 18:43:14 NZST stopped and restored the three named systemd services, but an exact `nvtop` guard rejected an abbreviated username and therefore left `nvtop` running; no unapproved process was touched. A corrected window at 18:44:04 NZST validated `nvtop` by exact process identity, terminated it with TERM, stopped only `nvargus-daemon.service`, `jtop.service`, and `statd.service`, and confirmed no `/dev/nvidia*` holder at reset time. The GPU reset still returned exit 255 because another device client remained. `nvargus-daemon`, `statd`, `jtop`, and `nvtop` were restored to their pre-state by 18:44:10 NZST.

The fresh post-restore check at 18:44:45 NZST found the named holders active, no compute application, no candidate/model process, no running container, and protected ports clear. NVIDIA Thor reported unavailable memory fields through `nvidia-smi`; the last exact pinned microprobe before the window measured 3,974,664,192 free CUDA bytes. The exact disposable candidate container and user unit were already cleaned, so the post-restore controlled 32768-token service-restart gate was not rerun and `READY_FOR_LOCAL` was not established.

Cleanup passed: the exact candidate container and user unit are absent/inactive, ports 8100 and 8101 are clear, no matching model process remains, the generated candidate Triton cache was removed from its exact path, and the model artifact and deployment manifest remain. CLIProxyAPI was not changed; no tunnel, generation traffic, raw prompt, completion, or tool argument was created or stored.

Next action: keep the final candidate private and unchanged for local readiness
use. Do not configure CLIProxyAPI, create a tunnel, expose a public/LAN
listener, tune precision or utilization, or stop processes by inference.

## Structural tool-parser validation — 2026-08-16 NZST

The pinned vLLM runtime accepted the exact parser `qwen3_coder`. Its resolved
`Qwen3CoderToolParser` class loaded against the mounted Qwen3.8 tokenizer, whose
tool-call, function, and parameter markers matched the parser. The persisted
container uses the required feature pair
`--enable-auto-tool-choice --tool-call-parser qwen3_coder`; all other serving
arguments were compared against the fixed deployment contract and passed.

Aggregate local checks passed: health and exact identity; non-stream and stream
text; one required named tool structure; auto-parser streaming with five tool
deltas and five argument fragments reassembled as one valid JSON object; a
synthetic tool-result continuation with no tool execution; privacy; and a
post-restart parser smoke. The endpoint remained private at `127.0.0.1:8101`,
with one listener and protected ports clear.

The parser container was first corrected to preserve the prior `/tmp` and
`/root/.cache` tmpfs mounts. The later retained-CUDA-allocation restart holds
were cleared by the already authorized exact NVIDIA module lifecycle recovery.
The final controlled restart requested at 21:00:07 NZST reached health HTTP
200 at 21:01:25 NZST. The active service and model remain loaded; the exact
failed temporary container record was removed, while the explicitly named
stopped pre-parser rollback container remains available. Full aggregate
details are in `PARSER-VALIDATION.md`.
