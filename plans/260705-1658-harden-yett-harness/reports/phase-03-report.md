# Phase 3 Implementation Report — Sandbox Isolation and Exec Scope

> Note: task instructions asked for this report at
> `D:\FIS\ai-first\yett\plans\260705-1658-harden-yett-harness\reports\phase-03-report.md`
> (main tree). The Write tool refused that path ("agent is isolated in the worktree ... edit the
> worktree copy instead"), so this report is written at the worktree-local path the harness
> assigned instead. Content below is what should be copied/merged into the main-tree report path
> by whoever integrates this worktree.

## Executed Phase
- Phase: phase-03-sandbox-isolation-and-exec-scope
- Plan: `D:\FIS\ai-first\yett\plans\260705-1658-harden-yett-harness`
- Worktree: `D:\FIS\ai-first\yett\.claude\worktrees\agent-ab743cf8e6f01fa62`
- Branch: `worktree-agent-ab743cf8e6f01fa62`
- Status: **completed** (with one noted verification gap — no Docker daemon on this machine)

## Files Modified
- `src/yett/app.py` (+~65/-3 lines): sandbox construction moved entirely into `App.__init__`
  (single source of truth); `build_app` no longer guesses/creates a sandbox. Added
  `_project_roots(cfg)` (host workspace_root/project roots → fixed container paths, e.g.
  `/workspace`, `/workspace/projects/<name>`) and `_cwd_mapper(backend, roots)` (host path →
  sandbox path; identity for local, container path for docker, longest-prefix root match).
- `src/yett/sandbox/docker.py` (+~55/-10 lines): added `docker_unavailable_reason()` (probes
  `shutil.which("docker")` then `docker info`) and `probe_docker()` (raises `UserFacingError`
  with remediation). `_base_flags()` now emits `--read-only`, `--tmpfs /tmp:rw,noexec,nosuid,
  size=256m`, `--user 65534:65534`, `--pids-limit 128` in addition to existing `--rm`,
  `--cap-drop ALL`, `--security-opt no-new-privileges`, `--memory`/`--cpus`, `-v` mounts. Fixed
  the no-op `--network` ternary (was `cfg.network if cfg.network != "none" else "none"`, which
  always equals `cfg.network` — replaced with the plain value).
- `src/yett/sandbox/local.py` (docstring only): removed the false "bị Policy Gate chặn trong
  build production" claim (no such gate exists) and rewrote to state the actual invariant —
  LocalSandbox is selected only when `cfg.sandbox.backend == "local"`; nothing downgrades from
  Docker to Local at runtime; missing Docker with `backend: docker` fails closed instead.
- `src/yett/tools/builtin/exec.py`: `ExecTool` now accepts optional `scope: ProjectScope` and
  `to_sandbox_path: Callable[[Path], str]`. `cwd` is scope-checked on the **host** path via
  `ProjectScope.resolve_in_scope` (raises `UserFacingError`, out-of-scope → denied) before being
  mapped to the sandbox path. Without `scope`/`to_sandbox_path` (old call sites), behavior is
  unchanged (raw string passthrough) — backward compatible with existing `ExecTool(LocalSandbox())`
  call sites in `tests/unit/test_tools_wiring.py` (not modified, still green). Docstring now states
  explicitly that this scope-check is hygiene, not containment (command body can still `cd`/use
  absolute paths — real containment is Docker isolation).
- `src/yett/doctor.py`: added `_docker_config_problem(config_path)` (reads config if present,
  returns a problem string when `sandbox.backend == "docker"` and Docker CLI/daemon isn't ready)
  and wired it into `run_doctor(emit, config_path="config/harness.yaml")`. Default arg preserves
  the existing `run_doctor(emit=...)` call signature/behavior exactly when no config is present
  (verified: `config/harness.yaml` is gitignored/absent in this repo, so the pre-existing
  `test_doctor_configio.py::test_doctor_runs_and_reports` — not touched — still passes unchanged).
- `tests/integration/test_docker_sandbox.py` (**new** — see Deviation note below).
- `tests/unit/test_docker_wiring.py` (**new**, 26 tests): mount computation, host→container cwd
  mapping (including longest-prefix and out-of-scope raise), `ExecTool` scope-check + mapping +
  backward-compat, `probe_docker` fail-closed (CLI missing / daemon unreachable / ready), `App`
  fail-closed when backend=docker + Docker missing (and does **not** fall back to LocalSandbox),
  `App` builds `DockerSandbox` with correct mounts when Docker is ready, `DockerSandbox._base_flags()`
  hardening-flag assembly, `--network` reflecting cfg (not the old no-op ternary), `-w` cwd flag
  on `run()`, and `doctor.py`'s new docker-config-aware reporting (missing/ready/backward-compat).

## Tasks Completed
- [x] P0-1: `App.__init__` builds `DockerSandbox` when `backend=="docker"`; Docker/daemon
      missing → `probe_docker()` raises `UserFacingError` with remediation. Removed the
      `sandbox or LocalSandbox()` unconditional fallback — LocalSandbox is now only chosen when
      `sandbox is None` **and** `cfg.sandbox.backend != "docker"`.
- [x] RT-1: `mounts` computed from `cfg.workspace_root` + `cfg.projects` → fixed container paths
      (`/workspace`, `/workspace/projects/<name>`), rw; passed into `DockerSandbox`. (tmpfs scratch
      added at `/tmp`, not a separate "projects scratch" mount — see Note below.)
- [x] RT-1/RT-14: `ExecTool` scope-checks `cwd` on the **host** path via `ProjectScope` first,
      then maps to the container path via the injected mapper for `-w`. Out-of-scope cwd raises
      `UserFacingError` before the sandbox is ever touched. Docstring explicitly disclaims that
      this is hygiene, not containment.
- [x] Docker hardening flags: drop caps, no-new-privileges, read-only rootfs + tmpfs scratch,
      non-root (65534:65534), pids-limit, mem/cpu limits, `--rm`, `--network` (fixed no-op
      ternary), timeout (unchanged — already handled by `asyncio.wait_for` + kill, which is the
      correct layer since `docker run` has no exec-duration flag).
- [x] `local.py`: docstring fixed, marked dev/test-only; env-allowlist item confirmed cut (no
      action taken, per plan).
- [x] `doctor.py`: reports clearly when `sandbox.backend: docker` is configured and Docker is
      unavailable (CLI missing or daemon unreachable), without changing default behavior when no
      config is present.
- [x] `xfail(strict=True)` marker: **not present to remove** — see Deviation below. New
      `test_docker_sandbox.py` was written directly in the fixed (non-xfail) form: `skipif` on
      missing daemon only, real assertions otherwise.

## Deviation: `tests/integration/test_docker_sandbox.py` did not exist

This worktree branches from the plan's shared branch point (commit `2cc89a8`) **before** Phase 1
merged. `tests/integration/test_docker_sandbox.py` is a Phase-1 deliverable (creates the file with
`skipif` + `xfail(strict=True)`) and Phase 3 `blockedBy: [1]` per `plan.md`. Neither the file nor
Phase 1's other artifacts exist in this worktree's lineage.

Since my file-ownership list explicitly names this file (with the instruction to remove its xfail
marker), I created it directly in the **post-fix** shape the plan describes: `pytest.mark.skipif`
gated on `docker info` succeeding, real assertions for (1) exec running inside the container with
workspace mounted (proven via `/etc/os-release` — a signal absent on the Windows host — and by
reading back a file written from the host through the mount), (2) no network egress, (3) `cwd`
mapped to the correct container path, (4) out-of-scope `cwd` denied before the sandbox runs. No
`xfail` marker was ever added, so there was nothing to remove. **This is a scope substitution, not
an omission** — flagging it explicitly per fail-loud discipline. When Phase 1 merges, its
`test_docker_sandbox.py` (if different) will need reconciling with this one; they should converge
on equivalent assertions since both target the same success criteria in `plan.md`.

## Tests Status
- Type check (`python -m mypy`): **pass** — `Success: no issues found in 104 source files`.
- Unit tests (`python -m pytest -q`): **250 passed, 4 skipped, 1 failed** (pre-existing, verified
  unrelated — see Known Pre-Existing Failure below). All 26 new tests in
  `tests/unit/test_docker_wiring.py` pass.
- Integration tests: `tests/integration/test_docker_sandbox.py` — **4 tests, all SKIPPED** (Docker
  Desktop CLI is present on this machine but the daemon is not running; `docker info` exits 1 —
  verified manually). **This is the verification gap**: the actual container-execution, mount, and
  no-egress behavior has NOT been exercised end-to-end on real Docker in this session. Everything
  else (mount computation, cwd mapping, fail-closed behavior, hardening-flag assembly on generated
  argv) is verified without a daemon per the task's guidance, using mocks/direct argv inspection.
- Ruff (`python -m ruff check`): pass on all modified/new files.
- import-linter (`lint-imports`): **crashes** with `'charmap' codec can't decode byte 0x8d...` —
  verified via `git stash`/`stash pop` that this crash is **pre-existing** on the unmodified branch
  tip (Windows default-encoding issue reading a Vietnamese-text source file, unrelated to any file
  I touched). Not in my acceptance criteria or ownership; not fixed.

## Known Pre-Existing Failure (not caused by this phase)
`tests/unit/test_setup_wizard.py::test_file_secret_store` fails on Windows:
`stat.S_IMODE(mode) == 0o600` → actual `0o666`. Verified via `git stash -u` (stashing all Phase 3
changes) that this failure reproduces identically on the clean branch tip — a Windows POSIX
permission-bit limitation in `secrets/file_store.py` (`secrets/` is explicitly out of Phase 3's
ownership; this is Phase 5/secret-perm territory per `plan.md`'s Windows-CI acceptance criteria).
Confirmed the worktree was fully restored (`git status` matched pre-stash) after the check.

## Design Notes / Judgment Calls
- **Single sandbox-construction point.** The plan's Architecture section flags two buggy lines:
  `build_app`'s own sandbox guess and `App.__init__`'s `sandbox or LocalSandbox()` fallback. Rather
  than patching both independently (risking them drifting out of sync on mount/mapper logic), I
  consolidated sandbox construction into `App.__init__` alone; `build_app` now just forwards to
  `App()` without ever constructing or guessing a sandbox. This is the DRY fix for both flagged
  lines at once.
- **Fallback tied to declared backend, not "sandbox is None".** `App.__init__` only falls back to
  `LocalSandbox()` when `sandbox` is `None` **and** `cfg.sandbox.backend != "docker"`. All existing
  tests that construct `App(...)` directly without a `sandbox=` kwarg use
  `SandboxCfg(backend="local")` explicitly, so none of them regressed (verified: full suite green
  aside from the pre-existing failure above).
- **Container path scheme.** `workspace_root` → `/workspace`; a registered project root that is
  *not* already nested under `workspace_root` → `/workspace/projects/<name>` (a project nested
  inside `workspace_root` reuses the workspace mount, no duplicate `-v`). This matches the plan's
  example (`/workspace`) while still giving every registered project a reachable, deterministic
  container path.
- **Hardening choices not covered by existing config fields.** `SandboxCfg` has no fields for
  non-root UID, pids-limit, or tmpfs size — adding them would touch `config/models.py`, which is
  out of this phase's ownership. I hardcoded reasonable defaults (`65534:65534` = nobody:nogroup,
  `--pids-limit 128`, `/tmp` tmpfs `size=256m`) directly in `docker.py`, consistent with the
  existing pattern of `mem_limit`/`cpus` being the only tunables. [Inference] These are common,
  conservative defaults for hardened containers; I have not load-tested them against real workloads.
- **`--network` fix is a simplification, not new behavior.** The old ternary
  (`cfg.network if cfg.network != "none" else "none"`) was mathematically identical to
  `cfg.network` in every case — verified by hand and covered by
  `test_docker_sandbox_network_reflects_cfg_not_hardcoded_ternary`. I replaced it with the plain
  value; no behavior changed, only the confusing dead conditional was removed.
- **`env-allowlist` (RT-25):** confirmed cut per Q1 decision in `plan.md` — no action taken.

## Issues Encountered
- Phase 1 dependency not present in this worktree lineage (see Deviation section above) —
  required creating `test_docker_sandbox.py` from scratch rather than editing an existing file.
- No Docker daemon available for real end-to-end verification (expected per task setup notes).
- Write tool refused the main-tree report path given in the task instructions (worktree isolation
  enforced by the harness itself, stricter than the task's stated exception) — report written to
  the worktree-local `plans\reports\` path instead, per the harness's injected naming convention.

## Next Steps
- When Phase 1 merges its own `test_docker_sandbox.py`, reconcile with the version created here
  (same success criteria — mount, no-egress, cwd mapping — should converge without conflict, but
  someone should diff them before merge).
- Real end-to-end verification of the Docker path (mount visibility, `--network none` egress
  block, container-vs-host proof) still needs to run once on a machine with the daemon up —
  flagging as unverified, not claiming it as tested.
- Phase 6/7 (which touch `app.py` per the plan's conflict matrix) should rebase onto this commit
  before editing `app.py` further, since sandbox construction now lives entirely in `App.__init__`.
- Whoever integrates this worktree should copy/merge this report to
  `D:\FIS\ai-first\yett\plans\260705-1658-harden-yett-harness\reports\phase-03-report.md` as
  originally requested.

Status: DONE_WITH_CONCERNS
Summary: Docker sandbox now wired with real mounts, host→container cwd mapping, and full hardening flags; backend=docker with missing Docker fails closed (unit-tested, no daemon needed). Real end-to-end Docker verification is unverified on this machine (daemon down) and `tests/integration/test_docker_sandbox.py` had to be authored from scratch since Phase 1 (which owns creating it) hasn't merged into this worktree's lineage.
Concerns/Blockers: (1) Verification gap — Docker daemon unavailable here, so mount/no-egress/container-execution behavior is only argv/mock-verified, not run against a real container. (2) `test_docker_sandbox.py` is a new file I authored rather than an edit to an existing Phase-1 artifact; needs reconciliation when Phase 1 merges. (3) Pre-existing, unrelated failure in `test_setup_wizard.py::test_file_secret_store` (Windows file-permission bits) — confirmed not caused by this phase, left untouched (out of ownership). (4) Report path constraint — could not write to the main-tree path requested; wrote to worktree-local path instead.
