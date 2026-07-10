"""``foreman init`` — onboard a new repo onto Foreman.

The init flow is a one-shot setup pass that:

  1. Validates the target repo + clone path
  2. Refuses to overwrite an existing ``[[projects]]`` block whose
     ``name`` matches unless ``--force`` is passed
  3. Writes a ``.foreman/INSTRUCTIONS.md`` template into the local clone
     (skipping if one already exists — even with ``--force``)
  4. Creates the Foreman v4 state labels on the target repo (idempotent:
     existing labels are left alone). The label set tracks the v4
     observer-owned state machine: the ``foreman:plan`` trigger plus the
     ``foreman:state-*`` set, one per :data:`STATE_REGISTRY` entry.
  5. Best-effort verifies that each role's GitHub App can mint an
     installation token against the target repo
  6. Appends the ``[[projects]]`` block to ``~/.foreman/v4/config.toml``
  7. Returns a structured :class:`InitResult` the CLI surfaces as a
     ready-to-use summary

Design notes:

* The logic lives here (not in ``cli.py``) so it can be tested without
  click. ``cli.py`` is a thin wrapper that constructs the
  :class:`InitConfig`, calls :func:`run_init`, and prints the summary.
* Label creation is intentionally idempotent and best-effort. Foreman
  treats existing labels as "operator may have customized the color or
  description"; overwriting those would be hostile, so we only create
  what's missing.
* Bot verification is best-effort. The operator may want to set up
  labels + config now and install bots later; refusing to finish init
  because one bot isn't installed would force them to interleave.
* The top-level ``[apps.<role>]`` and ``[orchestrator]`` blocks are NOT
  written when adding a new project to an existing config; they are
  operator-curated single-source identity blocks that ``run_init``
  refuses to clobber. When the config file does not exist yet, init
  writes a templated full v4 shape with placeholder values the operator
  must edit before the daemon will start.
* TOML writing uses string manipulation rather than ``tomli_w`` (not a
  current dep). Existing project blocks and comments are preserved
  verbatim — we only append the new block (or replace it block-by-block
  under ``--force``).

Phase 8d.9: this module was rewritten end-to-end onto the v4 substrate
(V4Config / v4/config.toml / v4 state labels). The v3 ``foreman init``
ergonomic now writes a config the v4 daemon can actually read.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from github import Github, GithubException

from foreman.auth import fetch_app_metadata, mint_installation_token
from foreman.labels import Label
from foreman.v4.config import AppsConfig, V4Config, load_config
from foreman.v4.states.registry import STATE_REGISTRY

_log = logging.getLogger(__name__)

# Repo slug must look like ``owner/repo`` with the usual GitHub character set.
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

# Path to the instructions template (loaded via importlib.resources from
# ``foreman.templates``). Pinned as a module constant so the template's
# filename appears in exactly one place.
_TEMPLATE_RESOURCE_NAME = "instructions.md.template"

# Default check-command — must agree with
# :mod:`foreman.roles.worker._DEFAULT_CHECK_COMMAND` (same default applies
# at both ends of the pipeline). Duplicated here as a local constant
# rather than imported so init has no dependency on the worker module.
_DEFAULT_CHECK_COMMAND = "just check"

# Default v4 config path. Mirrors ``foreman.v4.cli.init._DEFAULT_CONFIG``
# but without requiring typer in this module. Phase 8d.9 moved this from
# ``~/.foreman/config.toml`` (v3) to ``~/.foreman/v4/config.toml`` so a
# fresh ``foreman init`` produces a file the v4 daemon can read.
_DEFAULT_CONFIG_PATH = Path.home() / ".foreman" / "v4" / "config.toml"

# Default v4 daemon paths used when we emit a brand-new config file.
_DEFAULT_LOG_DIR = "~/.foreman/v4/logs"

# Placeholder Postgres DSN written into a brand-new config's [storage]
# block. Persistence is Postgres-only; the operator must
# replace this with a real DSN before the daemon will connect. It is a
# syntactically valid postgresql:// URL so the config loads under the
# loud-fail StorageConfig validator (which requires a non-empty dsn).
_DEFAULT_STORAGE_DSN = "postgresql://USER:PASSWORD@HOST:5432/foreman"


def _state_label_name(state_name: str) -> str:
    """Map a STATE_REGISTRY entry ("Queued", "SpecReview", ...) to a label.

    The label form is ``foreman:state-<kebab>``. "SpecReview" →
    "spec-review", "NeedsHelp" → "needs-help".
    """
    kebab = re.sub(r"(?<!^)([A-Z])", r"-\1", state_name).lower()
    return f"foreman:state-{kebab}"


def _build_v4_label_catalog() -> list[tuple[Label | str, str, str]]:
    """Catalog the v4 labels ``foreman init`` writes to a target repo.

    Returns a list of ``(name, color, description)`` triples where
    ``name`` is a :class:`Label` member when the label already lives in
    the typed enum (the ``foreman:plan`` trigger), otherwise a bare
    string for the observer-owned ``foreman:state-*`` set.

    The ``foreman:state-*`` set is derived from
    :data:`foreman.v4.states.registry.STATE_REGISTRY` so adding a new
    state automatically grows the init-time label set — no second place
    to keep in sync. Color coding by category mirrors the v3 catalog's
    intent (queue/in-flight/blocking/terminal palettes).
    """
    # Map state name → (color, description). Anything not listed here
    # is treated as in-flight (yellow) — defense in depth so a new state
    # added to the registry without being categorized still gets a
    # plausible label.
    state_metadata: dict[str, tuple[str, str]] = {
        "Queued": ("0E8A16", "Foreman v4: queued, waiting for Poller"),
        "Planning": ("FBCA04", "Foreman v4: Planner running"),
        "SpecReview": ("FBCA04", "Foreman v4: Reviewer running on spec PR"),
        "SpecFix": ("D93F0B", "Foreman v4: Fixer running on spec PR"),
        "Implementing": ("FBCA04", "Foreman v4: Worker running"),
        "ImplReview": ("FBCA04", "Foreman v4: Reviewer running on impl PR"),
        "ImplFix": ("D93F0B", "Foreman v4: Fixer running on impl PR"),
        "Merging": ("FBCA04", "Foreman v4: Orchestrator merging PR"),
        "Done": ("6F42C1", "Foreman v4: ticket complete"),
        "Failed": ("B60205", "Foreman v4: ticket exhausted retries (terminal)"),
        "NeedsHelp": ("FBCA04", "Foreman v4: surfaced for human intervention"),
    }
    catalog: list[tuple[Label | str, str, str]] = [
        (
            Label.PLAN,
            "0E8A16",
            "Foreman v4: trigger label — Poller picks up the ticket",
        ),
    ]
    for state_name in STATE_REGISTRY:
        color, description = state_metadata.get(
            state_name,
            ("FBCA04", f"Foreman v4: state {state_name}"),
        )
        catalog.append((_state_label_name(state_name), color, description))
    return catalog


# The Foreman v4 labels created on the target repo. Phase 8d shifted all
# label writes to the observer + state machine; the init-time catalog
# mirrors that set so an operator can label an issue ``foreman:plan`` and
# expect the rest of the ``foreman:state-*`` labels to be available for
# the observer to stamp.
_FOREMAN_LABELS: list[tuple[Label | str, str, str]] = _build_v4_label_catalog()

# The four role names init knows about; mirrors :mod:`foreman.identity`.
_ROLE_NAMES: tuple[str, ...] = ("planner", "reviewer", "fixer", "worker")


# ----------------------------------------------------------------------
# Public types
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class InitConfig:
    """Inputs to :func:`run_init`.

    Mirrors the ``foreman init`` CLI surface; constructed by the CLI from
    parsed args. Frozen because every step of init reads it as immutable
    input.
    """

    repo: str
    """Target GitHub repo in ``owner/name`` form."""

    name: str
    """Project name used as the ``[[projects]].name`` key in the config."""

    clone_path: Path
    """Absolute path to the local clone on disk."""

    check_command: str
    """Quality gate command; written into the config block only when
    different from the default (see :func:`run_init`)."""

    force: bool
    """When True, overwrite an existing matching ``[[projects]]`` block."""

    config_path: Path
    """Path to ``~/.foreman/v4/config.toml`` (or override)."""


@dataclass
class BotVerification:
    """Result of a single best-effort bot installation check."""

    role: str
    ok: bool
    detail: str
    """Human-readable detail: ``"OK"`` on success, ``"skipped: ..."`` when
    credentials are missing, or the exception message on failure."""


@dataclass
class InitResult:
    """Structured return value of :func:`run_init`.

    The CLI prints :attr:`summary` directly. Tests inspect the typed
    fields for fine-grained assertions.
    """

    repo: str
    name: str
    clone_path: Path
    config_path: Path
    instructions_path: Path
    instructions_written: bool
    """True when the template was newly written; False when an existing
    file was preserved."""
    labels_created: list[str]
    """Names of labels newly created on this run."""
    labels_existing: list[str]
    """Names of labels that already existed and were not modified."""
    bot_verifications: list[BotVerification] = field(default_factory=list)
    instructions_dirty_warning: str | None = None
    """Copy-pasteable ``git add ... && git commit ...`` command when the
    generated ``.foreman/INSTRUCTIONS.md`` is untracked or has
    uncommitted modifications after init. ``None`` when the file is
    clean (committed, no diff) OR when the git-status check itself
    failed (defensive: a git error must not block init)."""
    summary: str = ""


# ----------------------------------------------------------------------
# Argument validation
# ----------------------------------------------------------------------


def _validate_repo_slug(repo: str) -> tuple[str, str]:
    """Return ``(owner, repo)`` or raise ``ValueError`` with a clear message."""
    if not _REPO_SLUG_RE.match(repo):
        raise ValueError(
            f"Repo must be in 'owner/repo' form (got {repo!r}); "
            "see https://github.com/<owner>/<repo>"
        )
    owner, name = repo.split("/", 1)
    return owner, name


def _validate_clone_path(clone_path: Path, expected_repo: str) -> None:
    """Verify that ``clone_path`` is a usable git clone of ``expected_repo``.

    Checks existence, that the path is a directory, that it is a git
    repo, and that its ``origin`` remote points at ``expected_repo``.
    Raises ``ValueError`` with a specific message for each failure mode.
    The remote check accepts both HTTPS and SSH URL shapes so operators
    using SSH push aren't forced to reconfigure their remote for init.
    """
    if not clone_path.exists():
        raise ValueError(f"Clone path does not exist: {clone_path}")
    if not clone_path.is_dir():
        raise ValueError(f"Clone path is not a directory: {clone_path}")
    if not (clone_path / ".git").exists():
        raise ValueError(f"Clone path is not a git repository (no .git dir): {clone_path}")
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=clone_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(
            f"Could not read 'origin' remote in {clone_path}: "
            f"{result.stderr.strip() or 'no remote configured'}"
        )
    origin_url = result.stdout.strip()
    if not _remote_matches_repo(origin_url, expected_repo):
        raise ValueError(
            f"Clone's 'origin' remote ({origin_url!r}) does not match "
            f"the target repo {expected_repo!r}. Re-run with --clone-path "
            "pointing at the correct clone."
        )


def _remote_matches_repo(origin_url: str, expected_repo: str) -> bool:
    """Return True if ``origin_url`` references ``expected_repo``.

    Accepts the common URL shapes:
      * ``https://github.com/owner/repo``
      * ``https://github.com/owner/repo.git``
      * ``git@github.com:owner/repo.git``
      * ``ssh://git@github.com/owner/repo.git``

    The check is intentionally permissive — we want to recognize a
    correctly-pointed clone regardless of the operator's preferred URL
    scheme.
    """
    if not origin_url:
        return False
    stripped = origin_url.rstrip("/")
    if stripped.endswith(".git"):
        stripped = stripped[: -len(".git")]
    # HTTPS / ssh:// shapes — last two path segments are owner/repo.
    for scheme in ("https://", "http://", "ssh://"):
        if stripped.startswith(scheme):
            tail = stripped.split("/")
            if len(tail) >= 2:
                candidate = f"{tail[-2]}/{tail[-1]}"
                return candidate.lower() == expected_repo.lower()
            return False
    # SCP-style: ``git@github.com:owner/repo``
    if "@" in stripped and ":" in stripped:
        candidate = stripped.split(":", 1)[1]
        return candidate.lower() == expected_repo.lower()
    return False


# ----------------------------------------------------------------------
# Cwd detection (used by CLI to default --clone-path)
# ----------------------------------------------------------------------


def detect_matching_clone(cwd: Path, expected_repo: str) -> Path | None:
    """Return ``cwd`` if it's a git repo whose origin matches ``expected_repo``, else ``None``.

    Used by the CLI to default ``--clone-path`` when the operator runs
    ``foreman init owner/repo`` from inside that repo's clone. Never
    raises — a non-matching cwd just returns ``None``.
    """
    try:
        _validate_clone_path(cwd, expected_repo)
    except ValueError:
        return None
    return cwd


# ----------------------------------------------------------------------
# Instructions template
# ----------------------------------------------------------------------


def _load_instructions_template() -> str:
    """Load the packaged instructions template as a string."""
    return (
        resources.files("foreman.templates")
        .joinpath(_TEMPLATE_RESOURCE_NAME)
        .read_text(encoding="utf-8")
    )


def _render_instructions_template(repo_name: str, check_command: str) -> str:
    """Substitute ``<repo-name>`` and ``<configured_check_command>``.

    Simple string replacement keeps the template human-readable as a
    standalone markdown file (no Jinja, no f-string escaping). Markers
    are angle-bracketed placeholders the operator is unlikely to use
    literally in real instructions, and they sit in clearly templated
    positions (header line + quality-gate section).
    """
    template_text = _load_instructions_template()
    return template_text.replace("<repo-name>", repo_name).replace(
        "<configured_check_command>", check_command
    )


def _write_instructions_template(
    *, clone_path: Path, repo_name: str, check_command: str
) -> tuple[Path, bool]:
    """Write the template to ``<clone>/.foreman/INSTRUCTIONS.md``.

    Returns ``(path, wrote_new)``. Skips (preserves existing content)
    when the file is already present — even when ``--force`` was passed
    upstream, because instructions are operator-curated and overwriting
    them would destroy their work. The CLI surfaces the skip in the
    summary so the operator knows.

    Creates the ``.foreman/`` parent dir when missing.
    """
    foreman_dir = clone_path / ".foreman"
    foreman_dir.mkdir(parents=True, exist_ok=True)
    target = foreman_dir / "INSTRUCTIONS.md"
    if target.exists():
        return target, False
    rendered = _render_instructions_template(repo_name, check_command)
    target.write_text(rendered, encoding="utf-8")
    return target, True


def _check_instructions_committed(clone_path: Path) -> str | None:
    """Return a warning if ``.foreman/INSTRUCTIONS.md`` isn't committed in ``clone_path``.

    Runs ``git status --porcelain -- .foreman/INSTRUCTIONS.md`` and
    inspects the output:

    * Empty output → the file is committed and clean → returns ``None``.
    * Non-empty output → the file is untracked or modified → returns a
      warning string naming the exact ``git add ... && git commit ...``
      command the operator should run.
    * Subprocess failure (non-zero exit, missing git binary, OSError,
      etc.) → returns ``None``. A git problem here must NOT block
      ``foreman init`` from completing; the operator's clone is in a
      state we can't reason about, so we stay silent rather than
      surfacing a misleading warning.

    The warning embeds ``-C <clone_path>`` so the printed command works
    regardless of the operator's current working directory.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", ".foreman/INSTRUCTIONS.md"],
            cwd=clone_path,
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        # Missing git binary, permission denied, etc. — silently treat
        # as clean rather than failing init.
        return None
    if result.returncode != 0:
        return None
    if not result.stdout.strip():
        return None
    return (
        f"git -C {clone_path} add .foreman/INSTRUCTIONS.md && "
        f'git -C {clone_path} commit -m "chore: commit .foreman/INSTRUCTIONS.md"'
    )


