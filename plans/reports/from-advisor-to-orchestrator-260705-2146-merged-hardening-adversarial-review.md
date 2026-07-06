# Adversarial review — merged hardening (Phases 1-5 + review fixes)

- **Reviewer:** code-reviewer advisor (internal; substitutes for /codex:adversarial-review — codex out of quota)
- **Diff:** `2cc89a8..3975c49` (11 commits, ~2100 insertions / 34 files)
- **State at review:** pytest 314 passed / 4 skipped (docker no-daemon), mypy clean, ruff clean, import-linter 2 kept
- **Verdict:** SOUND on primary invariants. **0 Critical, 0 High.** 4 Medium + 4 Low = defense-in-depth / correctness-guarantee gaps the happy-path + FakeProvider suite doesn't reach. Phase 6/7 can build on top.

## Verified holding (empirically, not assumed)
- P0-3 system prompt reaches provider (`openai_compat.py:76`, `context.py:122`).
- P0-2 tool_use ordering valid incl. after prune; assistant(tool_calls) before tool_result (`loop.py:169,174-184`, `context.py:88-115`).
- Idempotency signature is **sha256** not builtin `hash()` (`context.py:21-26`) — stable across resumed process.
- SSH delete hardline fail-closed: eval/exec/perl/nested-`$()` → OTHER → default-deny (`cmdguard.py:210`).
- SQL read-only enforced with correct driver in `db_query.run()` (`db_query.py:54`); CTE-DELETE / SET GLOBAL / COPY…FROM PROGRAM / `/*!*/` all deny; fail-closed on parse error.
- Docker fail-closed via `probe_docker()` (`app.py:132`), no host fallback; `--network none`; hardening flags present (`docker.py:87-102`); `_container_user` maps host UID on POSIX; cwd host→container scope-checked + longest-prefix mapped, raises if unmapped (`app.py:86-95`).
- Secrets: keyring fails loud on missing lib (`backends.py:57-65`); `resolve.py` no silent plaintext fallback; filters redact recursively + truncated PEM.

## MEDIUM

**M1 — `denylist.check_exec` regex-only, quote/interpreter-bypassable; it is the SOLE delete-hardline for the local `exec` tool** (cmdguard only runs for `ssh_exec`). `security/denylist.py:21-31,60`.
- `'rm' -rf /workspace` and `eval 'rm -rf /workspace'` → denylist NONE (quote isn't a boundary char).
- `python -c "import shutil; shutil.rmtree('/workspace/proj')"` → ALLOWED by shipped example allowlist (`harness.example.yaml:133-134`) and executes.
- Mitigation keeping it Medium: Docker read-only rootfs + `--cap-drop ALL` contains OS-file damage; workspace deletion already within exec envelope. Escalates under LocalSandbox (dev-only) or broadened allowlist w/o Docker.
- Fix: shlex-tokenize in `check_exec` (mirror cmdguard `_basename` head-check) and/or run `cmdguard.classify` for `exec` in BasicGate. **→ Phase 6.**

**M2 — ImmutableCore is inert in the running app.** `app.py:164` wires `BasicGate`; `PolicyEngine` (only consumer of ImmutableCore) is never instantiated in `src` (grep-verified). So Phase 4's multi-dialect SQL fallback and immutable protected-path checks do NOT run. Live SQL enforcement is db_query's own (correct → no leak), but the "immutable core cannot be overridden" invariant is currently NOT satisfied by merged code. Consistent w/ Phase 6 deferral (Phase 6 wires it) — not a regression, but the green suite must not be mistaken for ImmutableCore being active. **→ Phase 6 (already in scope).**

**M3 — Resume is at-least-once on hard crash, not never-re-run.** `loop._execute_tool_call` (`loop.py:232-245`) runs effect → add_tool_result → ckpt.save. Hard crash (kill -9/OOM/power) between effect and checkpoint → resume re-issues the call → double execution. Cooperative cancel is safe (only crash-only). Also `consult_cache=is_resume` (`loop.py:183`) stays true for the whole resumed turn → a NEW same-tool+args call after the resume point served stale from cache. Fix: persist an "intent" checkpoint before effect for non-idempotent tools; scope consult_cache to the pre-crash pending batch. **→ document true guarantee; decide in Phase 6.**

**M4 — `odbc_dsn_pwd` filter leaks quoted/braced ODBC passwords.** `security/filters.py:25` value class `[^;'"\s]+` stops at quote/semicolon. `Pwd='sec;ret'` → not redacted at all; `Pwd={p@ss;word}` → only `{p@ss` redacted. Vector: driver exception echoing the connection string (`db_query.run()` doesn't catch non-UserFacingError, `db_query.py:58`). Primary defense (resolve-at-point-of-use) still holds → backstop gap. Fix: allow quoted/braced values (redact through matching quote or `{...}`). **→ small fix, filters.py (merged, unowned).**

## LOW
- **L1** `journalctl` in `_READONLY_BINS` w/o sub-command restriction (`cmdguard.py:37-39`) → `journalctl --vacuum-time=1s`/`--rotate` classify READONLY → ALLOW on ssh_exec, deletes journal data. (docker/systemctl/kubectl ARE sub-command-gated.) **→ Phase 6/7 cmdguard.**
- **L2** `EXPLAIN(SELECT 1)` (no space) → infinite self-recursion in `classify_sql` (`sqlguard.py:87-92`): strip regex needs `explain\s+`. RecursionError caught → deny (fail-closed) BUT ~980 sqlglot parse calls + log spam per query. Mild DoS/log flood. Fix: strip `explain` without requiring trailing whitespace, or depth-guard. **→ small fix, sqlguard.py (merged, unowned).**
- **L3** Truncated-PEM fallback (`filters.py:39-42`, `.*` DOTALL to EOS) over-redacts legit content after a headerless "BEGIN…PRIVATE KEY" line — availability cost, safe direction. `generic_sk_key` needs ≥20 chars after `sk-` (short keys leak — acceptable for real keys).
- **L4** `SandboxCfg.network` allows `"proxy"` (`config/models.py:52`); default `none` keeps no-egress but `--network proxy` just names a docker network — operator creating a bridged net named "proxy" → full egress. No enforcement it's actually restricted. **→ document controlled-egress mode.**

## Top-3 to address before/within Phase 6
1. Wire ImmutableCore into the active gate (M2) — Phase 6 already scheduled.
2. Harden `denylist.check_exec` / apply cmdguard.classify to `exec` tool (M1).
3. Decide + document the true resume guarantee (M3); at-least-once is fine if stated.

Nothing contradicts the green suite — all gaps are outside happy-path + FakeProvider reach.
