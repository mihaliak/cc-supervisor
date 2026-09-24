# Daemon socket protocol (proto 1)

Contract between `ccs daemon run` and its clients: the launcher (P05), the app (P10), and the CLI. Decided in ADR-0005.

- **Socket:** `~/.local/state/ccs/daemon.sock` (`$CCS_STATE_DIR` / `$XDG_STATE_HOME/ccs`), mode 0600. Keep the path under ~104 bytes.
- **Framing:** JSON Lines. One UTF-8 JSON object per line, at most 1 MB per line; a longer line gets `line_too_long` and the connection closes.
- **Envelope:** every message carries `"proto": 1`. A different `proto` gets `proto_mismatch` and the connection closes.
- **Handshake:** the first message must be `hello`. Anything else gets `hello_required` and the connection closes.
- **Requests:** `{"proto":1,"id":<int>,"op":"<op>",…}` gets exactly one reply `{"proto":1,"id":<same>,"ok":true|false,…}`. Errors carry `error` (a code) and sometimes `detail`.
- **Pushes:** server-initiated lines have no `id`: `{"event": …}`, `{"snapshot": …}`, `{"cmd": …}`. Clients must accept pushes interleaved with replies.

## Error codes
| Code | When |
|------|------|
| `proto_mismatch` | `proto` is not 1 (connection closes) |
| `hello_required` | the first message wasn't `hello` (connection closes) |
| `line_too_long` | the line exceeds 1 MB (connection closes) |
| `bad_json` | the line isn't a JSON object (connection stays open) |
| `unknown_op` | there is no handler for `op` |
| `not_implemented` | `pause` / `resume` / `warmup` before the P06/P08 handlers are installed |
| `bad_request` | a required field is missing |
| `unknown_profile` | the `profile_id` isn't in the config (the daemon reloads the config once before answering) |
| `unknown_wrapper` | the `wrapper_event` names an unregistered wrapper |
| `bad_topics` | `subscribe` got no valid topics |
| `internal_error` | a handler raised an exception (`detail` has the message) |

## Ops

### `hello`
```json
→ {"proto":1,"id":1,"op":"hello","client":"launcher|app|cli","version":"0.1.0"}
← {"proto":1,"id":1,"ok":true,"daemon":{"version":"0.1.0","pid":4242,"started_at":"2026-09-24T16:00:00Z"}}
```

### `status`
```json
→ {"proto":1,"id":2,"op":"status","profile_id":"work"}          // profile_id optional
← {"proto":1,"id":2,"ok":true,
   "daemon":{"version":"0.1.0","pid":4242,"started_at":"…","uptime_s":120,"responsive":true},
   "profiles":[{"id":"work",
                "usage":{…UsageSnapshot…}|null,
                "supervisor":{"state":"normal","holds":[]},     // P06 extends
                "sessions":[{…session record…}],
                "other_sessions":{"interactive":1,"background":2},
                "next_warmup_at":null}]}                         // P08 fills
```

### `refresh`
Forces a poll (all profiles when `profile_id` is omitted). The reply comes immediately; new data arrives through `usage/<id>.json`, the snapshot, and events.
```json
→ {"proto":1,"id":3,"op":"refresh","profile_id":"work"}
← {"proto":1,"id":3,"ok":true}
```

### `subscribe`
Topics: `events`, `snapshot`. On a `snapshot` subscription the current snapshot is pushed right away, then after every write.
```json
→ {"proto":1,"id":4,"op":"subscribe","topics":["events","snapshot"]}
← {"proto":1,"snapshot":{…widget snapshot…}}                    // push (may precede the reply)
← {"proto":1,"id":4,"ok":true,"topics":["events","snapshot"]}
← {"proto":1,"event":{"schema":1,"ts":"…","type":"limit.warn","profile_id":"work","key":"…",
                      "data":{…,"title":"…","body":"…","notify":true}}}
```

### `reload_config`
```json
→ {"proto":1,"id":5,"op":"reload_config"}
← {"proto":1,"id":5,"ok":true,"changed":true,"revision":4,"issues":[]}
← {"proto":1,"id":5,"ok":false,"changed":false,"revision":5,"issues":[{"path":"profiles[0].limits.session","message":"warn must be below pause"}]}
```

### `register_wrapper` (launcher)
Writes `sessions/<wrapper_id>.json` with `supervision.state = "running"` and emits `session.started` (only the first time). Re-registering the same `wrapper_id` after a reconnect keeps the record. Hooks (P06) may add fields to the reply.
```json
→ {"proto":1,"id":6,"op":"register_wrapper","wrapper_id":"9f…","profile_id":"work",
   "wrapper_pid":123,"claude_pid":124,"cwd":"/Users/me/code","started_overridden":false}
← {"proto":1,"id":6,"ok":true,"holds":[],"supervision":{"state":"running","holds":[],…}}
```

### `unregister_wrapper` (launcher)
Removes the record and emits `session.ended` (`reason: "exited"`).
```json
→ {"proto":1,"id":7,"op":"unregister_wrapper","wrapper_id":"9f…","exit_code":0}
← {"proto":1,"id":7,"ok":true,"known":true}
```

### `wrapper_event` (launcher)
`kind`: `input_submitted_while_paused` | `injected` | `inject_failed`. Logged and forwarded to hooks (P06).
```json
→ {"proto":1,"id":8,"op":"wrapper_event","wrapper_id":"9f…","kind":"injected","detail":{"cmd_id":"…","what":"esc"}}
← {"proto":1,"id":8,"ok":true}
```

### `pause` / `resume` (P06), `warmup` (P08)
Until those plans install their handlers:
```json
→ {"proto":1,"id":9,"op":"pause","profile_id":"work"}
← {"proto":1,"id":9,"ok":false,"error":"not_implemented"}
```
Request fields (ADR-0005): `pause`/`resume` take `{profile_id? | wrapper_id?}`; `warmup` takes `{profile_id? | all: true, trigger, force?}`.

## Commands: daemon → launcher
The daemon pushes a command on the launcher's registered connection. The launcher answers with an ack line (no `id`, no `op`). With no ack within 10 s, the daemon records `result: "timeout"`.
```json
← {"proto":1,"cmd":{"cmd_id":"a1b2c3d4e5f6","type":"pause","holds":["session"],"resume_at":"2026-09-24T20:00:00Z"}}
→ {"proto":1,"ack":"a1b2c3d4e5f6","result":"injected|skipped|failed","detail":{"was_busy":true}}

← {"proto":1,"cmd":{"cmd_id":"…","type":"resume","prompt":"The usage limit window has reset. …"|null}}
→ {"proto":1,"ack":"…","result":"injected|skipped|failed","detail":{}}
```

## Events
The event line format lives in [`event.schema.json`](event.schema.json). The types are listed in ADR-0005. `EventBus.emit` adds `data.title`, `data.body`, and `data.notify` (ADR-0015).
