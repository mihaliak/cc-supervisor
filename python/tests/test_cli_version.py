from __future__ import annotations

import pytest

from ccs import __version__
from ccs.cli import main


def test_version_prints_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"ccs {__version__}"


def test_version_is_0_1_0() -> None:
    assert __version__ == "0.1.0"