# ----------------------------------------------------------------------
# Label creation (idempotent)
# ----------------------------------------------------------------------


def _ensure_labels(*, client: Github, repo_slug: str) -> tuple[list[str], list[str]]:
    """Create any missing Foreman labels on ``repo_slug``.

    Returns ``(newly_created, already_existed)`` — both as label-name
    lists, in the original :data:`_FOREMAN_LABELS` order so the CLI's
    summary lists them in the operator-meaningful sequence.

    Existing labels are left untouched on color and description: an
    operator may have customized them, and init refuses to overwrite
    human customization. A subsequent ``foreman label sync`` (future
    ticket) is the appropriate surface for forced-update semantics.
    """
    repo = client.get_repo(repo_slug)
    existing_names = {label.name for label in repo.get_labels()}
    newly_created: list[str] = []
    already_existed: list[str] = []
    for label_id, color, description in _FOREMAN_LABELS:
        # Catalog entries are either a Label enum member (typed) or a
        # bare string (state-* labels). Coerce to string for the GitHub
        # API; both forms are equal-comparable for the set membership
        # check above.
        name = str(label_id)
        if name in existing_names:
            already_existed.append(name)
            continue
        try:
            repo.create_label(name=name, color=color, description=description)
            newly_created.append(name)
        except GithubException as exc:
            # 422 means a race: another process (or operator) just
            # created the label between our list-and-create. Treat it
            # as "already existed" — same end state.
            if exc.status == 422:
                already_existed.append(name)
                continue
            raise
    return newly_created, already_existed


