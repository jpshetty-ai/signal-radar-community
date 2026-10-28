# ADR-0017: Neo4j as the serving store for the Graqle graph

**Status:** Accepted (2026-10-21). Built and verified the same day.

## Context
Today the Graqle graph is one file, `data/graqle/graqle.json` (15,537 nodes, 16,351 edges on 2026-10-21),
rebuilt from scratch by the daily job (`graph_export.push`: `graq scan docs -o` then `graq learn batch -g`).
`graqle.yaml` says `connector: networkx`, so `graq mcp serve` and `graq reason` read that file. On 2026-10-21 a
one-off copy was imported into the Neo4j database `signalradar` (Neo4j Desktop, Enterprise 2026.10.0,
`bolt://localhost:7687`); nothing reads it yet. The user wants Neo4j to become the graph that Graqle queries.

Only Graqle uses the graph. The console, public site, brief and the `signal-sources` MCP server read
`signals.sqlite` and the corpus, and are not affected by this decision.

What graq 0.85 can and cannot do (read from the installed source):
1. `graq scan docs` writes JSON only (`--output` is a file path). There is no Neo4j target.
2. `graq learn batch` with `connector: neo4j` loads the whole graph from Neo4j into memory, adds to it and writes
   it back with MERGE. If the load fails it prints a warning and silently continues on a JSON file.
3. Nothing in graq deletes from Neo4j: `to_neo4j`, `Neo4jConnector.save` and `neo4j-import` only upsert. A node
   that leaves the file stays in Neo4j forever.
4. `graq neo4j-import` reads edges from an `edges` key; our networkx file names it `links` (0 edges imported).
5. With `connector: neo4j`, the graq MCP server and `graq reason` read Neo4j; if the connection fails they fall
   back to the JSON file named in the config.
6. Environment: `NEO4J_PASSWORD` is exported in `~/.zshenv`. The launchd job (`io.signal-radar.daily`) starts the
   CLI directly, without a shell, so it does not see that variable.

## Decision
Build, then load. The file stays the build artifact; Neo4j becomes the serving store.

1. **Build unchanged.** `graph_export.push` keeps building `graqle.json` from scratch (points 1 and 2 make
   writing straight into Neo4j unsafe: no scan target, and a silent fallback to a file).
2. **Load with a new step, `signal-radar graph sync`** (`graph_sync.py`, our own code on the `neo4j` driver and
   graqle's `Neo4jConnector.save`/`save_chunks`, projecting nodes the way graq's `to_neo4j` does):
   - refuses to run unless the target database is configured and is not `neo4j` or `system`;
   - upserts all nodes (with their properties), edges and chunks from the file. Edges are keyed by the file's
     networkx `key` (`source___RELATION___target`): graq's own edge ids are positions in the file, so one removed
     document would renumber every later edge and each sync would rewrite thousands of unchanged edges;
   - deletes what the file no longer has: nodes whose id is not in the file (DETACH DELETE) and relationships
     whose key is not in the file. Only in the configured database;
   - validates: node, chunk and per-type relationship counts and the full id sets must equal the file, else exit 1;
   - writes `data/graqle/neo4j_sync.json` (time, file checksum, counts, result) for the console and the log.
   Bounded: batch size 500, a total timeout, `--dry-run`, and a guard that refuses an empty build or one that
   would delete more than 20% of the nodes (`config/graph.yaml`, `sync:`). Idempotent: a run that stops halfway
   is completed by the next.
3. **Graqle reads Neo4j.** `graqle.yaml` gets `graph.connector: neo4j`, uri, username, a `${...}` password
   reference and database from a new `config/graph.yaml`, through `scripts/sync_graqle_config.py` (so `--check` keeps working).
   The JSON file stays in place as graq's fallback, so a Neo4j outage degrades to yesterday's file, not to nothing.
