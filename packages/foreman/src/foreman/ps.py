"""Operator-facing read-only views over the daemon's SQLite store."""

from __future__ import annotations

from foreman.storage import Storage


def format_active_pipelines(storage: Storage) -> str:
    """Return a human-readable table of non-terminated pipelines."""
    lines = ["PROJECT       ISSUE   STATE                  STARTED"]
    for row in storage.iter_pipelines_in_flight():
        lines.append(
            f"{row['project']:<13} #{row['issue_number']:<5} "
            f"{row['current_state'][:22]:<22} {row['started_at']}"
        )
    if len(lines) == 1:
        lines.append("(no active pipelines)")
    return "\n".join(lines)


def format_pipeline_detail(storage: Storage, project: str, issue_number: int) -> str:
    """Return a human-readable detail view of one pipeline."""
    pipeline = storage.get_pipeline(project, issue_number)
    if pipeline is None:
        return f"No pipeline found for {project}#{issue_number}."

    lines = [
        f"Pipeline {project}#{issue_number}:",
        f"  Current state: {pipeline['current_state']}",
        f"  Started: {pipeline['started_at']}",
        f"  Terminated: {pipeline['terminated_at'] or '(in flight)'}",
        "",
        "Node runs:",
    ]
    with storage.connect() as conn:
        node_runs = list(
            conn.execute(
                "SELECT * FROM node_runs WHERE pipeline_id = ? ORDER BY started_at",
                (pipeline["id"],),
            )
        )
        transitions = list(
            conn.execute(
                "SELECT * FROM transitions WHERE pipeline_id = ? ORDER BY at",
                (pipeline["id"],),
            )
        )

    for run in node_runs:
        lines.append(
            f"  [{run['started_at']}] {run['role']:<10} "
            f"{run['outcome'] or '(running)'}"
        )
    lines.append("")
    lines.append("Transitions:")
    for tr in transitions:
        lines.append(
            f"  [{tr['at']}] {tr['actor']:<10} "
            f"{tr['from_labels_json']} -> {tr['to_labels_json']}"
        )

    return "\n".join(lines)