# ----------------------------------------------------------------------
# Bot verification (best-effort)
# ----------------------------------------------------------------------


def _verify_bot_installation(*, role: str, apps: AppsConfig, repo_slug: str) -> BotVerification:
    """Best-effort check that ``role``'s App is installed on ``repo_slug``.

    Skipped (with a clear ``detail``) when the role's App credentials
    are placeholders or unreadable — the operator may set up apps
    incrementally. Failures (network errors, missing key file,
    installation absent) are recorded but do not raise; init continues
    with the next role.
    """
    try:
        app_id = _resolve_app_id(role, apps)
        key_path = _resolve_key_path(role, apps)
    except RuntimeError as exc:
        return BotVerification(role=role, ok=False, detail=f"skipped: {exc}")
    try:
        # Fetch app metadata first — confirms the key + ID match a real
        # App. Then mint a token to confirm the installation exists.
        fetch_app_metadata(app_id, key_path)
        mint_installation_token(app_id, key_path, repo_slug)
    except Exception as exc:
        return BotVerification(role=role, ok=False, detail=f"{type(exc).__name__}: {exc}")
    return BotVerification(role=role, ok=True, detail="OK")


def _resolve_app_id(role: str, apps: AppsConfig) -> int:
    """Look up a role's App ID on a v4 :class:`AppsConfig`."""
    creds = getattr(apps, role)
    app_id: int = creds.app_id
    if app_id <= 0:
        raise RuntimeError(f"apps.{role}.app_id is not set (got {app_id})")
    return app_id


