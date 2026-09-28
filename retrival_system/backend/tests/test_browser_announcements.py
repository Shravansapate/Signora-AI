"""Typed, structured and recorded input through a production Next.js build and actual ASR."""

# ruff: noqa: F811 -- imported module fixtures are scoped to this test's isolated database.
import os

import pytest
from test_announcements import announcement_setup, approved_template  # noqa: F401
from test_browser_playback import run_browser
from test_lifecycle import lifecycle_clients  # noqa: F401
from test_registry import registry_database  # noqa: F401


@pytest.mark.skipif(os.environ.get("SIGNORA_TEST_BROWSER") != "1", reason="Opt-in real browser")
def test_real_browser_common_announcement_input(registry_database, approved_template, tmp_path):
    run_browser(
        registry_database[0], tmp_path, script="verify-announcement-input.mjs", announcements=True
    )
