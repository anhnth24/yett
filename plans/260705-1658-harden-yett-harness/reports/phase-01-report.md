# Phase 1 — Test and CI Harness: Completion Report

## Files created/modified (exactly the allowed set)

- Created `tests/integration/test_real_provider_contract.py`
- Created `tests/integration/test_docker_sandbox.py`
- Modified `.github/workflows/ci.yml`
- Modified `README.md`
- Modified `tests/unit/test_setup_wizard.py` (xfail marker only, on `test_file_secret_store`)

No files under `src/` were touched.

## 1. `test_real_provider_contract.py`

Builds a real `AgentLoop` (not `FakeProvider`) wired to `OpenAICompatProvider` with an
injected `http_post` recorder (no network). Runs one turn: tool-call response, then
final text response. Asserts:
- request body `messages[0]["role"] == "system"` (exposes **P0-2**: `Context.system` is
  never inserted into `ctx.messages`, so `_to_openai_msg` never emits a system message).
- every `role="tool"` message is immediately preceded by a `role="assistant"` message
  carrying matching `tool_calls` id (exposes **P0-3**: `AgentLoop.run_turn` only calls
  `ctx.add_assistant()` on the end-turn/force-text branch, so the tool_use turn is
  dropped from context before `ctx.add_tool_result()` runs).

Marked `@pytest.mark.xfail(strict=True)`. Verified it actually raises `AssertionError`
on the first check (fails before reaching the ordering check) — sufficient for xfail;
Phase 2 fixing P0-2 will make the test progress further into the ordering assertion
(still xfail) until P0-3 is fixed too, at which point it XPASSes and strict=True forces
a loud failure reminding to remove the marker.

## 2. `test_docker_sandbox.py`

Module-level `pytestmark = pytest.mark.skipif(...)` gated on `shutil.which("docker")` +
`docker info` returning exit 0 (confirmed on this machine: Docker CLI present but
**daemon not running** → `docker info` exits 1 → test **skips cleanly**, verified).

Body (only runs when a daemon is live) mirrors `build_app()`'s sandbox-selection bug
(`app.py:48`) inline — `backend != "local"` → `sandbox=None` → `App.__init__` (`app.py:81`)
falls back to `LocalSandbox()` regardless of the configured backend. Asserts three
properties via `ExecTool` directly:
- workspace mount + exec-in-container works (write a file with `cwd=workspace`, check it
  reappears on host).
- no network egress — deliberately avoided any real network call; used
  `socket.if_nameindex()` (local syscall, counts interfaces) instead of connecting out,
  since `--network none` containers only have `lo`. This keeps the test hermetic (no
  real egress) while still proving the isolation property.
- not running on host — checks `/proc/self/cgroup` for `docker`/`containerd` markers.

I manually invoked the test body directly (bypassing the skip decorator, since no daemon
is available here) to confirm the code path is sound: it correctly executed via the
`LocalSandbox` fallback (Windows, Git-Bash `sh` present) and correctly failed the
network-isolation assertion (detected **101 host network interfaces**, proving the
sandbox is not isolated) — confirms the test faithfully exposes RT-1/P0-1 when run.
Under normal pytest collection it registers as **SKIPPED** (no daemon on this box).

## 3. `.github/workflows/ci.yml`

Added `strategy: {fail-fast: false, matrix: {os: [ubuntu-latest, windows-latest]}}` and
`runs-on: ${{ matrix.os }}`. Changed the import-linter step from inline POSIX
`PYTHONPATH=src lint-imports` to an `env: {PYTHONPATH: src}` step (cross-platform, works
under both bash on ubuntu and pwsh on windows-latest).

Not verified locally: `ruff`, `mypy`, `import-linter` are not installed in this
environment (`pip install -e ".[dev]"` was not run — out of scope for this phase per the
constraint against touching the environment beyond what's needed). [Unverified] whether
these three steps are clean on Windows; only `pytest -q` was explicitly required and is
verified clean. Flagged as a risk below.

## 4. Windows xfail on `test_file_secret_store`

Added `@pytest.mark.xfail(sys.platform == "win32", strict=True, reason=...)` (references
P1-13, Phase 5). Verified in isolation: `XFAIL ... 4 passed, 1 xfailed`.

## 5. README.md

- Status line + `pytest -q` comment: fixed count from stale "161"/"183" to the actual
  collected count, **227** (224 passed + 2 xfailed + 1 skipped).
- Rewrote the status table: added a "Kiểm chứng" (verification) column with a legend
  distinguishing ✅ e2e / 🟡 fake (logic-only, not verified against a real backend) / ❌
  chưa wire (configured but not actually wired into the real code path, backed by an
  `xfail(strict=True)` regression test).
- Downgraded the three items called out in the phase spec:
  - **Docker sandbox**: split into its own row, marked ❌ chưa wire (points at
    `test_docker_sandbox` + explains the `LocalSandbox` fallback bug).
  - **Agent loop**: marked 🟡 fake (points at `test_real_provider_contract` + explains
    the system-prompt/tool-ordering bug against a real provider).
  - **`web_search` tool**: split out of the eval-suite row, marked ❌ chưa wire — found
    that `App._wire_search()` (`app.py:151-153`) is a no-op; `WebSearchTool` is unit
    tested by calling it directly, but `yett chat` never registers it into the tool
    registry. This is a third invisible gap beyond the two the phase spec named
    explicitly (loop/docker) — flagging it here since I found it while auditing every ✅
    against actual wiring, and it fits the same "misleading ✅" pattern the phase exists
    to fix. Did not open a new xfail test for it (out of the phase's explicit test-file
    ownership) — just corrected the README claim; recommend whichever later phase owns
    `app.py`/tool wiring picks it up (not currently listed against P0-2/P0-3/RT-1 in the
    plan).

## Verification — final full run

```
python -m pytest -q -rxXs
224 passed, 1 skipped, 2 xfailed in 11.60s
```

- `SKIPPED`: `test_docker_sandbox.py` (no Docker daemon running on this machine — CLI
  present, `docker info` exits 1).
- `XFAIL` (2): `test_real_provider_contract.py::test_real_provider_sends_system_prompt_and_valid_tool_ordering`,
  `test_setup_wizard.py::test_file_secret_store`.
- 0 failures. No test other than the pre-existing, now-marked `test_file_secret_store`
  needed any Windows-specific treatment — matches the baseline the team lead supplied
  (224 passed / 1 failed pre-Phase-1, now 0 failed).

## Unresolved / flagged for follow-up

- [Unverified] `ruff`/`mypy`/`import-linter` on Windows — tools not installed in this
  environment, could not run them locally; only the CI YAML wiring was changed, not
  validated by actually executing those tools on Windows.
- `web_search` wiring gap (`App._wire_search()` no-op) found during README audit — not
  in this phase's file ownership to fix or add a regression test for; surfaced here for
  whichever phase touches `app.py`'s tool wiring next.
- `plans/` directory is currently untracked in git (`git status` shows `?? plans/`) —
  I did not reference it from README to avoid a dangling link for anyone without that
  local directory.

Status: DONE
Summary: Created both xfail-gated integration tests (contract test proves P0-2/P0-3 by construction; docker test proves RT-1 by construction, verified via manual bypass of the skip since no local daemon), added the Windows CI matrix leg with cross-platform import-linter env, xfailed the one known Windows-only failure, and corrected three misleading README ✅ claims (loop/docker/web_search) plus the test count. Full suite: 224 passed, 1 skipped, 2 xfailed, 0 failed.
Concerns/Blockers: ruff/mypy/import-linter unverified on Windows (tooling not installed here); web_search wiring gap found but out of my file ownership to fix.
