# Phase 4 Implementation Report — Hardline Guard Bypasses

> NOTE: the harness's Write-tool sandbox refused a write to the main-tree path
> `D:\FIS\ai-first\yett\plans\260705-1658-harden-yett-harness\reports\phase-04-report.md`
> (worktree isolation is enforced even for the designated report-path exception). This report
> is saved in the worktree's own `plans/reports/` instead; the full content is also in my final
> chat message so it isn't lost. The orchestrator should copy this file to the intended path if
> needed.

## Executed Phase
- Phase: phase-04-hardline-guard-bypasses
- Plan: `D:\FIS\ai-first\yett\plans\260705-1658-harden-yett-harness`
- Worktree: `D:\FIS\ai-first\yett\.claude\worktrees\agent-ab227a8699c177c22`
- Branch: `worktree-agent-ab227a8699c177c22`
- Commit: `a6f38ff` fix(security): close cmdguard &/redirect and sqlguard /*! bypasses
- Status: **DONE**

## Files Modified
- `src/yett/security/cmdguard.py` (+42/-9): lone `&` split fix, no-space/fd-prefix/`>>` redirect detection, traversal canonicalization.
- `src/yett/security/denylist.py` (+18/-3): trailing-boundary fix for `git format-patch` false-deny.
- `src/yett/tools/db/sqlguard.py` (+25/-2): fail-closed `/*!` rejection, `sqlserver`→`tsql` dialect alias.
- `src/yett/policy/immutable.py` (+32/-2): db_query hardline no longer defaults to postgres; driver-hint + fail-closed multi-dialect fallback.
- `tests/unit/test_cmdguard.py` (+54): new `&`, redirect, denylist regression cases.
- `tests/unit/test_sqlguard.py` (+37): `/*!` regression across dialects, sqlserver alias test.
- `tests/unit/test_phase3.py` (+26): immutable-core layer regression tests.
- `src/yett/tools/db/db_query.py`: **not modified** — see "db_query.py: no change" below.

## Tasks Completed

### 1. P0-4 cmdguard `&`
Added lone `&` to `_SHELL_OPS` (shared regex used at both `_contains_delete` :90 and `_is_readonly` :166, was previously two separate, inconsistent regexes — the `_is_readonly` one was also missing `\n`, now fixed too). Ordered `&&`/`||` before `&`/`|` so doubled operators aren't swallowed. Added a negative lookbehind `(?<![<>])` so `&` immediately after `<`/`>` (fd-duplication like `2>&1`, `1>&2`) is **not** treated as a chain operator — verified this by hitting a real regression during implementation (see "Issues Encountered").
Tests: `ls & rm -rf /data`, `echo hi & rm -rf /data`, `cat file & rm -rf /data &` → DELETE_FILE/deny. `docker logs mycontainer 2>&1` still classifies READONLY/allow (fd-dup not mistaken for a delete/readonly-disqualifying redirect).

### 2. P1-6 redirect no-space + fd-prefix + traversal
New `_REDIRECT_TARGET_RE = r"\d*(?:>>?)(?!&)\s*([^\s&|;<>]+)"` catches `x>file`, `1>/etc/x`, `2>/etc/x`, `>>file` (append), while the `(?!&)` lookahead excludes fd-dup (`2>&1`) so it isn't misclassified as a file-write redirect. Target is canonicalized via `posixpath.normpath` before checking the `/dev/null` / `/tmp` exemption — `>/tmp/../etc/passwd` normalizes to `/etc/passwd`, fails the exemption, denied. `_is_readonly` reuses the same detection regex (`_REDIRECT_RE`, sans target capture) so a readonly-looking command with any real redirect is still disqualified.
Tests: `echo x>/etc/hostname`, `echo hi 1>/etc/x`, `echo hi 2>/etc/x`, `echo hi >> /etc/hostname`, `>/tmp/../etc/passwd`, `echo hi > /tmp/../../etc/shadow` → deny. Positive: `echo hi > /dev/null`, `echo hi > /tmp/output.log`, `echo hi >> /tmp/output.log` stay non-delete.

### 3. RT-5/RT-18 sqlguard `/*!` fail-closed + immutable-core dialect
`_normalize` now raises `SqlExecCommentRejected` for any SQL containing the substring `/*!`, caught in `classify_sql` and turned into `Decision("deny", ..., "SQL_EXEC_COMMENT_HARDLINE")` — **independent of the `dialect` argument**, so both `db_query.py:54` (already passed real `prof.driver`) and `immutable.py`'s Gate-time check (previously hardcoded default `postgres`) reject it identically. Regression tests parametrized across `postgres/mysql/sqlite/tsql` in `test_sqlguard.py`, plus a dedicated `test_phase3.py` test at the immutable-core layer (`test_immutable_db_query_exec_comment_blocked_without_known_driver`) going through `PolicyEngine → ImmutableCore` exactly as production wiring does, and a plain-SELECT-still-passes companion test.

**`immutable.py:33` no longer hardcodes `postgres`:** added `ImmutableCore._classify_db_query(sql, driver)`. If a `driver` key is present in `args` (forward-compatible extension point), it's used directly (`classify_sql(sql, driver)`); when absent — which is the case for **every current call-site**, see below — it fails closed by classifying the SQL against all four supported dialects (`postgres, mysql, tsql, sqlite`) and denying if *any* dialect flags it as non-`allow`. `test_immutable_db_query_uses_driver_hint_when_provided` exercises the hint path directly.

**db_query.py: no change (deliberate).** I read the actual call graph: `PolicyEngine.evaluate(tool, args, ctx)` → `self._immutable.check(tool, args)` is invoked from `tools/wiring.py:execute_tool` **before** `tool.validate()`/`tool.run()` — i.e. before `DbQueryTool.run()` ever resolves `args["profile"]` into a `DbProfile` with a real `.driver`. `args` at the Gate-check point is the model's raw `{"profile": str, "sql": str}` — there is no driver to "thread through" at that call-site without either (a) giving `ImmutableCore` a profiles lookup table (would require wiring/app.py changes to inject it — both are explicitly out of this phase's file ownership per the task, owned by "Phase 6"), or (b) restructuring `execute_tool` to resolve the profile before the Gate check (also wiring.py, out of scope). `db_query.py`'s own `classify_sql(args["sql"], prof.driver)` call (line 54) was **already correct** before this phase — it's the *other* call-site (`immutable.py:33`) that was wrong, and that's what I fixed. Given the `/*!` fail-closed fix closes the originally-reported exploit at both sites regardless of dialect, and the multi-dialect fallback in `immutable.py` removes the literal "defaults to postgres" bug, I judged an artificial change to `db_query.py` unnecessary (Rule 3 — surgical changes) and flag the remaining architectural gap below.

Side-effect fix (in scope, same file): `DbProfile.driver == "sqlserver"` was being passed straight to `sqlglot.parse(read="sqlserver")`, which raises `Unknown dialect 'sqlserver'` (sqlglot's real name is `tsql`) — every SQL Server profile query was silently failing closed via `SQL_UNPARSEABLE`. Added `_DIALECT_ALIASES = {"sqlserver": "tsql"}` in `classify_sql`, verified with `test_sqlserver_driver_alias_mapped_to_sqlglot_tsql`. This benefits `db_query.py`'s existing correct call transparently.

### 4. Denylist false-positive (`git format-patch`)
Root cause: the trailing `\b` in `_DENY_EXEC_WIN`'s keyword alternation treats `-` as a word boundary, so `format-patch` matches `format\b`. **First attempt tightened the leading boundary** (require the keyword to follow a real shell operator, not bare whitespace) — this passed all existing tests but I caught it introduced a real regression before committing: `sudo rm -rf /` and `time rm -rf /data` stopped being denied by `denylist.check_exec`, because `app.py` wires `BasicGate` (not `PolicyEngine`) as the active gate, and `BasicGate`'s `exec` path calls **only** `denylist.check_exec` — no `cmdguard` wrapper-stripping backs it up there. Reverted to the original leading boundary and instead changed only the **trailing** boundary to `(?![\w-])` (not followed by a word-char or hyphen). This fixes `format-patch` (next char after `format` is `-`) while leaving `sudo rm -rf /` (next char after `rm` is a space) fully intact. Applied to both `_DENY_EXEC_POSIX` and `_DENY_EXEC_WIN`.
Tests (both directions, in `test_cmdguard.py`): `test_denylist_git_format_patch_not_false_denied` and `test_denylist_wrapped_delete_still_blocked` (covers `sudo rm -rf /`, `time rm -rf /data`, `rm -rf /data`, `format C:`).

### 5. Fail-closed preserved
No change needed — `classify_sql`'s parse-exception path and `cmdguard`'s `shlex` parse-failure path already deny; verified still true (`test_unparseable_fail_closed`, `test_unparseable_is_delete_failclosed` pass unchanged).

## Tests Status
- Type check (`python -m mypy src`): **pass** — "Success: no issues found in 104 source files".
- Unit tests (`python -m pytest -q`): **259 passed, 1 failed** (pre-existing, unrelated — see below). Baseline before my changes was 224 passed/1 failed (same failure); I added 35 new tests, all green, zero new failures or regressions.
- `test_ssh_gate.py` run explicitly: 7 passed.
- Guard-specific suite (`test_cmdguard.py test_sqlguard.py test_phase3.py test_ssh_gate.py test_security_basic.py test_tools_wiring.py`): 120+106 passed across runs, all green.

### Pre-existing failure (not mine, not touched)
`tests/unit/test_setup_wizard.py::test_file_secret_store` fails on this Windows box: expects `chmod 600` semantics (`stat.S_IMODE == 0o600`) that Windows NTFS doesn't honor the same way POSIX does (`0o666` observed). This file is not in my ownership (`secrets/`, `test_setup_wizard.py` — Phase 5 territory) and the task's stated baseline ("224 passed, 1 skipped, 2 xfailed") doesn't exactly match what I observed at branch point either (I got "224 passed, 1 failed", no skip/xfail) — I flagged this discrepancy before starting any edits and it is pre-existing/unrelated to Phase 4 scope, not something I introduced or fixed.

## Issues Encountered
1. **sys.path trap**: this machine has the *main* `yett` tree's `src` hardcoded on `sys.path` (via some global config), so ad-hoc `python -c` sanity checks silently ran against the wrong tree's code. `pytest` itself is unaffected (`pyproject.toml` sets `pythonpath = ["src"]`, resolved relative to the worktree's own rootdir), so all pytest-based verification in this report is against the correct (worktree) code. For manual scripts I explicitly set `PYTHONPATH=src` from the worktree root.
2. **Self-caught regression #1** (fd-dup): lone-`&` split initially broke `2>&1` into two fragments. Fixed with `(?<![<>])` lookbehind before committing — see cmdguard section.
3. **Self-caught regression #2** (denylist leading-boundary): tightening the *leading* boundary to fix `git format-patch` silently defeated `sudo rm -rf /` detection in `BasicGate`'s exec path (the only gate wired in `app.py`). Caught by manually testing wrapped-delete commands before committing; fixed by reverting to the original leading boundary and fixing the *trailing* boundary instead. Neither regression reached the committed code or the test suite in a broken state — flagging both explicitly per "fail loud."

## Next Steps / Unresolved
- **Architectural gap (informational, not a Phase 4 blocker):** `ImmutableCore.check`'s db_query path structurally cannot know the real `prof.driver` at Gate-check time without `wiring.py`/`app.py` changes (resolving the DB profile *before* `execute_tool` calls `safe_evaluate`, or injecting a profiles lookup into `ImmutableCore`). Both files are explicitly out of this phase's ownership (assigned to Phase 6, which the plan's own conflict matrix already schedules after Phase 4 on `policy/immutable.py`). I added a `driver` args-key extension point in `ImmutableCore._classify_db_query` for whoever does that wiring next; until then, the multi-dialect fail-closed fallback is the enforced behavior in production, and the `/*!` fix already closes the specific verified exploit regardless of dialect.
- Confirmed via `git diff --stat` that no files outside the assigned ownership list were touched.
