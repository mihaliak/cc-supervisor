# schema/

JSON contracts between the Python `ccs` side and the Swift app/widgets (ADR-0001, ADR-0005).

- One JSON Schema file per contract, e.g. `config.schema.json`, `usage-snapshot.schema.json`, `live-report.schema.json`, `widget-snapshot.schema.json`.
- `fixtures/` holds shared test vectors that **both** test suites load, e.g. `time_format.json` (ADR-0009) and sample snapshots.

## Adding or changing a contract
1. Edit or add the schema here first. Bump the `schema` integer on breaking changes (ADR-0005).
2. Update the Python models and writers, then the Swift `Codable` models.
3. Add or refresh fixtures, and make both test suites load them.
4. Update the manual page that documents the file, if users see it.
