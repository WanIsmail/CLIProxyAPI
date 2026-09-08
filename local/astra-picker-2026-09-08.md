# Astra 1M picker repair — 2026-09-08 NZST

Status: INSTALLED and verified. Gateway discovery revision 2026.09.08.2.

## Problem and change

The previous gateway only advertised the plain Astra ID. Consequently /model
had no explicit 1M choice, and selecting Astra saved the plain 200K model.
The installed discovery response adds claude-gateway-gpt-6-astra[1m] with visible
label gpt-6-astra[1m], alongside the existing plain option, only when gpt-6-astra
is available upstream. Other models do not acquire unqualified capacity claims.
Routing already strips the suffix and preserves it in responses. The launcher
already copies advertised IDs into its model allowlist and discovery cache.
No launcher, user settings, statusline code or compaction-default change is needed.

## Verification

- RED: new discovery regression failed against unchanged source, showing only
  plain Astra and Sol. Negative control without Astra retained existing IDs.
- GREEN: candidate gateway package tests and race checks pass; gateway builds.
- Existing launcher fixture suite passes, including native 200000 compaction.
- Real installed Claude Code 2.1.263 was exercised in a disposable terminal,
  isolated config and synthetic loopback upstream that refuses inference.
  The /model picker displayed both variants. Selecting the 1M option saved the
  suffixed ID and reported context_window_size=1000000 to the statusline hook.
  Native /autocompact still reported 200K. Native /context displays its effective
  compaction window, distinct from the statusline's model capacity field.
- Disposable statusline binary fixtures separately rendered 200K and 1M correctly.

Candidate and evidence: runtime/tmp/astra-picker-rug92fn_/. Real terminal check
report: ui-check/report.json. Legacy allowlist check: ui-check-legacy/report.json.
Rollback: runtime/backups/_astra-picker-20260908-1811/ with manifest.json.
Candidate binary SHA256: 4d584db411b835439c570158c1e59d09af5eb6af7d404309a7e5979328b86157.

## Activation

Wait for existing gateway connections to drain before replacing the binary and
restarting claudex-gateway.service. Preserve core proxy and user client processes.
Existing clients may retain their discovery catalogue until relaunch; do not
silently type into or restart user panes. Recheck authenticated live discovery
and binary identity after activation.

At 18:16:03 NZST, seven established gateway connections remained. Activation
is pending either an idle interval or explicit user approval to interrupt active
requests. No production source/binary, settings, user clients or services changed.
The disposable legacy-allowlist test also passed: the new discovered variant is
selectable with the previous plain-only allowlist and reports 1M to statusline.

## Installed acceptance

The user explicitly authorized proceeding with the shared gateway restart.
At 18:19:16 NZST, source, regression test and verified binary were installed;
only claudex-gateway.service restarted. New gateway PID 189621; core proxy
retained PID 1476646. The running gateway executable matches the candidate
SHA256 above. Live authenticated discovery returned both Astra IDs at
18:19:45 NZST. Installed gateway package tests passed.

A final disposable terminal used the actual installed launcher, live gateway,
isolated settings/cache, and installed statusline. Its /model picker included
Astra [1m]; selecting it saved the suffixed model in the disposable config and
the real statusline rendered 1M. No inference prompt was submitted. Evidence:
runtime/tmp/astra-picker-rug92fn_/installed-ui/report.json and terminal.txt.
User settings and user client processes were not changed or relaunched.
Existing ptt-cl retains plain Astra until its user relaunches/selects [1m].

The durable private candidate source is commit `210a7fd` on
`work/astra-1m`. That isolated repository intentionally has no remote. Its Go
suite, race suite, launcher-model suite, ShellCheck and gateway build pass. Two
pre-existing source-snapshot checks remain non-green: the output-guard test
compares its candidate path with the installed runtime path embedded in settings,
and the service test expects ignored deployment artifacts absent from the source
snapshot. The installed service-contract check passes.

To load the updated catalogue and resume ptt-cl with the expanded context:

```sh
claudex --resume ptt-cl --model 'claude-gateway-gpt-6-astra[1m]'
```

User confirmation at 2026-09-08 18:22:21 NZST: "it works". Picker/statusline repair complete.
