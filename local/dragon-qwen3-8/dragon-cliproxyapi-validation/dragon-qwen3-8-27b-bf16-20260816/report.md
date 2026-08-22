# Dragon Qwen3.8-27B BF16 replacement

Verdict: HOLD.

The exact retained Qwen3-Coder-Next artifact was removed under the guarded deletion checks. The official `Qwen/Qwen3.8-27B` revision `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0` was downloaded as unquantized BF16 safetensors, all 19 live large-file hashes passed, and the final artifact remains at the pinned path. The pinned ARM64 vLLM image and Triton/CUDA microgate passed.

The private manual canary on `127.0.0.1:8101` passed health, exact identity, bounded text completion, streaming, cancellation, bounded errors, 8K/16K/32K probes, privacy, and protected-port checks. Tool execution was not performed; automatic tool choice was disabled and no exact Qwen3.8 parser was verified.

The mandatory service-restart gate failed. On restart, vLLM saw 32.52 GiB free of 122.82 GiB, below the 0.70 startup target of 85.98 GiB. A bounded cooldown did not restore headroom. The candidate container and service unit were removed exactly; port 8101 and model processes are clear. The verified model and deployment manifest were retained. No CLIProxyAPI or unrelated service was changed.

Next action: perform an operator-approved host CUDA/GPU memory recovery, then rerun the same pinned candidate and repeat service-restart readiness. Do not configure local CLIProxyAPI until that gate passes.
