# Phase 5 Report — Secret Handling

- Worktree: `D:\FIS\ai-first\yett\.claude\worktrees\agent-a7d958b135071e04e`
- Branch: `worktree-agent-a7d958b135071e04e`
- Commit: `f6fe36b` — `fix(secrets): full PEM redaction, provider-prefixed key patterns, Windows ACL, keyring backend`
- Status: DONE

## Baseline discrepancy (noted, not a blocker)

Task brief stated baseline = "224 passed, 1 skipped, 2 xfailed (incl. `test_file_secret_store` on win32)".
Actual baseline measured in this worktree before any edits: **224 passed, 1 failed** (`test_file_secret_store`
failed outright on the `stat.S_IMODE(mode) == 0o600` assertion; no xfail marker present anywhere in the repo
for that test, nor anywhere in the codebase). [Unverified] whether Phase 1's xfail marker landed on a
different branch/worktree than this one. Regardless, the underlying bug (Windows chmod no-op + silent
`except OSError: pass`) is exactly the one the phase file describes, so it was fixed and the test now
passes for real — there was no marker to remove.

## Deliverables

### 1. P0-5 + RT-15 — `src/yett/security/filters.py`
- `_PRIVATE_KEY_BLOCK`: `-----BEGIN (...PRIVATE KEY)-----.*?-----END \1-----` with `re.DOTALL`,
  non-greedy plus a backreference so two adjacent PEM blocks in one output do not get merged into one
  match (verified by `test_redact_two_full_blocks_does_not_swallow_middle_content`).
- `_PRIVATE_KEY_TRUNCATED`: `-----BEGIN [A-Z ]*PRIVATE KEY-----.*` (DOTALL) as a second pass — only
  matches headers left over after the full-block pass, i.e. genuinely truncated output (no footer found
  anywhere) — redacts header-to-end-of-string. Covers `head -c 400 id_rsa`, paginated logs, capped exec
  output. This implements the "long base64 run right after the header" heuristic from the phase spec:
  with no footer to delimit it, greedily consuming to end-of-string is the deliberate over-redact-first
  choice the phase file calls for.
- Negative test `test_redact_certificate_not_over_redacted`: public certs (`BEGIN CERTIFICATE`) pass
  through untouched — the pattern only fires on the literal string "PRIVATE KEY".

### 2. RT-6/19 — key pattern + DSN + recursive redact
- New `generic_sk_key` pattern: `\bsk-[A-Za-z0-9-]{20,}\b`. Matches `sk-cp-...` (the leaked-then-rotated
  MiniMax shape), `sk-proj-...`, and arbitrary provider prefixes. The `\b` before `sk-` prevents mid-word
  matches (tested: `desk-top-computer-...` is NOT redacted).
- `odbc_dsn_pwd` pattern: case-insensitive `pwd`/`password` followed by `=` and a value token, replaced
  as `key=[REDACTED]` — covers ODBC/ADO DSNs (`Pwd=...;`, `Password=...;`), keeps the key name, redacts
  only the value. Negative test: the bare word "password" without `=` is left untouched.
- `redact_attrs` now recurses through nested dict/list via a `_redact_value` helper (`Any`-typed,
  justified inline since span attrs are JSON-like/dynamic by nature); tested two levels deep
  (dict of dict of list).

### 3. P1-13 — `src/yett/secrets/file_store.py` (Windows ACL, fail-loud)
- POSIX path unchanged: chmod 700 (dir) / chmod 600 (file).
- Windows: `icacls <path> /inheritance:r` then grant Full Control to the current user's SID only,
  obtained via `whoami /user /fo csv /nh` rather than a bare username string. This matters concretely on
  this machine: the local computer name and the username collide, and passing the plain username to
  icacls resolved ambiguously (to the machine account rather than the user account). SID-based grant is
  unambiguous and was verified for real against actual icacls output: owner-only, no inherited ACEs, no
  Everyone/BUILTIN-Users/Authenticated-Users entries.
- Any icacls/chmod failure now logs a warning and prints to stderr — never a silent swallowed OSError.
  Verified with a monkeypatched failing subprocess call.
- `tests/unit/test_setup_wizard.py::test_file_secret_store` now branches on `sys.platform`: POSIX keeps
  the original stat-mode check; Windows runs a real icacls query against the file this test just wrote
  (no mocking) and asserts owner-only, no-inheritance. Passes on this machine with no xfail marker.
  Added a second test for the loud-fail path.

### 4. P1-17 — `src/yett/secrets/backends.py` (existing file, added class) + `resolve.py`
- `backends.py` already existed in this worktree (owns EnvSecretStore/InMemorySecretStore) — the
  KeyringSecretStore class was added to it rather than creating a duplicate file, since the phase doc's
  "Create" instruction predates this file's actual presence on this branch.
