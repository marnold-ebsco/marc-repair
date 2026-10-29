"""Shared fixtures."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import marc_repair  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _no_real_profiles(tmp_path_factory):
    """Tests never pick up the profiles/ directory in the checkout (which holds a user's own
    profiles): main() would otherwise show a menu, or error without a terminal. Session
    scoped so class/module-scoped fixtures that call main() are covered too."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            marc_repair, "DEFAULT_PROFILE_DIR", str(tmp_path_factory.mktemp("no_profiles"))
        )
        yield
