"""Generates the self-contained per-profile statusline script (`<config_dir>/ccs-statusline.py`).

The script embeds the source of `ccs.timefmt`, `ccs.clock`, `ccs.statusline.render` and
`ccs.statusline.runtime` plus baked profile constants, so it runs with the stdlib only via
`<abs python> -S -E <script>` (ADR-0006/0013) — no site-packages, no `ccs` install needed.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccs import __version__, paths
from ccs.config.models import Config, Profile
from ccs.fsio import atomic_write_text
from ccs.statusline.render import fallback_line

# 2: constants baked as a JSON string parsed inside the loader's `try` (never a NameError)
GENERATOR_VERSION = 2
SCRIPT_NAME = "ccs-statusline.py"
SCRIPT_MODE = 0o755
EMBEDDED_MODULES = (
    "ccs.timefmt",
    "ccs.clock",
    "ccs.statusline.render",
    "ccs.statusline.runtime",
)
HEADER_RE = re.compile(
    r"^# ccs-statusline generator=(?P<generator>\d+) ccs=(?P<ccs>\S+) "
    r"profile=(?P<profile>\S+) sources=(?P<sources>[0-9a-f]+)$",
    re.MULTILINE,
)
SHEBANG_FLAGS = " -S -E"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

_LOADER = """

def _ccs_boot():
    import sys
    import types

    for pkg in ("ccs", "ccs.statusline"):
        module = types.ModuleType(pkg)
        module.__path__ = []
        sys.modules[pkg] = module
    sys.modules["ccs"].statusline = sys.modules["ccs.statusline"]
    for name, source in _SOURCES.items():
        module = types.ModuleType(name)
        module.__file__ = __file__
        sys.modules[name] = module
        parent, _, leaf = name.rpartition(".")
        setattr(sys.modules[parent], leaf, module)
        exec(compile(source, __file__ + ":" + name, "exec"), module.__dict__)
    return sys.modules["ccs.statusline.runtime"]


if __name__ == "__main__":
    try:
        import json

        CONSTANTS = json.loads(_CONSTANTS_JSON)
        _runtime = _ccs_boot()
    except Exception:
        print(_FALLBACK)
        raise SystemExit(0)
    raise SystemExit(_runtime.main(CONSTANTS))
"""


@dataclass(frozen=True)
class GeneratedScript:
    """Result of `generate`: where the script lives and whether the file changed."""

    profile_id: str
    path: Path
    content: str
    generator_version: int
    sources_hash: str
    changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "script_path": str(self.path),
            "generator_version": self.generator_version,
            "sources_hash": self.sources_hash,
            "changed": self.changed,
        }


def module_source(name: str) -> str:
    """Source text of an embedded module (read from its file)."""
    module = importlib.import_module(name)
    file = getattr(module, "__file__", None)
    if not file:
        raise RuntimeError(f"module {name} has no source file")
    return Path(file).read_text(encoding="utf-8")


def embedded_sources() -> dict[str, str]:
    """`{module name: source}` in dependency (exec) order."""
    return {name: module_source(name) for name in EMBEDDED_MODULES}


def sources_hash(sources: dict[str, str]) -> str:
    """First 12 hex chars of sha256 over the embedded sources."""
    digest = hashlib.sha256()
    for name, source in sources.items():
        digest.update(name.encode("utf-8") + b"\0" + source.encode("utf-8") + b"\0")
    return digest.hexdigest()[:12]


def interpreter() -> str:
    """The Python that runs the script: this `ccs` install's interpreter (pipx venv)."""
    return sys.executable


def script_path(profile: Profile) -> Path:
    """`<config_dir>/ccs-statusline.py` (config dir as given: `~` expanded, symlinks kept)."""
    return Path(profile.config_dir_env) / SCRIPT_NAME


def statusline_command(script: Path, python: str | None = None) -> str:
    """Exactly `<abs python> -S -E <script>` (shell-quoted only when a path needs it)."""
    parts = (python or interpreter(), "-S", "-E", str(script))
    return " ".join(shlex.quote(part) for part in parts)


