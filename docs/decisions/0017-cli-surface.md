# ADR-0017: `ccs` CLI surface

- Status: accepted
- Date: 2026-09-24
- Source: user requirement (`ccs --work`, `ccs --personal`) + planner design

## Decision
The canonical command set. Every command the app calls supports `--json`, which prints clean JSON on stdout with exit code 0/1.

```
# launcher (ADR-0006) — first argument selects the profile, the rest passes to claude
ccs [--<flag> | --profile <id>] [--force] [--no-supervise] [claude args…]

# observe
ccs status    [--profile <id>] [--json]            # usage + supervisor state + daemon health
ccs usage     [--profile <id>] [--refresh] [--json]
ccs sessions  [--profile <id>] [--json]            # supervised + other sessions (claude agents --json)
ccs events    [--follow] [--json]

# control
ccs pause     (--profile <id> | --session <wrapper_id>) [--json]
ccs resume    (--profile <id> | --session <wrapper_id>) [--json]
ccs warmup    (--profile <id> | --all) [--trigger manual|app_start|unlock_wake|schedule|auto_chain] [--force] [--json]

# setup
ccs auth       login [--terminal] | status | logout   --profile <id> [--json]
ccs statusline generate | apply | revert | preview    --profile <id> [--json]
ccs profile    list | show <id> | add … | remove <id> | set <id> <dotted.key>=<value>… [--json]
ccs config     path | show | validate | defaults | set <dotted.key>=<value>… [--json]
ccs daemon     install | uninstall | start | stop | restart | status | run | logs [--json]
ccs doctor     [--json]
ccs --version
```

- `ccs profile add --id <id> --flag <flag> --name <name> --emoji <emoji> --config-dir <dir> [--default]`. Everything else takes its default and can be changed with `set`. `--default` also makes it the `default_profile`.
- `ccs profile remove <id> [--default <other>]` removes only the config entry (no config-dir deletion, no sign-out). It refuses to remove the default profile while others exist unless `--default <other>` names the new default.
- `ccs profile set work limits.session.pause=92 warmup.model=haiku`. Values are parsed as JSON when valid, otherwise as strings.
- `ccs config set default_profile=work display.menu_bar=icon_only` changes top-level keys only. `profiles` is rejected (use `ccs profile …`).
- `ccs daemon run` is what launchd executes (foreground). Humans use `start|stop|restart`.
- `--trigger` defaults to `manual`.

## Rules for implementers
- Adding or changing a command means updating this ADR (superseding it if the change breaks behavior) and `docs/manual/05-ccs-cli.md`.
