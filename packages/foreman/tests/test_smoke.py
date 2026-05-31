"""Smoke test — verifies the package is importable and CI has something to run."""

import foreman


def test_package_importable() -> None:
    """foreman must be importable; the test exists so CI has a passing test on scaffold."""
    assert foreman.__doc__ is not None
