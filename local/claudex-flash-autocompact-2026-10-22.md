# Flash Next automatic compaction — build 2026.10.22.1

The shared Claudex launcher supplied a session-wide `--autocompact 900000` to
Flash Next as well as large-context providers. Flash Next serves 262144 tokens.
The launcher now supplies native settings with a 900000 default and a 200000
per-model window for both Flash identities:

- `dragon-qwen3.8-flash-next-nvfp4`
- `claude-gateway-dragon-qwen3-8-flash-next-nvfp4`

A model-settings table lets native named resumes, exact-session resumes,
background launches and later model switches select their own window. No
transcript scan or guessed current model is required to pick the window.
The 200000 setting leaves 62144 tokens below backend capacity. It is a window,
not a promise of compaction at exactly 200000 input tokens: native output
reserves and model-capacity clamping still apply. The gateway alias continues
to display its existing 200K client capacity; this change does not enlarge it.

Explicit `--autocompact` and `CLAUDE_CODE_AUTO_COMPACT_WINDOW` overrides remain
operator-controlled. The separate qualified `claudex-flash-next.sh` launcher's
explicit 262144-window/90%-threshold/8192-output-reserve profile is unchanged.
Other models retain the 900K default; ordinary `claude` is unchanged.

## Installation and custody

Installed `/home/gr33nl4dy/dev/cliproxyapi/runtime/claude-codex` and updated its
two regression suites after testing an isolated candidate. No service restart
is required. Runtime startup configuration, credentials, proxy binaries,
model serving and active named sessions were not modified.

Rollback files and hash manifest:
`/home/gr33nl4dy/dev/cliproxyapi/runtime/backups/flash-autocompact-20261022/`.

Reproducible delta: `local/patches/claudex-flash-autocompact-2026-10-22.patch`,
relative to the preexisting 2026.10.16.1 launcher with model-memory fixes.
Apply after the existing native-task/model-memory/background-compaction patch
stack when rebuilding the runtime. New behavioral suite:
`local/tests/test_flash_autocompact.py`, invoked with `CLAUDEX_TEST_ROOT` set
to the isolated or installed runtime directory. No real inference is performed
by these fixture tests.

## Validation

- New regressions failed against the original launcher (after correcting the
  test-only fixture key quoting), then all 6 test methods passed; subcases cover
  both model identities, named/exact/continue/background selection, other-model
  default, hooks/statusline, explicit overrides and the prompt terminator.
- Complete launcher shell regression suite passed on candidate and installed
  bytes.
- Model-memory suite: 19 tests passed.
- Tab-resume suite: 25 tests passed. Prior assertions for the removed blanket
  CLI flag were replaced with checks for the native settings table.
- Bash syntax, ShellCheck and diff whitespace checks passed.
- Native Claude Code 2.1.306 in an isolated terminal showed at approximately
  2026-10-22 02:47 NZDT: `Current setting for dragon-qwen3-8-flash-next-nvfp4:
  200k tokens (from settings)`. Canceled the read-only dialog with Escape and
  exited only this disposable session. No model inference or forced compaction.
- No Go or Dragon deployment sources changed. Go build/tests and Dragon's
  aggregate deployment gate are outside this launcher-only change; the full
  relevant launcher suites above are its validation gate.

## Active-session boundary

Already-running clients retain their session-wide CLI override. In an idle
client, native `/autocompact 200k` clears that session override and applies the
chosen setting. Otherwise the next launch/resume through `claudex` receives this
fix. Do not inject that command into a busy retry loop or interrupt unrelated
work. The separate five-minute API retry/timeout diagnosis remains unresolved;
this change alone is not claimed to repair it. Earlier suggestions that a 900K
setting necessarily bypassed capacity were incorrect: native Claude clamps the
window to its recognized model capacity.
