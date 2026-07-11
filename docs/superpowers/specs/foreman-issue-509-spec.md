# Spec: catch `tomllib.TOMLDecodeError` in `projects.toml` startup and reload guards (issue #509)

## Goal

Both the daemon startup guard (`main()` in `v4/cli/__init__.py`) and the hot-reload guard (`_apply_project_reload()` in `v4/daemon.py`) added by issue #503 fail to catch `tomllib.TOMLDecodeError` — the exception raised when `projects.toml` contains a TOML syntax error. This leaves a gap: at startup the operator sees a raw traceback; during a live reload the daemon crashes. Both guards must be extended to also catch `tomllib.TOMLDecodeError` and produce a clean error message without crashing.

Tracks issue [#509](https://github.com/jeffrichley/foreman/issues/509).

## Acceptance criteria

- `main()` in `packages/foreman/src/foreman/v4/cli/__init__.py` catches `tomllib.TOMLDecodeError` from `load_projects()` and:
  - prints a clean, operator-facing error to stderr naming the offending path and the TOML error
  - raises `typer.Exit(code=1)`
  - does **not** let the traceback propagate
- `_apply_project_reload()` in `packages/foreman/src/foreman/v4/daemon.py` catches `tomllib.TOMLDecodeError` and:
  - logs a `WARNING` naming the exception and its message
  - leaves the current project set unchanged (same behaviour as `FileNotFoundError` / `ValidationError` paths)
  - does **not** crash or re-raise
- `load_projects()` docstring in `packages/foreman/src/foreman/v4/config.py` documents `tomllib.TOMLDecodeError` as a possible exception (alongside the already-documented `FileNotFoundError` and `pydantic.ValidationError`)
- `packages/foreman/tests/v4/test_main.py` has a test `test_main_broken_toml_projects_file_exits_cleanly` that writes a syntactically-broken `projects.toml`, calls `main()`, and asserts `typer.Exit(code=1)` + a clean stderr message
- `packages/foreman/tests/v4/test_daemon_project_reload.py` has a test `test_apply_project_reload_toml_decode_error_keeps_current` that raises `tomllib.TOMLDecodeError` from the projects loader, triggers a reload, and asserts the project set is unchanged and a warning was logged
- `just check` exits zero

## Approach

**No GoF pattern applies. This is straightforward exception-catch broadening.**

The Google principle in play is "make the right thing easy" (and its corollary, "make the wrong thing loud but survivable"): a TOML typo is the single most likely operator mistake during an edit-then-reload cycle, so the daemon must degrade gracefully rather than crash.

### Root cause

`load_projects()` in `config.py` calls `tomllib.loads(path.read_text(...))`. If the file contains invalid TOML, `tomllib.loads()` raises `tomllib.TOMLDecodeError`, which is a subclass of `ValueError` — **not** `pydantic.ValidationError`. Both existing `except` clauses therefore let it propagate as an unhandled exception.

### Fix 1 — startup guard (`cli/__init__.py:main()`)

The existing guard (lines 209–224) has two `except` branches. Add a third:

```python
import tomllib  # (alongside the existing local `from pydantic import ValidationError`)

try:
    projects = load_projects(projects_path)
except FileNotFoundError:
    typer.echo(
        f"ERROR: projects file not found at {projects_path}\n"
        "Create the file with at least one [[projects]] block before "
        "starting the daemon.",
        err=True,
    )
    raise typer.Exit(code=1) from None
except ValidationError as exc:
    typer.echo(
        f"ERROR: projects file at {projects_path} failed validation:\n{exc}",
        err=True,
    )
    raise typer.Exit(code=1) from None
except tomllib.TOMLDecodeError as exc:          # NEW
    typer.echo(
        f"ERROR: projects file at {projects_path} contains invalid TOML:\n{exc}",
        err=True,
    )
    raise typer.Exit(code=1) from None
```

### Fix 2 — reload guard (`daemon.py:_apply_project_reload()`)

The existing guard (line 234) catches `(FileNotFoundError, ValidationError)`. Extend the tuple:

```python
import tomllib  # local import alongside `from pydantic import ValidationError`

try:
    new_projects = self._projects_loader()
except (FileNotFoundError, ValidationError, tomllib.TOMLDecodeError) as exc:  # TOMLDecodeError added
    _log.warning(
        "config reload: failed to load projects file (%s: %s); "
        "keeping current project set unchanged",
        type(exc).__name__,
        exc,
    )
    return
```

The `type(exc).__name__` in the existing warning format already names the exception class correctly (`TOMLDecodeError`), so no change to the log format string itself is needed — only the except tuple.

### Fix 3 — docstring (`config.py:load_projects()`)

Add `tomllib.TOMLDecodeError` to the `Raises:` section so callers know all three failure modes.

### Why not catch bare `ValueError`?

`ValueError` is broader than intended — Pydantic's `ValidationError` is NOT a `ValueError`, but other code paths that call validators might raise `ValueError`. Catching `tomllib.TOMLDecodeError` by name is precise and documents the intent. The issue body notes this explicitly.

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/v4/cli/__init__.py:main()`, add `import tomllib` to the local-imports block (alongside `from pydantic import ValidationError`) and add a third `except tomllib.TOMLDecodeError as exc:` branch with a clean error message and `raise typer.Exit(code=1) from None`.

2. In `packages/foreman/src/foreman/v4/daemon.py:_apply_project_reload()`, add `import tomllib` to the local-imports block and extend the except tuple from `(FileNotFoundError, ValidationError)` to `(FileNotFoundError, ValidationError, tomllib.TOMLDecodeError)`.

3. In `packages/foreman/src/foreman/v4/config.py:load_projects()`, update the docstring `Raises:` section to also document `tomllib.TOMLDecodeError: when the TOML file contains a syntax error`.

4. Add `test_main_broken_toml_projects_file_exits_cleanly` to `packages/foreman/tests/v4/test_main.py` (see File-level changes for the full test body). This follows the same pattern as `test_main_malformed_projects_file_exits_cleanly`.

5. Add `test_apply_project_reload_toml_decode_error_keeps_current` to `packages/foreman/tests/v4/test_daemon_project_reload.py`. This follows the same pattern as `test_apply_project_reload_validation_error_keeps_current`.

6. Run `just check`; confirm exit zero.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/v4/cli/__init__.py` | Add `import tomllib` (local) and a `except tomllib.TOMLDecodeError` branch to the `load_projects()` try/except in `main()` |
| `packages/foreman/src/foreman/v4/daemon.py` | Add `import tomllib` (local) and `tomllib.TOMLDecodeError` to the `except` tuple in `_apply_project_reload()` |
| `packages/foreman/src/foreman/v4/config.py` | Update `load_projects()` docstring to document `tomllib.TOMLDecodeError` |
| `packages/foreman/tests/v4/test_main.py` | Add `test_main_broken_toml_projects_file_exits_cleanly` |
| `packages/foreman/tests/v4/test_daemon_project_reload.py` | Add `test_apply_project_reload_toml_decode_error_keeps_current` |

### Full test bodies

**`test_main_broken_toml_projects_file_exits_cleanly`** (in `test_main.py`):

```python
def test_main_broken_toml_projects_file_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """issue #509: a syntactically-broken projects.toml makes main() exit
    non-zero with a clean 'invalid TOML' message, NOT a raw
    TOMLDecodeError traceback."""
    import typer

    from foreman.v4.cli import main

    config_path = _write_valid_config(tmp_path)
    monkeypatch.setenv("FOREMAN_V4_CONFIG", str(config_path))
    # A projects file with invalid TOML syntax → tomllib.TOMLDecodeError.
    broken_projects = tmp_path / "projects.toml"
    broken_projects.write_text("[[projects\n", encoding="utf-8")  # missing closing bracket
    monkeypatch.setenv("FOREMAN_PROJECTS_PATH", str(broken_projects))
    monkeypatch.setattr("sys.argv", ["foreman", "daemon", "start"])

    with pytest.raises(typer.Exit) as excinfo:
        main()

    assert excinfo.value.exit_code == 1, (
        "startup guard must exit non-zero on broken TOML"
    )
    err = capsys.readouterr().err
    assert "invalid TOML" in err.lower(), f"expected TOML error text; got: {err!r}"
    assert str(broken_projects) in err, "error should name the offending path"
    assert "Traceback" not in err, "must be a clean message, not a raw traceback"
```

**`test_apply_project_reload_toml_decode_error_keeps_current`** (in `test_daemon_project_reload.py`):

```python
def test_apply_project_reload_toml_decode_error_keeps_current(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``tomllib.TOMLDecodeError`` during reload logs a warning and
    leaves the current project set unchanged — the daemon must NOT crash."""
    import tomllib

    def _bad_loader() -> list[ProjectConfig]:
        raise tomllib.TOMLDecodeError("invalid TOML syntax")

    daemon = _make_daemon(_PC1, loader_fn=_bad_loader)

    initial_configs = dict(daemon._project_configs)

    with caplog.at_level(logging.WARNING, logger="foreman.v4.daemon"):
        daemon.request_project_reload()
        daemon.tick_once()

    assert daemon._project_configs == initial_configs, (
        "project_configs changed despite TOMLDecodeError during reload"
    )
    assert any(
        "tomldecoder" in rec.message.lower() or "failed to load" in rec.message.lower()
        for rec in caplog.records
    ), f"Expected warning log; got: {[r.message for r in caplog.records]}"
```

## Alternatives considered

- **Catch bare `ValueError`**: `tomllib.TOMLDecodeError` is a `ValueError`, so one `except ValueError` would handle it. Ruled out because it silently swallows unrelated `ValueError`s from Pydantic validators or other code in the call stack — broader than the guard's intent. Explicit `tomllib.TOMLDecodeError` is more precise and self-documenting.
- **Catch in `load_projects()` and return `None` or `[]`**: Moving the guard down into the loader would hide the error from callers and make `load_projects()` ambiguous (empty list = no projects OR broken file). The caller (`main()` and `_apply_project_reload()`) is the right place to handle the error because it holds the policy (exit vs. keep-current). Ruled out.

## Open questions

(none — the code is unambiguous and both patch sites are clearly identified)

## Out of scope

- Changing the `load_config()` guard in `main()` (it handles the top-level `config.toml`, which already surfaces TOML errors via `tomllib` propagation — the issue is specific to `load_projects()`)
- Catching other `ValueError` subclasses not related to TOML parsing
- Any change to how `foreman daemon reload` sends SIGHUP (that path is correct and unchanged)
- Linting or formatting `projects.toml` for operators beyond the error message
