"""Smoke test for the foreman.v4 package."""
import importlib


def test_v4_package_importable():
    module = importlib.import_module("foreman.v4")
    assert module is not None
