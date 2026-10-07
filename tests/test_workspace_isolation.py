"""Offline guarantees for the migrated checkout, without inspecting credentials."""
from pathlib import Path
import importlib.util
import sys

import pytest

from goalkeeper.paths import DATA, PRIVATE, PROJECT, inside_project


@pytest.mark.parametrize('outside', [
    str(PROJECT.parent / 'other-project'),
    str(PROJECT.parent / 'previous-version'),
    str(PROJECT.parent / 'original-project'),
])
def test_other_versions_are_outside_current_project(outside):
    with pytest.raises(ValueError, match='当前项目'):
        inside_project(Path(outside))


def test_imports_virtual_environment_and_runtime_are_local():
    assert Path(sys.prefix).resolve() == (PROJECT / '.venv').resolve()
    assert DATA == PROJECT / 'runtime' and PRIVATE == PROJECT / 'runtime/private'
    for name in ('engine', 'provider', 'sandbox', 'tools', 'evaluation', 'native'):
        assert Path(importlib.util.find_spec('goalkeeper.'+name).origin).resolve().is_relative_to(PROJECT)
    assert importlib.util.find_spec('goalkeeper.connection_diagnostics') is None
    assert not (PROJECT / 'frontend').exists()
