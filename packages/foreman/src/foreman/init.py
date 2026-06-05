"""``foreman init`` — onboard a new repo onto Foreman.

The init flow is a one-shot setup pass that:

  1. Validates the target repo + clone path
  2. Refuses to overwrite an existing ``[projects.<name>]`` config block
     unless ``--force`` is passed
  3. Writes a ``.foreman/INSTRUCTIONS.md`` template into the local clone
     (skipping if one already exists — even with ``--force``)
  4. Creates the Foreman state + modifier + attempt labels on the
     target repo (idempotent: existing labels are left alone)
  5. Best-effort verifies that each role's GitHub App can mint an
     installation token against the target repo
  6. Appends the ``[projects.<name>]`` block to ``~/.foreman/config.toml``
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
* The ``[projects.<name>.apps]`` block is NOT written. App IDs are
  per-bot and operator-managed; the summary points at the existing
  ``voice`` config as a reference shape.
* TOML writing uses string manipulation rather than ``tomli_w`` (not a
  current dep). Existing project blocks and comments are preserved
  verbatim — we only append the new block (or replace it block-by-block
  under ``--force``).
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
from foreman.config import AppsConfig, Config, load_config

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

# Default config path. Mirrors ``cli._default_config_path`` but without
# requiring click in this module.
_DEFAULT_CONFIG_PATH = Path.home() / ".foreman" / "config.toml"

# The Foreman labels created on the target repo. Order is intentional:
# state labels first (in v3 pipeline order), then modifier labels, then
# attempt counters. The structure mirrors the operator's mental model of
# the pipeline rather than alphabetic order. Keep in sync with the
# v3 reconciler rule catalog + role modules.
_FOREMAN_LABELS: list[tuple[str, str, str]] = [
    # name, color (no leading '#'), description
    ("foreman:planning", "FBCA04", "Foreman: spec phase (Planner + Reviewer)"),
    (
        "foreman:plan-approved",
        "0E8A16",
        "Foreman: spec approved, queued for Worker",
    ),
    ("foreman:spec-fix", "D93F0B", "Foreman: spec PR needs human follow-up"),
    ("foreman:impl-review", "FBCA04", "Foreman: impl PR ready for Reviewer"),
    (
        "foreman:impl-approved",
        "0E8A16",
        "Foreman: impl approved, queued for merge",
    ),
    ("foreman:impl-fix", "D93F0B", "Foreman: impl PR needs Fixer follow-up"),
    (
        "foreman:needs-help",
        "FBCA04",
        "Foreman: surfaced for human intervention",
    ),
    ("foreman:hold", "BFD4F2", "Foreman: manual pause (blocks all rules)"),
    ("foreman:done", "6F42C1", "Foreman: ticket complete"),
    ("foreman:failed", "B60205", "Foreman: ticket exhausted retries (terminal)"),
    ("foreman:impl-attempt-1", "BFD4F2", "Foreman: impl cycle attempt 1 of 3"),
    ("foreman:impl-attempt-2", "BFD4F2", "Foreman: impl cycle attempt 2 of 3"),
    ("foreman:impl-attempt-3", "BFD4F2", "Foreman: impl cycle attempt 3 of 3"),
    ("foreman:fix-attempt-1", "BFD4F2", "Foreman: fix cycle attempt 1 of 3"),
    ("foreman:fix-attempt-2", "BFD4F2", "Foreman: fix cycle attempt 2 of 3"),
    ("foreman:fix-attempt-3", "BFD4F2", "Foreman: fix cycle attempt 3 of 3"),
]

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
    """Project name used as the ``[projects.<name>]`` key in the config."""

    clone_path: Path
    """Absolute path to the local clone on disk."""

    check_command: str
    """Quality gate command; written into the config block only when
    different from the default (see :func:`run_init`)."""

    force: bool
    """When True, overwrite an existing ``[projects.<name>]`` block."""

    config_path: Path
    """Path to ``~/.foreman/config.toml`` (or override)."""


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
    """Verify that ``clone_path`` exists, is a git repo, and its ``origin``
    remote points at ``expected_repo``.

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
    """Return ``cwd`` if it is a git repo whose origin matches
    ``expected_repo``, else ``None``.

    Used by the CLI to default ``--clone-path`` when the operator runs
    ``foreman init owner/repo`` from inside that repo's clone. Never
    raises — a non-matching cwd just returns None.
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
    for name, color, description in _FOREMAN_LABELS:
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

    Skipped (with a clear ``detail``) when the role's App ID is not
    configured — the operator may set up apps incrementally. Failures
    (network errors, missing key file, installation absent) are
    recorded but do not raise; init continues with the next role.
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
    """Look up a role's App ID via :class:`AppsConfig`'s resolvers."""
    resolver_name = f"resolve_{role}_app_id"
    resolver = getattr(apps, resolver_name)
    result: int = resolver()
    return result


def _resolve_key_path(role: str, apps: AppsConfig) -> Path:
    """Look up a role's private-key path via :class:`AppsConfig`."""
    resolver_name = f"resolve_{role}_private_key_path"
    resolver = getattr(apps, resolver_name)
    result: Path = resolver()
    return result


