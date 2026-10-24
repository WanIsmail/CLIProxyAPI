# 2026-10-24 Flash maintenance journal

Timezone: Pacific/Auckland, NZDT (UTC+13).

The operator authorized reducing the generic Flash compaction default to 180K
and continuing the timeout investigation. Subsequent instructions required
durable CLIProxyAPI notes/changelog/journals and checking upstream releases
without losing local patches. No upstream upgrade or shared-service restart
was performed.

At 13:56:38 the tested 180K launcher and tab-resume assertion were installed
atomically with rollback and hashes. Policy regressions first failed at 200K; all six methods then passed at
180K. The existing launcher suites also passed. Native
2.1.310 confirmed 180K from settings at 14:02:51. Fixture setup needed an
external-import dialog response and a small input-readiness delay; this was
confined to the disposable terminal. All such processes were closed. No
existing tab received input.

The executor handshake reproduced withholding tool arguments until completion.
The prepared repair streams the current block and carries quote-repair state
across chunks. Source, tests and candidate remain in the isolated
`.worktrees/flash-stream-keepalive` worktree. Production proxy/gateway hashes
remain unchanged. The full suite retains one documented baseline failing test;
97 packages passed. Focused tests, race checks, build and vet passed.

At 14:06:14, two native clients using the installed binary each saw only a final
argument delta. At 14:06:59, two clients using the candidate each saw two
argument deltas, with the first before generation finished, and completed.
Only a synthetic upstream and fixture-file reads were used. No Dragon model
capacity was consumed. No disposable Zellij tab was created; subprocesses
were closed after each run.

v8.0.41 was inspected at pinned commit
`8289ec4c71ad914d0612c2776352b76ba3d355a6`. It includes reasoning-field and
opt-in early-keepalive fixes but still buffers tool arguments. The local quota
endpoint and transport protections are absent. The separate gateway image
history patch and runtime launcher must remain intact. No v8 qualification or
deployment is claimed.

The [detailed note](../flash-compaction-and-streaming-2026-10-24.md) records exact
hashes, patch order, remaining timeout limits, rollback and future upgrade
checks. It distinguishes installed compaction settings, prepared server repair
and source-only upstream review. No commit, merge or push was requested.