def _resolve_key_path(role: str, apps: AppsConfig) -> Path:
    """Look up a role's private-key path on a v4 :class:`AppsConfig`."""
    creds = getattr(apps, role)
    raw: str = creds.private_key_path
    if not raw:
        raise RuntimeError(f"apps.{role}.private_key_path is not set")
    return Path(raw)


# ----------------------------------------------------------------------
# Config writing (string-based, no tomli_w)
# ----------------------------------------------------------------------


def _format_project_block(*, name: str, repo: str, clone_path: Path, check_command: str) -> str:
    """Render a ``[[projects]]`` TOML block.

    ``check_command`` is omitted when it equals the default — the
    Worker resolves None to ``"just check"``, so a project on the
    default doesn't need to repeat it in config. Projects with a
    non-default value emit the line so it's discoverable.
    """
    lines = ["[[projects]]"]
    lines.append(f'name = "{name}"')
    lines.append(f'repo = "{repo}"')
    # Normalize Windows backslashes to forward slashes inside the TOML
    # so the config is portable across OSes (TOML treats backslashes
    # specially in basic strings; forward slashes are unambiguous).
    posix_clone = clone_path.as_posix()
    lines.append(f'local_clone_path = "{posix_clone}"')
    if check_command and check_command != _DEFAULT_CHECK_COMMAND:
        lines.append(f'check_command = "{check_command}"')
    return "\n".join(lines) + "\n"