4. **Secret and user.** Signal Radar connects as Neo4j user `signal_radar` (role `signal_radar_rw`: access, read,
   write, name, index and constraint management on `signalradar` only; home database `signalradar`). Its password
   is `SIGNAL_RADAR_NEO4J_PASSWORD` from the environment, else the keychain (service `signal-radar`). Deliberately
   not `NEO4J_PASSWORD`, which holds the shared instance's admin password. `graqle.yaml` holds only `${...}`.
   The graq MCP server starts through `signal-radar graq-mcp`, which adds the password from the keychain.
5. **The build stays on the file.** graq 0.85's config resolver is on by default and walks up from
   `data/graqle/` to the project's `graqle.yaml`; with `connector: neo4j`, `graq learn batch` would then load
   from and write to Neo4j. The build sets `GRAQLE_USE_RESOLVER=0` (graq's opt-out) so it always writes the file.
6. **Rollback.** Set `connector: networkx` in `config/graph.yaml` and regenerate. Neo4j keeps its data; nothing
   else changes.

## Consequences
- Positive: Cypher and Neo4j Browser on the live graph; graq queries against an indexed store; stale nodes are
  removed, so Neo4j matches the file exactly after every sync; the build stays testable offline.
- Negative: a second copy to keep in step (the sync validates it every run); the daily job gains a dependency on
  a running Neo4j; every sync re-sends all nodes, edges and chunks (about 47,000 rows, 4 s on 2026-10-21).
- The architecture in CLAUDE.md changes: "Graqle graph + SQLite signal store" becomes "graph built as a file,
  served from Neo4j". CLAUDE.md and docs/architecture.md are updated with this ADR.

## Decisions taken (2026-10-21)
- **Where:** option 1B, the `signalradar` database on the shared instance, with the restricted user above.
  Verified: `signal_radar` can read and write `signalradar`; reading or writing `neo4j` is refused (Forbidden).
- **Who syncs:** option 2A, the daily job, after the graph build (`sync.daily: true`; only when
  `connector: neo4j`). A failed build skips the sync, so Neo4j keeps the previous copy.

## Verified (2026-10-21)
- First sync: 15,537 nodes, 16,351 relationships (9 types, counts equal per type), 15,536 chunks; 4 s. Documents
  now carry url, date and title (the one-off import had dropped them). A second sync has nothing to do.
- Daily path in launchd's environment (no shell variables): build, then sync with the password from the keychain;
  the rebuilt file needed zero changes in Neo4j. The graq MCP server reports `neo4j://bolt://localhost:7687`.

## Known issues
- Three graq installs: the daily build uses `/opt/homebrew/bin/graq` 0.68.1 (launchd's PATH), the venv has 0.85.0,
  `~/.local/bin` has 0.85.1. graq 0.85 is unlicensed here (anonymous, 500-node cap, grace ended 2026-10-11), so
  `signal-radar graph` from a shell with the venv on PATH fails and leaves no file until the next good build.
  0.68.1 enforces no cap. Fix: register or license graq 0.85, then use one version everywhere.
- `graph.connector: neo4j` is deprecated in graq 0.85 in favour of `backends:`, which 0.85 still ignores at
  runtime. Revisit when graq makes `backends:` the runtime switch.

## Options considered
1. **Where Neo4j runs.**
   - (A) A dedicated DBMS for Signal Radar in Neo4j Desktop, on its own ports. Full isolation from the shared
     instance; costs memory (about 1 to 2 GB) and one manual setup in the Desktop app.
   - (B) Keep the `signalradar` database on the shared DBMS at `localhost:7687`, with a dedicated Neo4j user
     granted access to `signalradar` only (Enterprise role-based access), so a bug cannot reach other databases.
     No extra process; the daily sync loads the shared server for about a minute.
2. **Who runs the sync.**
   - (A) The daily job, after the graph step, unattended. A failed sync is reported and sets exit code 1 but
     does not undo the data run (as with publish).
   - (B) Manual only: `signal-radar graph sync` when you want Neo4j current. Neo4j is a snapshot between runs.
   The global rule forbids writes to a shared stateful service from git hooks; a scheduled job is not a hook,
   but it is the same kind of unattended write. 1A + 2A has the least coupling to the shared instance.