# ----------------------------------------------------------------------
# Config writing (string-based, no tomli_w)
# ----------------------------------------------------------------------


def _format_project_block(*, name: str, repo: str, clone_path: Path, check_command: str) -> str:
    """Render a ``[projects.<name>]`` TOML block.

    ``check_command`` is omitted when it equals the default — the
    Worker resolves None to ``"just check"``, so a project on the
    default doesn't need to repeat it in config. Projects with a
    non-default value emit the line so it's discoverable.
    """
    lines = [f"[projects.{name}]"]
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
    """Compile a regex matching the ``[projects.<name>]`` block.

    The match runs from the block header through the start of the next
    top-level block (``[`` at line start) or end of file. Sub-blocks
    like ``[projects.<name>.apps]`` are NOT included — those are
    operator-curated and we deliberately leave them untouched on
    overwrite (so the bot config survives a re-init).

    Each repeated content line is anchored to ``^`` in multiline mode
    so we never let ``[^\\[].*`` accidentally cross a newline into a
    subsequent ``[…]`` block header. Blank lines DO match (their first
    char is ``\\n``, not ``[``), which is what we want — operators
    typically leave a blank line between the main block and the
    ``.apps`` sub-block.
    """
    pattern = r"^\[projects\." + re.escape(name) + r"\][^\n]*\n" + r"(?:^[^\[\n].*\n)*"
    return re.compile(pattern, flags=re.MULTILINE)


def _write_project_block_to_config(
    *,
    config_path: Path,
    block_text: str,
    name: str,
    force: bool,
) -> None:
    """Append or replace the project block in the config file.

    Behavior:
      * Config missing → create with just this block.
      * Block absent → append (separated by a blank line for readability).
      * Block present + ``force`` False → :class:`FileExistsError`
        (the caller should have checked earlier; this is a defense in
        depth so the write-step never silently overwrites).
      * Block present + ``force`` True → replace ONLY the
        ``[projects.<name>]`` block; do not touch the sibling
        ``[projects.<name>.apps]`` block (operator-managed).

    String-based to avoid a new ``tomli_w`` dependency. Existing
    project blocks and comments are preserved verbatim.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(block_text, encoding="utf-8")
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
        raise FileExistsError(f"Project block [projects.{name}] already exists in {config_path}")
    replaced = block_re.sub(block_text, existing, count=1)
    config_path.write_text(replaced, encoding="utf-8")


def _project_block_exists(config_path: Path, name: str) -> bool:
    """Return True iff ``[projects.<name>]`` is already in the config."""
    if not config_path.exists():
        return False
    existing = config_path.read_text(encoding="utf-8")
    return _project_block_re(name).search(existing) is not None


def _load_config_or_empty(config_path: Path) -> Config:
    """Load the config or return an empty one when the file is absent.

    Used to read the existing ``[projects.<name>.apps]`` block (if any)
    so bot verification can run against operator-supplied App IDs even
    on a fresh-but-partial init.
    """
    if not config_path.exists():
        return Config()
    return load_config(config_path)


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

    summary = (
        f"OK Foreman initialized for {result.repo}\n"
        f"  Config block:  {result.config_path} "
        f"(added [projects.{result.name}])\n"
        f"  Instructions:  {result.instructions_path} ({instructions_note})\n"
        f"  Labels:        {len(_FOREMAN_LABELS)} labels total on {result.repo} "
        f"({len(result.labels_created)} newly created, "
        f"{len(result.labels_existing)} already existed)\n"
        f"  Bots:\n{bots_block}\n"
        "\n"
        "Next steps:\n"
        f"  1. Add the [projects.{result.name}.apps] block to "
        f"{result.config_path} with your\n"
        "     bot App IDs (see an existing project's apps block for reference).\n"
        f"  2. Review and customize {result.instructions_path}\n"
        "  3. Label an issue with foreman:planning and run:\n"
        f"     foreman plan https://github.com/{result.repo}/issues/<N> "
        f"--project {result.name}"
    )
    return summary


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------


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
        FileExistsError: ``[projects.<name>]`` already exists and
            ``force`` was not set.
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

    # Step 4: labels (idempotent).
    labels_created, labels_existing = _ensure_labels(
        client=admin_client, repo_slug=init_config.repo
    )

    # Step 5: best-effort bot verification. Read the apps block from
    # the existing config (if any) so operators who already populated
    # ``[projects.<name>.apps]`` benefit from verification on re-init.
    existing_config = _load_config_or_empty(init_config.config_path)
    apps = (
        existing_config.projects[init_config.name].apps
        if init_config.name in existing_config.projects
        else AppsConfig()
    )
    bot_verifications = [
        _verify_bot_installation(role=role, apps=apps, repo_slug=init_config.repo)
        for role in _ROLE_NAMES
    ]

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
