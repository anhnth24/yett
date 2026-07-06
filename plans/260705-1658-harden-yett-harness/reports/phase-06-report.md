# Phase 6 Implementation Report — Policy Gate Defense-in-Depth

> NOTE: the harness's Write-tool sandbox refused a write to the main-tree path
> `D:\FIS\ai-first\yett\plans\260705-1658-harden-yett-harness\reports\phase-06-report.md`
> (same isolation behavior Phase 4's agent hit). This report is saved in the worktree's own
> `plans/reports/` instead; the full content is also in my final chat message. The orchestrator
> should copy this file to the intended path if needed.

## Executed Phase
- Phase: phase-06-policy-gate-defense-in-depth
- Plan: `D:\FIS\ai-first\yett\plans\260705-1658-harden-yett-harness`
- Worktree: `D:\FIS\ai-first\yett\.claude\worktrees\agent-a33cb2eb14f1cf611`
- Branch: `worktree-agent-a33cb2eb14f1cf611`
- Commit: `9f1b3dd` fix(policy): re-gate mutated args, enforce subagent toolset, canonical immutable paths
- Status: **DONE**

## Files Modified
- `src/yett/policy/immutable.py` (+87/-14 net): split `write_file` (canonicalize `args['path']` + `is_relative_to`) from `exec`/`ssh_exec` (shlex-parse operands, basename fail-closed match against protected names); threading-hint comment updated for the Phase 4 handoff.
- `src/yett/tools/wiring.py` (+69/-6): `execute_tool` gains explicit `allowed_tools` param (subset enforcement before Gate); PreToolUse mutation triggers a full re-gate (deny stays denied, need_approval asks again with mutated args, approver-absent denies); PostToolUse mutated result is re-filtered before return; new `_gate_args`/`_resolve_db_driver_hint` thread the real DB profile driver into the Gate-time check when `DbQueryTool` is already registered.
- `src/yett/core/loop.py` (+8): `allowed_tools` (already accepted by `run_turn`) now also flows into `_execute_tool_call` → `execute_tool` at both call sites (resume-pending batch + main loop), not just into schema filtering.
- `src/yett/rpc/broker.py` (+9): `RpcSession` gains an `allowed_tools: set[str] | None = None` field, enforced independently of whatever the bound `handler` does (defense-in-depth, no reliance on closures remembering to restrict).
- `src/yett/tools/remote/ssh_exec.py` (+27/-3): `_path_allowed` rewritten to normalize the remote path lexically (`PurePosixPath` + `posixpath.normpath`, no local `Path.resolve()`) and match glob patterns only against the filename component, not the whole path.
- `src/yett/security/allowlist.py` (+29/-6): `_args_match` uses `re.fullmatch` instead of `re.search`; `path`/`cwd` arg values are lexically normalized (`posixpath.normpath` after `\`→`/`) before matching.
- `src/yett/policy/schema.py` (+17/-4): `Match.matches` gets the same fullmatch + path-normalize treatment (separate small helper, not imported from `allowlist.py`, to avoid a security→policy import for one line).
- `config/harness.example.yaml` (+4/-1): the sample `exec` allowlist rule's `cmd` pattern updated to `^(git|ls|cat|grep|python|pytest|npm|go)(\s.*)?$` so it still matches under fullmatch semantics.
- `tests/unit/test_phase3.py` (+283): 11 new/updated tests (see below).
- `tests/unit/test_ssh_gate.py` (+51): 3 new tests for `_path_allowed`/`LogReadTool` containment.
- `tests/unit/test_security_basic.py` (+29): 2 new tests for allowlist anchoring + path normalize; 1 existing pattern updated.
- `tests/unit/test_tools_wiring.py` (+4/-2): **1 line changed outside my nominal ownership list** — see "Ownership deviation" below.

## Tasks Completed

### 1. P1-8/RT-4 — immutable path, write_file vs exec split
`ImmutableCore.check` now branches: `write_file` → `_write_file_hits_protected` (canonicalize `args['path']` via `Path.resolve()`, compare `== ` or `is_relative_to` against each protected path); `exec`/`ssh_exec` → `_exec_hits_protected` (shlex-split the command, skip the program name and `-`-flags, basename-match remaining operands against protected filenames — fail-closed per the phase's own accepted design, since cwd is unknown for either local exec or remote ssh_exec). Shlex parse failure → fail-closed deny.
Tests (`test_phase3.py`): `test_immutable_protects_policy_file_via_exec_relative_path` reproduces the exact `sed -i policy.yaml` vector for both `exec` and `ssh_exec`, plus a negative case (`sed -i other.txt` still allowed); `test_immutable_write_file_uses_canonical_path_not_substring` proves a decoy filename (`policy.yaml.bak.notes.txt`) is no longer falsely blocked by the old substring check.

### 2. P1-10/RT-8 — subagent toolset subset enforced at execute
`execute_tool` takes `allowed_tools: set[str] | None = None`; checked first (`name not in allowed_tools` → deny with `TOOLSET_SUBSET`), before the Gate — this is a structural boundary, not a policy decision. `core/loop.py`'s `_execute_tool_call` now accepts and forwards `allowed_tools` (loop's `run_turn` already had the param for schema-filtering; now it also reaches execution). `rpc/broker.py`'s `RpcSession` got its own `allowed_tools` field, enforced independently of the `handler` closure (defense-in-depth — no assumption the handler already restricts).
Tests: `test_execute_tool_denies_outside_allowed_tools`, `test_loop_enforces_allowed_tools_at_execute` (real `AgentLoop` + `FakeProvider`, proves the side-effect tool never runs despite Gate allowing it), `test_rpc_broker_denies_tool_outside_subagent_subset` (proves the bound `handler` is never even called).

### 3. P1-11 — PreToolUse re-gate (user-verified scenario)
After a PreToolUse hook mutates args, `execute_tool` re-runs `safe_evaluate` on the mutated args. `deny` → `[DENIED]` immediately. `need_approval` → approver is called **again** with the mutated args (a fresh local variable, never reusing the original `approved`); approver absent → deny fail-closed.
Tests: `test_pretooluse_hook_mutation_to_dangerous_cmd_is_regated` reproduces the user's exact snippet (Gate allows `echo safe`, hook mutates to `rm -rf /`, re-gate now denies via the immutable hardline instead of running it). `test_pretooluse_hook_mutation_requires_fresh_approval_not_reused` proves the approver is called exactly once with the mutated args (not the original), and that omitting the approver denies even though the pre-hook verdict was a plain allow.

### 4. P1-16 — PostToolUse re-filter (user-verified scenario)
`execute_tool` now re-runs `filter_apply` on the (possibly hook-mutated) result content before returning, instead of returning `mutated_result` directly.
Test: `test_posttooluse_hook_secret_injection_gets_refiltered` reproduces the user's exact snippet (hook appends a `sk-cp-...` key after the original filter ran) and asserts the leaked key is now redacted (`[REDACTED]`) in the final result.

### 5. P1-9/RT-13 — log_read containment
`_path_allowed` normalizes the (always-POSIX, remote) path with `posixpath.normpath` via `PurePosixPath` — no local `Path.resolve()`, which would apply Windows semantics on a Windows controller and touch the wrong filesystem. Absolute + no residual `..` required; glob wildcards apply only to the filename, not across `/`, closing the old `fnmatch.fnmatch(path, pattern)` full-path loose-glob bypass (`/var/log/app/../../etc/passwd.log` used to match `/var/log/app/*.log`).
Tests (`test_ssh_gate.py`): `test_log_read_path_allowed_exact_and_glob_on_filename_only`, `test_log_read_path_traversal_denied` (unit-level on `_path_allowed`), `test_log_read_tool_denies_traversal_before_touching_backend` (end-to-end via `LogReadTool.run` with a fake backend, asserting the backend is never called).

### 6. allowlist.py / schema.py anchored matching + path normalize
Both `_args_match` (allowlist.py) and `Match.matches` (schema.py) switched from `re.search` to `re.fullmatch`. `path`/`cwd` arg values are normalized with `posixpath.normpath(val.replace("\\", "/"))` before matching — **deliberately not `os.path.normpath`**: verified empirically (see below) that on Windows `os.path.normpath` rewrites `/`-separated paths to `\`-separated ones, which would break the POSIX-style project paths this harness's own example config documents (WSL2 mounts, `/mnt/d/...`). `posixpath.normpath` after a `\`→`/` pre-pass gives a deterministic, OS-independent canonical form regardless of which separator the input or the pattern used.
`config/harness.example.yaml`'s sample exec rule updated (`^(git|ls|...)(\s.*)?$`) since the old pattern had no trailing anchor.
Tests: `test_allowlist_anchor_rejects_appended_command` (bare `"ls"` pattern no longer matches `"ls; whoami"` — deliberately not `rm` so this isolates the allowlist fix from the separate hardline denylist test), `test_allowlist_path_normalized_before_match` (`/workspace/../etc/passwd` no longer satisfies a `^/workspace/.*$` rule); plus `test_policy_rule_allow`/`test_policy_engine_is_drop_in_for_wiring` (test_phase3.py) and `test_allowlist_allows` (test_security_basic.py) updated from unanchored `^echo`/`^ls` patterns to fullmatch-safe equivalents.

### 7. Phase 4 handoff — real DB driver threaded into gate-time check
`wiring.py._resolve_db_driver_hint` reads the driver **best-effort** from the already-registered `DbQueryTool`'s profile map (`getattr` all the way down, no exceptions, no I/O, no secrets) when the tool is `db_query` and a `profile` name is present. `_gate_args` merges this into a copy of args used only for the Gate call (not the real args passed to `tool.run()`, which already resolves its own driver correctly via `db_query.py`, untouched — that file is out of my ownership). When the tool isn't registered or the profile is unknown, the hint is `None` and `ImmutableCore._classify_db_query`'s existing multi-dialect fail-closed fallback (from Phase 4) is unchanged.
Test: `test_db_query_gate_uses_real_profile_driver_via_wiring` — uses a backtick-quoted identifier query (`` SELECT `id` FROM `orders` ``) that sqlglot only parses successfully under `mysql`/`sqlite`, not `postgres`/`tsql`. First asserts the **baseline** (no hint) fail-closed fallback would false-deny it (since `postgres` is tried first and fails to parse); then asserts that going through `execute_tool` with a registered `mysql` profile correctly allows it — proving the real driver is threaded through, not just re-testing the fallback.

## Ownership deviation (flagged per Rule 9 — fail loud)
`tests/unit/test_tools_wiring.py` is **not** in my assigned file-ownership list, but the anchored-matching fix (item 6) is a correctness/security fix that necessarily changes semantics for any allowlist rule that relied on unanchored `search()` — including that file's `test_exec_runs_when_allowed`, which used `arg_patterns={"cmd": r"^echo"}` against `cmd: "echo hello"`. Under `fullmatch`, that pattern no longer matches (no trailing `$`/`.*`). I could not fix this by touching only files I own, so I made the smallest possible change: updated the pattern to `r"^echo hello$"` (1 line + 1 comment), preserving the test's original intent ("a matching allow-rule lets the tool run") without inventing new matching semantics elsewhere. I verified (via `grep`) that Phase 7's file list does not include this test file, so there's no cross-phase merge conflict risk. Flagging this explicitly rather than leaving the suite red or silently weakening the security fix.

## Tests Status
- Type check (`python -m mypy src`): **pass** — "Success: no issues found in 104 source files".
- Ruff (`python -m ruff check src tests`): **pass** — "All checks passed!".
- Import-linter (`lint-imports`): **pass** — "Contracts: 2 kept, 0 broken."
- Unit/integration tests (`python -m pytest -q`): **325 passed, 4 skipped** (docker, no daemon), **3 failed** — all 3 failures are pre-existing and unrelated to this phase (see below), verified via `git stash`/rerun on the pristine pre-Phase-6 tree (identical `PermissionError: [WinError 10013]` reproduces without any of my changes).

### Pre-existing failures (not mine, not touched, verified reproducible without my diff)
`tests/integration/test_web_approval.py::test_web_approval_approve`, `::test_web_approval_reject`, `tests/integration/test_web_ui.py::test_web_ui_health_chat_index` — all fail with `PermissionError: [WinError 10013] An attempt was made to access a socket in a way forbidden by its access permissions` when `ThreadingHTTPServer` tries to bind a port in this sandboxed environment. [Unverified] whether this is a permanent constraint of this specific worktree/session or transient — I did not investigate further since `web/` is explicitly out of my file ownership (owned by other phases) and the failure is a socket-bind permission issue in the sandbox, not a logic bug. Baseline task description said "314 passed, 4 skipped" before my changes; 314 + 4 = 318, and 318 + 14 (new tests I added) = 332 = 325 passed + 3 failed + 4 skipped observed now, confirming no regressions — only these 3 pre-existing environment-blocked tests plus my additions.

## Issues Encountered
1. **Worktree path mixup (self-caught, fixed before any Write landed on the wrong tree):** my first `Read`/`Write` attempts used `D:\FIS\ai-first\yett\...` (main checkout) instead of the worktree path. The main tree happened to be at the same commit (`3975c49`) so reads were accurate, but the `Write` tool correctly refused writing to the shared path. Re-read and re-wrote everything under `D:\FIS\ai-first\yett\.claude\worktrees\agent-a33cb2eb14f1cf611\...` from that point on; verified via `git diff --stat` that only worktree files changed.
2. **`os.path.normpath` cross-platform trap (self-caught before committing):** initially normalized `path`/`cwd` args with `os.path.normpath`, then wrote a test with a POSIX-style pattern (`^/workspace/.*$`) and a POSIX-style legitimate path (`/workspace/notes.txt`) — the test failed because on Windows `os.path.normpath` rewrites `/` to `\`, breaking the POSIX pattern even with no traversal involved. Verified empirically via a throwaway `python -c` check, then switched both `allowlist.py` and `schema.py` to `posixpath.normpath(val.replace("\\", "/"))`, which is separator-agnostic regardless of host OS — matches this harness's own documented use of WSL2/POSIX-style project paths on a Windows controller.
3. **Test confounding (self-caught before committing):** first version of the anchor-bypass regression test used `"ls; rm -rf /"` as the injected command, which the pre-existing hardline denylist (unrelated to my change) already denies for its own reason (`DENY_EXEC`), masking whether my allowlist fix actually did anything. Reran, saw the wrong `rule_id`, and switched to `"ls; whoami"` (harmless, not hardline-denied) so the test isolates the allowlist-anchor fix specifically.
4. **Empirically verified dialect-divergence for the driver-threading test:** picked the backtick-identifier SQL example by actually running `classify_sql` across all 4 dialects first (via `python -c`) rather than assuming sqlglot's behavior, confirming `postgres`/`tsql` reject it and `mysql`/`sqlite` accept it — this is what makes the regression test meaningfully prove driver-threading works rather than just re-testing the existing fallback.

## Next Steps / Unresolved
- None blocking. The Phase 4 handoff note's "structurally possible" caveat is satisfied via a defensive `getattr`-based read of the already-registered `DbQueryTool`'s profile map — this is coupling to a private attribute (`_profiles`) since `tools/db/` is out of this phase's ownership and I could not add a public accessor there; if `db_query.py` is ever refactored to rename/restructure that attribute, `_resolve_db_driver_hint` silently falls back to `None` (safe — multi-dialect fail-closed fallback still applies) rather than breaking.
- Per the task's final instruction, the orchestrator will run `/codex:adversarial-review` (or `code-reviewer` per the plan's Phase 7 resume note about Codex quota) on this diff next.

Status: DONE
Summary: Closed the immutable-path exec-vector bypass, wired explicit subagent toolset enforcement into both the loop and the RPC broker, added PreToolUse re-gate and PostToolUse re-filter (reproducing both user-verified bypass snippets with regression tests), fixed log_read's traversal-prone loose glob, anchored allowlist/schema matching with path normalization, and threaded the real DB driver into the gate-time check per the Phase 4 handoff — full pytest green (325 passed/4 skipped, 3 pre-existing unrelated environment failures), mypy strict clean, ruff clean, import-linter clean.
Concerns/Blockers: One file outside my nominal ownership (`tests/unit/test_tools_wiring.py`) required a 1-line pattern update to stay green after the anchored-matching fix — flagged above with justification; no other deviations.