def _project_block_re(name: str) -> re.Pattern[str]:
    r"""Compile a regex matching the ``[[projects]]`` block for ``name``.

    The match runs from the ``[[projects]]`` header through the start
    of the next top-level block (``[`` at line start) or end of file.
    The block must contain a ``name = "<name>"`` line — we anchor on
    that so the regex only matches blocks belonging to the named project.

    Each repeated content line is anchored to ``^`` in multiline mode
    so we never let ``[^\[].*`` accidentally cross a newline into a
    subsequent ``[…]`` block header. Blank lines DO match (their first
    char is ``\n``, not ``[``).
    """
    escaped = re.escape(name)
    pattern = (
        r"^\[\[projects\]\][^\n]*\n"
        r"(?:^[^\[\n].*\n)*?"
        r"^name = \"" + escaped + r"\"\n"
        r"(?:^[^\[\n].*\n)*"
    )
    return re.compile(pattern, flags=re.MULTILINE)


_INITIAL_CONFIG_TEMPLATE = """\
# Foreman v4 config — written by ``foreman init``.
#
# Edit the [apps.<role>] / [orchestrator] placeholders below to point at
# real GitHub App credentials before starting the daemon. The
# ``foreman init`` command does NOT write real credentials; it leaves
# placeholders so the file is daemon-loadable as a schema but refuses
# to run until the operator wires identity.

[daemon]
log_dir = "{log_dir}"
log_level = "INFO"
tick_seconds = 30.0
max_in_flight = 1
role_timeout_seconds = 600
max_state_attempts = 3

[storage]
engine = "postgres"
dsn = "{storage_dsn}"

[apps.planner]
app_id = 0
private_key_path = ""

[apps.reviewer]
app_id = 0
private_key_path = ""

[apps.fixer]
app_id = 0
private_key_path = ""

[apps.worker]
app_id = 0
private_key_path = ""

[orchestrator]
app_id = 0
private_key_path = ""

"""