- `KeyringSecretStore.__init__` does a lazy `import keyring`; ImportError raises UserFacingError
  immediately with remediation (install keyring, or switch secret_backend to env/file). get/set catch a
  broad Exception from the underlying OS credential manager call (justified inline — matches the
  project's existing fail-closed convention in errors.py rather than depending on keyring's exact
  exception hierarchy) and translate to SecretNotFound/SystemError.
- `resolve.py`: build_secret_store now returns a typed SecretStore; wires backend keyring to
  KeyringSecretStore (its constructor already raises on missing lib); backend age raises
  UserFacingError (kept in the models.py enum, not implemented, explicit remediation to switch backend);
  the old silent file-store fallback comment/branch is gone entirely — every backend is either real or
  fails loud.
- New `tests/unit/test_secret_backends.py` (10 tests): round-trip via a fake in-memory keyring module
  substituted through sys.modules (no real OS credential store touched anywhere); missing-library path
  simulated with the documented CPython `sys.modules[name] = None` technique (verified more reliable
  than keyring's own testing.util.ImportKiller helper, which does not work under Python 3.13's
  find_spec-only import protocol — confirmed empirically, not used); backend-error paths mapped to
  SecretNotFound/SystemError; build_secret_store wiring covered for keyring/age/env/file.

### 5. pyproject.toml
- Installed keyring==25.6.0 (pip show confirms), pinned exact in dependencies. Ships py.typed, so no
  mypy override entry was needed — mypy --strict resolves it natively.

### 6. P1-14 decision record (orchestrator decision, executed as instructed)
- Inline `api_key` in ProviderCfg stays — not removed, no behavior change to models.py for this field
  (it already had both api_key and api_key_secret, untouched).
- Added a warning comment in config/harness.example.yaml next to api_key recommending the secret store
  (file/keyring) for shared machines or backed-up configs.
- `/api/config` GET redaction explicitly deferred to Phase 7a (app.py/web/ are out of this phase's file
  ownership).
- config/harness.yaml (untracked, contains the already-rotated MiniMax key) was not opened or edited.

## Files Modified

- `src/yett/security/filters.py` — private key block/truncated redaction, generic provider-key pattern,
  ODBC DSN pattern, recursive redact_attrs.
- `src/yett/secrets/file_store.py` — Windows ACL via icacls+SID, fail-loud warnings, POSIX unchanged.
- `src/yett/secrets/resolve.py` — typed return, keyring/age wiring, no silent fallback.
- `src/yett/secrets/backends.py` — added KeyringSecretStore.
- `pyproject.toml` — added keyring==25.6.0.
- `config/harness.example.yaml` — warning comment near inline api_key.
- `tests/unit/test_filters.py` — 9 new tests (full block, truncated, two-block non-greedy, certificate
  negative, generic key incl. sk-cp-, hyphen-boundary negative, ODBC DSN, password-word negative,
  recursive nested redact_attrs).
- `tests/unit/test_setup_wizard.py` — test_file_secret_store made platform-aware (real icacls check on
  Windows); 1 new test for the loud-fail path.
- `tests/unit/test_secret_backends.py` (new) — 10 tests for KeyringSecretStore + build_secret_store
  wiring.
- `src/yett/config/models.py` — not modified: the secret_backend Literal already included
  keyring/age/env/file in this worktree; nothing to add.

## Tests Status

- Full suite: `python -m pytest -q` -> 245 passed (baseline 224 passed / 1 failed, plus 21 net new
  passing tests: 1 pre-existing test fixed in place + 20 new test functions).
- `python -m mypy` -> Success: no issues found in 104 source files.
- `python -m ruff check` on all touched files -> all checks passed.

## Issues Encountered

- backends.py already existed (not a new file as the phase doc assumed) — resolved by extending it,
  documented above.
- keyring's own testing.util.ImportKiller helper (for simulating missing imports) does not work on
  Python 3.13 (relies on the old find_module/load_module finder protocol, which the import system no
  longer calls) — used the standard sys.modules[name] = None technique instead, which is documented
  CPython behavior and verified working.
- Windows SID-vs-username gotcha for icacls: this machine's computer name collides with the username,
  which made icacls resolve a bare username ambiguously. Switched to granting by SID obtained via
  whoami /user, which is unambiguous and was verified against real icacls output.

## Unresolved Questions

- None blocking. The baseline-count discrepancy above is informational only — the actual bug and its fix
  are unaffected by whether a marker existed on this branch.