def statusline_setting(script: Path, python: str | None = None) -> dict[str, str]:
    """The `statusLine` settings value for `script`."""
    return {"type": "command", "command": statusline_command(script, python)}


def constants_for(profile: Profile, config: Config) -> dict[str, Any]:
    """Values baked into the script (profile identity, thresholds, colors, state dir)."""
    colors = config.display.colors
    return {
        "generator_version": GENERATOR_VERSION,
        "profile": {"id": profile.id, "name": profile.name, "emoji": profile.emoji},
        "limits": profile.limits.to_dict(),
        "colors": {"yellow_from": colors.yellow_from, "red_from": colors.red_from},
        "state_dir": str(paths.state_dir()),
    }


def _comment(text: str) -> str:
    """`text` safe for one `#` comment line: control characters escaped (`\\n`), never raw."""
    return _CONTROL_RE.sub(lambda m: repr(m.group())[1:-1], text)


def render_script(profile: Profile, config: Config, *, python: str | None = None) -> str:
    """The full script text for `profile`."""
    sources = embedded_sources()
    digest = sources_hash(sources)
    lines = [
        f"#!{python or interpreter()}{SHEBANG_FLAGS}",
        f"# ccs-statusline generator={GENERATOR_VERSION} ccs={__version__} "
        f"profile={profile.id} sources={digest}",
        "# Generated by `ccs statusline generate`. Do not edit: it is overwritten.",
        f"# Run as: {_comment(statusline_command(script_path(profile), python))}",
        "",
        # Plain str literals only: whatever the values (even a stray `inf`), these lines can't
        # fail at import, and the fallback line needs nothing else.
        f"_CONSTANTS_JSON = {json.dumps(constants_for(profile, config), ensure_ascii=False)!r}",
        f"_FALLBACK = {fallback_line(profile.name, profile.emoji)!r}",
        "",
        "_SOURCES = {",
    ]
    lines += [f"    {name!r}: {source!r}," for name, source in sources.items()]
    lines.append("}")
    return "\n".join(lines) + _LOADER


def parse_shebang(text: str) -> str | None:
    """The interpreter of a script's `#!` line, as `render_script` writes it (spaces allowed).

    Without our ` -S -E` suffix it is the first word (a foreign script).
    """
    first = text.split("\n", 1)[0].rstrip()
    if not first.startswith("#!"):
        return None
    body = first[2:]
    if body.endswith(SHEBANG_FLAGS):
        return body[: -len(SHEBANG_FLAGS)].strip() or None
    words = body.split()
    return words[0] if words else None


def parse_header(text: str) -> dict[str, str] | None:
    """`{generator, ccs, profile, sources}` from a generated script's header, if present."""
    match = HEADER_RE.search(text[:4096])
    return match.groupdict() if match else None


def generate(profile: Profile, config: Config, *, python: str | None = None) -> GeneratedScript:
    """Write the script when its content differs (atomic, mode 0755). Never touches settings."""
    path = script_path(profile)
    content = render_script(profile, config, python=python)
    try:
        existing: str | None = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        existing = None
    changed = existing != content
    if changed:
        atomic_write_text(path, content, SCRIPT_MODE)
    header = parse_header(content)
    return GeneratedScript(
        profile_id=profile.id,
        path=path,
        content=content,
        generator_version=GENERATOR_VERSION,
        sources_hash=header["sources"] if header else "",
        changed=changed,
    )


def ensure_script(profile: Profile, config: Config) -> Path:
    """Make sure the script exists and is current (regenerates on any difference); its path.

    The launcher (P05) calls this before injecting `--settings` with `statusline_command(...)`.
    """
    return generate(profile, config).path


def script_is_current(profile: Profile, config: Config) -> bool:
    """True when the script file exists with exactly the content `generate` would write."""
    try:
        existing = script_path(profile).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return existing == render_script(profile, config)