def _initial_v4_config_text() -> str:
    """Return the boilerplate written when ``config.toml`` doesn't exist.

    Includes a full V4Config skeleton — ``[daemon]``, ``[apps.<role>]``
    x4, ``[orchestrator]`` — with placeholder values the operator must
    edit before the daemon will accept the file. Trailing newline so the
    first ``[[projects]]`` block appears separated.
    """
    return _INITIAL_CONFIG_TEMPLATE.format(
        log_dir=_DEFAULT_LOG_DIR,
        storage_dsn=_DEFAULT_STORAGE_DSN,
    )


def _write_project_block_to_config(
    *,
    config_path: Path,
    block_text: str,
    name: str,
    force: bool,
) -> None:
    """Append or replace the project block in the v4 config file.

    Behavior:
      * Config missing → create with the full v4 skeleton +
        ``[[projects]]`` block.
      * Block absent → append (separated by a blank line for readability).
      * Block present + ``force`` False → :class:`FileExistsError`
        (the caller should have checked earlier; this is a defense in
        depth so the write-step never silently overwrites).
      * Block present + ``force`` True → replace ONLY the matching
        ``[[projects]]`` block; do not touch sibling blocks or the
        operator-curated ``[apps.<role>]`` / ``[orchestrator]`` blocks.

    String-based to avoid a new ``tomli_w`` dependency. Existing
    project blocks and comments are preserved verbatim.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        skeleton = _initial_v4_config_text()
        config_path.write_text(skeleton + block_text, encoding="utf-8")
        return

    existing = config_path.read_text(encoding="utf-8")
    block_re = _project_block_re(name)
    if block_re.search(existing) is None:
        # Append, separated from the previous content by a blank line.
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        config_path.write_text(existing + sep + block_text, encoding="utf-8")
        return

    if not force:
        # Defense in depth — the orchestrator already raised before we
        # got here, but never trust the caller blindly with a write.
        raise FileExistsError(f"Project block for {name!r} already exists in {config_path}")
    replaced = block_re.sub(block_text, existing, count=1)
    config_path.write_text(replaced, encoding="utf-8")


def _project_block_exists(config_path: Path, name: str) -> bool:
    """Return True iff the config already has a project block for ``name``.

    Checks against ``[[projects]]`` blocks with a matching
    ``name = "<name>"`` line.
    """
    if not config_path.exists():
        return False
    existing = config_path.read_text(encoding="utf-8")
    return _project_block_re(name).search(existing) is not None


def _load_config_or_empty(config_path: Path) -> V4Config | None:
    """Load the v4 config, or return ``None`` when absent / unparseable.

    Returns ``None`` (not a blank V4Config) when the file is absent or
    when parsing raises — V4Config requires ``apps`` + ``orchestrator``,
    so we can't fabricate a default. Callers treat ``None`` as
    "skip per-project bot verification — no usable apps shape yet".
    """
    if not config_path.exists():
        return None
    try:
        return load_config(config_path)
    except Exception as exc:
        # Defensive: config-load failures (bad TOML, missing required
        # blocks) must not crash init. Bot verification falls back to
        # "skipped" for every role and the operator sees a clear summary.
        _log.debug("v4 config at %s unparseable: %s", config_path, exc)
        return None


# ----------------------------------------------------------------------
# Summary rendering
# ----------------------------------------------------------------------


def _format_summary(result: InitResult) -> str:
    """Compose the multi-line ready-to-go summary the CLI prints."""
    bot_lines: list[str] = []
    for v in result.bot_verifications:
        marker = "OK" if v.ok else "FAIL"
        bot_lines.append(f"      {v.role}: {marker} ({v.detail})")
    bots_block = "\n".join(bot_lines) if bot_lines else "      (none verified)"

    instructions_note = (
        "review + customize" if result.instructions_written else "existing file preserved"
    )

    head = (
        f"OK Foreman initialized for {result.repo}\n"
        f"  Config block:  {result.config_path} "
        f"(added [[projects]] name={result.name!r})\n"
        f"  Instructions:  {result.instructions_path} ({instructions_note})\n"
        f"  Labels:        {len(_FOREMAN_LABELS)} labels total on {result.repo} "
        f"({len(result.labels_created)} newly created, "
        f"{len(result.labels_existing)} already existed)\n"
        f"  Bots:\n{bots_block}\n"
    )
    next_steps = (
        "\n"
        "Next steps:\n"
        f"  1. Edit {result.config_path} — fill in real app_id /\n"
        "     private_key_path for [apps.<role>] and [orchestrator].\n"
        f"  2. Review and customize {result.instructions_path}\n"
        "  3. Label an issue with foreman:plan and start the daemon:\n"
        "     foreman daemon start"
    )
    if result.instructions_dirty_warning:
        warning_block = (
            "\n"
            "Warning: .foreman/INSTRUCTIONS.md is untracked or modified in the\n"
            "clone. Commit it now to avoid a dirty-tree trip on the next build:\n"
            f"  {result.instructions_dirty_warning}\n"
        )
        return head + warning_block + next_steps
    return head + next_steps


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------


def _find_project(config: V4Config, name: str) -> AppsConfig | None:
    """Return the ``apps`` block usable for bot verification.

    V4Config carries a single top-level :class:`AppsConfig`; the
    ``name`` argument is accepted (and unused beyond a presence check)
    so the caller's intent stays clear at the call site.
    """
    _ = name  # presence-only — v4 apps are top-level, not per-project
    return config.apps


def run_init(
    init_config: InitConfig,
    *,
    admin_client: Github,
) -> InitResult:
    """Run the full init flow against ``init_config.repo``.

    Args:
        init_config: Parsed CLI inputs (repo, name, paths, flags).
        admin_client: A :class:`Github` client authenticated as a
            human / admin PAT with write access to the target repo.
            Used for label creation. Passed in (not constructed here)
            so tests can inject a fake without monkey-patching
            ``Github(...)``.

    Returns:
        Structured :class:`InitResult`. The CLI prints ``.summary``;
        tests assert against the typed fields.

    Raises:
        ValueError: Args are invalid (bad repo slug, clone-path
            mismatch, etc.).
        FileExistsError: A matching ``[[projects]]`` block already
            exists and ``force`` was not set.
    """
    owner, _repo_name = _validate_repo_slug(init_config.repo)
    _ = owner  # presence is enough; consumed by _validate_repo_slug
    _validate_clone_path(init_config.clone_path, init_config.repo)

    # Refuse to overwrite without --force. We do this BEFORE any
    # side-effects (no labels created, no files written) so re-running
    # with the wrong flag leaves the world unchanged.
    if _project_block_exists(init_config.config_path, init_config.name) and not init_config.force:
        raise FileExistsError(
            f"Project '{init_config.name}' already configured in "
            f"{init_config.config_path}. Use --force to overwrite."
        )

    # Step 3: instructions template.
    instructions_path, instructions_written = _write_instructions_template(
        clone_path=init_config.clone_path,
        repo_name=init_config.repo.split("/", 1)[1],
        check_command=init_config.check_command,
    )
    # Detect whether the just-written template is committed in the
    # clone. The check is defensive — any failure returns None — so it
    # cannot block init from completing.
    instructions_dirty_warning = _check_instructions_committed(init_config.clone_path)

    # Step 4: labels (idempotent).
    labels_created, labels_existing = _ensure_labels(
        client=admin_client, repo_slug=init_config.repo
    )

    # Step 5: best-effort bot verification. Read the apps block from
    # the existing config (if any). v4 apps are top-level — shared
    # across every project on this host — so they don't need to be
    # re-resolved per project. When the config is absent or
    # unparseable, every role is reported as ``skipped``.
    existing_config = _load_config_or_empty(init_config.config_path)
    apps = existing_config.apps if existing_config is not None else None
    bot_verifications: list[BotVerification] = []
    for role in _ROLE_NAMES:
        if apps is None:
            bot_verifications.append(
                BotVerification(
                    role=role,
                    ok=False,
                    detail="skipped: no v4 apps config on disk yet",
                )
            )
            continue
        bot_verifications.append(
            _verify_bot_installation(role=role, apps=apps, repo_slug=init_config.repo)
        )

    # Step 6: write the project block. We intentionally do this LAST so
    # any failure above leaves the config untouched.
    block_text = _format_project_block(
        name=init_config.name,
        repo=init_config.repo,
        clone_path=init_config.clone_path,
        check_command=init_config.check_command,
    )
    _write_project_block_to_config(
        config_path=init_config.config_path,
        block_text=block_text,
        name=init_config.name,
        force=init_config.force,
    )

    result = InitResult(
        repo=init_config.repo,
        name=init_config.name,
        clone_path=init_config.clone_path,
        config_path=init_config.config_path,
        instructions_path=instructions_path,
        instructions_written=instructions_written,
        labels_created=labels_created,
        labels_existing=labels_existing,
        bot_verifications=bot_verifications,
        instructions_dirty_warning=instructions_dirty_warning,
    )
    result.summary = _format_summary(result)
    return result


__all__ = [
    "BotVerification",
    "InitConfig",
    "InitResult",
    "detect_matching_clone",
    "run_init",
]
