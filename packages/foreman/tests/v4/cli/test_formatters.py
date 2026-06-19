"""Strategy pattern for output formatting — table | json | yaml."""
from __future__ import annotations

import json

import pytest

from foreman.v4.cli.formatters import (
    JsonFormatter,
    TableFormatter,
    YamlFormatter,
    get_formatter,
)

_ROWS = [
    {"id": 1, "project": "p", "state": "Planning"},
    {"id": 2, "project": "p", "state": "Done"},
]


def test_get_formatter_returns_correct_strategy():
    assert isinstance(get_formatter("table"), TableFormatter)
    assert isinstance(get_formatter("json"), JsonFormatter)
    assert isinstance(get_formatter("yaml"), YamlFormatter)


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        get_formatter("xml")


def test_json_formatter_round_trips():
    out = JsonFormatter().format(_ROWS)
    parsed = json.loads(out)
    assert parsed == _ROWS


def test_yaml_formatter_emits_valid_yaml():
    import yaml
    out = YamlFormatter().format(_ROWS)
    assert yaml.safe_load(out) == _ROWS


def test_table_formatter_includes_column_headers():
    out = TableFormatter().format(_ROWS)
    # Rich.Table renders with column names somewhere; loose assertion
    # avoids over-fitting to escape codes.
    plain = out.replace("\x1b[", "").lower()
    assert "id" in plain and "project" in plain and "state" in plain


def test_table_formatter_empty_rows_is_empty_table():
    out = TableFormatter().format([])
    # No exception; some kind of "no data" affordance is fine.
    assert isinstance(out, str)
