"""Launcher pre-parse (P05): profile selection, ccs-only options, passthrough, typos."""

from __future__ import annotations

from typing import Any

import pytest

from ccs.cli import parse_launcher_args
from ccs.config.models import Config
from ccs.launcher.args import LaunchArgsError, edit_distance, is_management, typo_suggestion


def make_config(
    default: str | None = "personal", flags: tuple[str, ...] = ("personal", "work")
) -> Config:
    raw: dict[str, Any] = {
        "version": 1,
        "revision": 0,
        "default_profile": default,
        "profiles": [
            {"id": f, "flag": f, "name": f.title(), "emoji": "🏠", "config_dir": f"/tmp/ccs-{f}"}
            for f in flags
        ],
    }
    return Config.from_dict(raw)


CFG = make_config()


@pytest.mark.parametrize(
    ("argv", "profile", "force", "no_supervise", "claude_args"),
    [
        ([], "personal", False, False, []),
        (["--work"], "work", False, False, []),
        (["--work", "--force", "-c"], "work", True, False, ["-c"]),
        (["--force"], "personal", True, False, []),
        (["--no-supervise", "-c"], "personal", False, True, ["-c"]),
        (["--force", "--work"], "work", True, False, []),
        (["--profile", "work", "--resume", "abc"], "work", False, False, ["--resume", "abc"]),
        (["--profile=work", "-c"], "work", False, False, ["-c"]),
        (["--resume"], "personal", False, False, ["--resume"]),
        (["-c", "--work"], "personal", False, False, ["-c", "--work"]),
        (["--work", "--", "--force"], "work", False, False, ["--", "--force"]),
        (["--work", "--model", "haiku"], "work", False, False, ["--model", "haiku"]),
        (["--work", "hello world"], "work", False, False, ["hello world"]),
        (["--work", "--work"], "work", False, False, []),
    ],
)
def test_parse_table(
    argv: list[str], profile: str, force: bool, no_supervise: bool, claude_args: list[str]
) -> None:
    spec = parse_launcher_args(argv, CFG)
    assert spec is not None
    assert spec.profile.id == profile
    assert spec.force is force
    assert spec.no_supervise is no_supervise
    assert list(spec.claude_args) == claude_args


def test_two_profiles_is_an_error() -> None:
    with pytest.raises(LaunchArgsError, match="two profiles"):
        parse_launcher_args(["--work", "--personal"], CFG)


def test_typo_suggestion() -> None:
    with pytest.raises(LaunchArgsError, match=r"unknown profile '--wrok'\. Did you mean --work\?"):
        parse_launcher_args(["--wrok"], CFG)


def test_claude_option_is_not_a_typo() -> None:
    # `--model` is a claude option: never treated as a profile typo
    spec = parse_launcher_args(["--model", "haiku"], CFG)
    assert spec is not None and list(spec.claude_args) == ["--model", "haiku"]


def test_far_unknown_flag_passes_to_claude() -> None:
    spec = parse_launcher_args(["--totally-unknown"], CFG)
    assert spec is not None and list(spec.claude_args) == ["--totally-unknown"]


@pytest.mark.parametrize("argv", [["status"], ["--version"], ["-h"], ["--help"], ["daemon", "run"]])
def test_management_returns_none(argv: list[str]) -> None:
    assert is_management(argv)
    assert parse_launcher_args(argv, CFG) is None


def test_unknown_profile_id() -> None:
    with pytest.raises(LaunchArgsError, match="unknown profile 'nope'"):
        parse_launcher_args(["--profile", "nope"], CFG)


def test_profile_needs_value() -> None:
    with pytest.raises(LaunchArgsError, match="needs a profile id"):
        parse_launcher_args(["--profile"], CFG)


def test_no_default_single_profile_is_used() -> None:
    cfg = make_config(default=None, flags=("work",))
    spec = parse_launcher_args([], cfg)
    assert spec is not None and spec.profile.id == "work"


def test_no_default_several_profiles_errors() -> None:
    cfg = make_config(default=None)
    with pytest.raises(LaunchArgsError, match="no default profile"):
        parse_launcher_args([], cfg)


def test_edit_distance() -> None:
    assert edit_distance("wrok", "work") == 2
    assert edit_distance("work", "work") == 0
    assert edit_distance("", "ab") == 2


def test_short_flags_do_not_attract_typos() -> None:
    cfg = make_config(flags=("ai", "work"))
    assert typo_suggestion("p", cfg) is None  # distance 2 == len("ai") → not a typo
