"""Statusline: pure render, embeddable runtime, script generator, settings apply/revert (P07).

Launcher hook (P05): `ccs.statusline.apply.ensure_statusline(profile, config=None)` makes the
script current and returns the `statusLine` command for `--settings` (`None` when disabled).
Lower level: `template.ensure_script(profile, config) -> Path` + `template.statusline_command`.
"""
