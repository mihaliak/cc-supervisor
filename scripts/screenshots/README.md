# Screenshot tooling

README screenshots come from an anonymized demo environment, never from real accounts.

- `demo_env.py --root DIR [--exact]` wipes and rebuilds DIR: a fake HOME with `~/.claude`, `~/.claude-work` and `~/.claude-client` (signed in as `you@example.com`, `you@work.example` and `you@client.example` through the repo's fake `claude`), a config with 3 profiles, and state written by ccs's own code (usage, a session pause, sessions, warm-ups, events, widget snapshot), with times relative to now. It prints the `export` lines to use it. Resets fall on whole hours; `--exact` gives exact countdowns such as `in 2h 13m`.
- `capture_cli.sh DIR OUT [--exact]` rebuilds DIR, runs a demo daemon on it (`demo_env.py --serve`: the real daemon against the fake `claude`, with a `launchctl` stub instead of launchd), then writes `OUT/<name>.ansi` and `OUT/<name>.cmd`. Paths are mapped to `~`, and the run fails if any capture contains personal data. A full run takes about 4 s.
- `run.sh` (`make screenshots`) does it all. It builds `macos/Screenshots/`, the app, shared and widget sources compiled with `-D SCREENSHOTS`, into `build/screenshots/harness`. Then it creates the demo env and the captures, and renders the real SwiftUI views and the captures into `docs/assets/screenshots/`. `ONLY=widgets,menu` renders a subset.
- Times use the local time zone; set `TZ` for reproducible shots. Keep DIR short (for example `/tmp/ccs-demo`): the daemon socket path is limited to about 100 characters.
