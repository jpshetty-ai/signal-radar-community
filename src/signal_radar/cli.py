"""Command line entry point: config checks, LLM smoke tests, mail sign-in and source fetches."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from .config import CONFIG_DIR, DATA_DIR, PROJECT_ROOT, ROLES, load_models, load_sources
from .llm import LLMError
from .sources import FeedError, MailError, SourceNotSupported


def _cmd_config(args: argparse.Namespace) -> int:
    models = load_models(overrides=args.model)
    sources = load_sources()
    out = {
        "project_root": str(PROJECT_ROOT),
        "roles": {r: s.key for r, s in models.roles.items()},
        "resolved": {r: [f"{label} ({spec.provider}/{spec.model})" for label, spec in models.resolve(r)]
                     for r in models.roles},
        "unpriced": sorted({f"{spec.provider}/{spec.model}" for r in models.roles for _, spec in models.resolve(r)
                            if models.price_for(spec.provider, spec.model) is None}),
        "budget_usd_month": models.budget.monthly_usd,
        "sources": [{"id": s.id, "type": s.type, "enabled": s.enabled} for s in sources.sources],
    }
    print(json.dumps(out, indent=2))
    return 0


def _cmd_llm(args: argparse.Namespace) -> int:
    from .llm import LLMRouter

    router = LLMRouter.from_config(overrides=args.model, job="cli")
    if args.role == "embedding":
        res = router.embed([args.prompt])
        print(json.dumps({"model": f"{res.provider}/{res.model}", "dims": len(res.vectors[0]),
                          "input_tokens": res.input_tokens, "cost_usd": res.cost_usd, "latency_ms": res.latency_ms}))
        return 0
    res = router.complete(args.role, args.prompt)
    print(res.text)
    print(json.dumps({"model": f"{res.provider}/{res.model}", "input_tokens": res.input_tokens,
                      "output_tokens": res.output_tokens, "cost_usd": res.cost_usd,
                      "latency_ms": res.latency_ms, "run_id": router.run_id}), file=sys.stderr)
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    from .calllog import CallLog

    print(json.dumps(CallLog(DATA_DIR / "llm_calls.sqlite").compare(args.input_hash), indent=2))
    return 0


def _graph_auth(account_name: str):
    from .config import GraphAccount
    from .sources import GraphAuth

    cfg = load_sources()
    account = cfg.mail_accounts.get(account_name)
    if not isinstance(account, GraphAccount):
        raise SystemExit(f"no graph mail account {account_name!r} in sources.yaml (have: {sorted(cfg.mail_accounts)})")
    return GraphAuth(account_name, account), cfg


def _cmd_auth(args: argparse.Namespace) -> int:
    from .config import ImapAccount
    from .sources import ImapMailClient

    account = load_sources().mail_accounts.get(args.account)
    if isinstance(account, ImapAccount):
        if args.logout:
            raise SystemExit("imap accounts use an app password: delete it with `security delete-generic-password`")
        client = ImapMailClient.from_account(account)
        client.check_login()
        print(f"IMAP login OK: {client.user} on {client.host} (password from the keychain)")
        return 0
    auth, _ = _graph_auth(args.account)
    if args.logout:
        auth.logout()
        print(f"removed cached tokens for {auth.user}")
        return 0
    print(f"signed in as {auth.login(echo=lambda m: print(m, flush=True))}; token cached in the macOS keychain")
    return 0


def _since(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _cmd_fetch(args: argparse.Namespace) -> int:
    from .sources import build_adapter

    cfg = load_sources()
    report = build_adapter(cfg.get(args.source), cfg).fetch_report(_since(args.days))
    if args.json:
        print(json.dumps([i.model_dump(mode="json") for i in report.items], indent=2, ensure_ascii=False))
    else:
        for i in report.items:
            print(f"{i.date} [{i.section or '-'}] {i.title}\n    {i.url}")
    print(json.dumps({"source": report.source_id, "scanned": report.scanned, "matched": report.matched,
                      "items": len(report.items), "skipped": report.skipped, "deferred": report.deferred,
                      "warnings": report.warnings}), file=sys.stderr)
    return 0


def _cmd_mail_senders(args: argparse.Namespace) -> int:
    from .sources import default_mail_factory, sender_groups

    client = default_mail_factory(args.account, load_sources())
    if args.save_html:
        out = Path(args.save_html)
        out.mkdir(parents=True, exist_ok=True)
        msgs = list(client.list_messages(args.folder, _since(args.days), with_body=True))
        for m in msgs:
            name = f"{m.received_at:%Y-%m-%d_%H%M}_{m.from_name.replace(' ', '_') or 'unknown'}.html"
            (out / name).write_text(m.html or "")
        print(f"saved {len(msgs)} message bodies to {out}", file=sys.stderr)
    else:
        msgs = list(client.list_messages(args.folder, _since(args.days), with_body=False))
    print(json.dumps(sender_groups(msgs), indent=2, ensure_ascii=False))
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from .ingest import run_ingest, run_summary

    since = _since(args.days) if args.days else None
    runs = run_ingest(args.source or None, since)
    print(json.dumps(run_summary(runs), indent=2, ensure_ascii=False))
    return 1 if any(r.error for r in runs) else 0


def _cmd_backfill(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from .ingest import backfill_archive

    for sid in args.source:
        run = backfill_archive(sid, args.days, delay_s=args.delay, log=lambda m: print(m, file=sys.stderr, flush=True))
        print(json.dumps(asdict(run), ensure_ascii=False), flush=True)
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from .extract import extract_items

    stats = extract_items(args.ids or None, redo=args.redo, limit=args.limit, workers=args.workers,
                          model_overrides=args.model, log=lambda m: print(m, file=sys.stderr, flush=True))
    print(json.dumps(asdict(stats)))
    return 0


def _jsonable(obj):
    from dataclasses import asdict, is_dataclass

    if isinstance(obj, list):
        return [_jsonable(o) for o in obj]
    return asdict(obj) if is_dataclass(obj) else obj


def _cmd_signals(args: argparse.Namespace) -> int:
    from . import signals as sg

    if args.action == "rebuild":
        out = sg.rebuild()
    elif args.action == "movers":
        out = sg.top_movers(window_days=args.days, entity_type=args.type, limit=args.limit,
                            direction="down" if args.down else "up", min_relevance=args.min_relevance)
    elif args.action == "trend":
        if not args.name:
            raise SystemExit("signals trend needs an entity name")
        out = sg.trend_metrics(args.name, window_days=args.days)
    elif args.action == "clusters":
        out = sg.cooccurrence_growth(window_days=args.days, limit=args.limit)
    elif args.action == "reading":
        out = sg.reading_list(days=args.days, limit=args.limit, category=args.category)
    else:
        out = sg.category_counts(days=args.days)
    print(json.dumps(_jsonable(out), indent=2, ensure_ascii=False, default=str))
    return 0


def _cmd_daily(args: argparse.Namespace) -> int:
    """ingest -> extract new items -> rebuild signal store -> Graqle graph -> console site. What a scheduler runs."""
    from dataclasses import asdict

    from . import schedule
    from . import signals as sg
    from .extract import extract_items
    from .ingest import run_ingest, run_summary

    if args.catch_up:
        age = schedule.hours_since_success("daily")
        if age is not None and age < schedule.CATCH_UP_HOURS:
            print(json.dumps({"skipped": f"last successful run {age:.1f} h ago"}))
            return 0
    runs = run_ingest()
    stats = extract_items(log=lambda m: print(m, file=sys.stderr, flush=True), model_overrides=args.model)
    rebuilt = sg.rebuild()
    try:
        from .graph_export import push

        _graph_build_env()
        graph = push()["learn_batch"]
    except Exception as e:  # noqa: BLE001 - the graph is optional; the signal store is what the jobs read
        graph = {"error": str(e)}
    graph_sync = _daily_graph_sync(built="error" not in graph)
    vectors = _daily_graph_embed(synced="database" in graph_sync)
    brief = _daily_brief()
    try:
        from .site.build import build_site

        site = build_site()
    except Exception as e:  # noqa: BLE001 - a failed page build must not fail the data run
        site = {"error": str(e)}
    published = _daily_publish()
    print(json.dumps({"ingest": run_summary(runs), "extract": asdict(stats), "signals": asdict(rebuilt),
                      "graph": graph, "graph_sync": graph_sync, "vectors": vectors, "brief": brief, "site": site,
                      "publish": published}, indent=2))
    failed = any(r.error for r in runs) or "error" in published or "error" in graph_sync
    if not failed:
        schedule.mark_success("daily")
    return 1 if failed else 0


def _daily_brief() -> dict:
    """Weekly step of `daily`: the first run of each ISO week writes its brief (both editions), window ending Monday."""
    from .brief import build_brief, weekly_due

    end = weekly_due()
    if end is None:
        return {"skipped": "this week's brief exists"}
    try:
        return {"written": [build_brief(end=end, edition=e) for e in ("console", "public")]}
    except Exception as e:  # noqa: BLE001 - the data run already succeeded; the next run retries the brief
        return {"error": str(e)}


def _cmd_graq_mcp(args: argparse.Namespace) -> int:
    """Start `graq mcp serve --read-only` for this project (what .mcp.json runs). Adds the Signal Radar Neo4j
    password from the keychain to graq's environment; without it graq would quietly fall back to the file.
    stdout belongs to the MCP protocol, so this prints nothing there."""
    import shutil

    from .config import PROJECT_ROOT, load_graph
    from .graph_sync import SyncError, neo4j_password

    cfg = load_graph()
    env = dict(os.environ)
    if cfg.connector == "neo4j":
        try:
            env[cfg.neo4j.password_env] = neo4j_password(cfg)
        except SyncError as e:
            print(f"graq-mcp: {e}", file=sys.stderr)
            return 1
    # The venv's graq (0.85, tested with Neo4j), not whatever is first on PATH: /opt/homebrew/bin/graq is 0.68.
    venv_graq = Path(sys.executable).with_name("graq")
    graq = str(venv_graq) if venv_graq.exists() else shutil.which("graq") or "graq"
    os.chdir(PROJECT_ROOT)  # graqle.yaml's file path (the fallback) is relative to the project root
    os.execvpe(graq, [graq, "mcp", "serve", "--read-only", "--config", str(PROJECT_ROOT / "graqle.yaml")], env)
    return 0  # not reached


def _graph_build_env() -> None:
    """The graph build always writes the file (ADR-0017). graq's config resolver, on by default since 0.85, walks
    up from data/graqle/ to the project's graqle.yaml; with connector: neo4j, `graq learn batch` would then load
    from and write to Neo4j instead of rebuilding the file. graq's documented opt-out keeps it on the file."""
    os.environ["GRAQLE_USE_RESOLVER"] = "0"


def _daily_graph_sync(built: bool) -> dict:
    """Step of `daily` after the graph build: copy the file into Neo4j when graph.yaml serves it from there."""
    from .config import load_graph
    from .graph_sync import sync

    cfg = load_graph()
    if cfg.connector != "neo4j" or not cfg.sync.daily:
        return {"skipped": f"connector {cfg.connector}, sync.daily {cfg.sync.daily} in config/graph.yaml"}
    if not built:
        return {"skipped": "the graph build failed; Neo4j keeps the previous copy"}
    try:
        res = sync(cfg, log=lambda m: print(m, file=sys.stderr, flush=True))
        return {"database": res.database, "add": res.add, "delete": res.delete, "seconds": res.seconds}
    except Exception as e:  # noqa: BLE001 - the data run already succeeded; report the failed sync
        return {"error": str(e)}


def _daily_graph_embed(synced: bool) -> dict:
    """Step of `daily` after the sync: embed new chunks and write their vectors (ADR-0019). Never fails the run:
    Ask and semantic_search fall back to keyword or local search."""
    from dataclasses import asdict

    from . import vectors
    from .config import load_graph
    from .graph_sync import neo4j_target

    cfg = load_graph()
    if not cfg.embed.daily:
        return {"skipped": "embed.daily is false in config/graph.yaml"}
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    try:
        st = vectors.embed_targets(vectors.targets_from_graph(), max_chunks=cfg.embed.max_chunks,
                                   max_minutes=cfg.embed.max_minutes, log=log)
        out = {"embedded": st.embedded, "remaining": st.remaining, "seconds": st.seconds, "stopped": st.stopped}
        if synced and st.dim:
            t = neo4j_target(cfg)
            try:
                p = vectors.push_to_neo4j(t, log=log)
            finally:
                t.close()
            out.update(written=p.written, unchanged=p.unchanged, missing_chunk=p.missing_chunk)
        return out
    except Exception as e:  # noqa: BLE001
        return {"warning": f"{type(e).__name__}: {e}"}


def _daily_publish() -> dict:
    """Optional last step of `daily`: deploy the public edition when config/publish.yaml says daily: true."""
    from .publish import deploy, load_publish

    try:
        if not load_publish().daily:
            return {"skipped": "daily: false in config/publish.yaml"}
        res = deploy(log=lambda m: print(m, file=sys.stderr, flush=True))
        return {k: res[k] for k in ("url", "deployment", "files", "data_to", "probes")}
    except FileNotFoundError:
        return {"skipped": "no config/publish.yaml"}
    except Exception as e:  # noqa: BLE001 - the data run already succeeded; report the failed publish
        return {"error": str(e)}


def _cmd_deploy(args: argparse.Namespace) -> int:
    from .publish import Cloudflare, PublishError, credentials, deploy, load_publish, read_viewers, setup

    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    cfg = load_publish()
    try:
        if args.setup:
            account, token = credentials(cfg)
            viewers = read_viewers(PROJECT_ROOT / cfg.cloudflare.viewers_file)
            print(json.dumps(setup(cfg, Cloudflare(account, token), viewers, log=log), indent=2))
            return 0
        print(json.dumps(deploy(cfg, dry_run=args.dry_run, build=not args.no_build,
                                narrate=not args.no_narrate, log=log), indent=2))
    except PublishError as e:
        print(f"deploy: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    from . import schedule as sch

    if args.action == "status":
        print(json.dumps(sch.status(), indent=2))
        return 0
    if args.action == "install":
        at = None
        if args.at:
            hh, mm = (int(x) for x in args.at.split(":"))
            at = {"Hour": hh, "Minute": mm}
        print(json.dumps(sch.install(args.job, at), indent=2))
    elif args.action == "uninstall":
        print(json.dumps(sch.uninstall(args.job), indent=2))
    elif args.action == "run":
        print(json.dumps(sch.run_now(args.job), indent=2))
    return 0


def _cmd_graph(args: argparse.Namespace) -> int:
    if args.action == "embed":
        return _graph_embed(args.max_chunks, args.max_minutes, push=not args.no_push)
    if args.action == "sync":
        from dataclasses import asdict

        from .graph_sync import SyncError, sync

        try:
            res = sync(dry_run=args.dry_run, log=lambda m: print(m, file=sys.stderr, flush=True))
        except Exception as e:  # noqa: BLE001 - SyncError, or the driver's auth/connection errors
            kind = "" if isinstance(e, SyncError) else f"{type(e).__name__}: "
            print(f"graph sync failed: {kind}{e}", file=sys.stderr)
            return 1
        print(json.dumps(asdict(res), indent=2))
        return 0
    from .graph_export import push

    _graph_build_env()
    print(json.dumps(push(min_mentions=args.min_mentions, scan_docs=not args.skip_docs), indent=2))
    return 0


def _graph_embed(max_chunks: int | None, max_minutes: float | None, push: bool = True) -> int:
    """Embed graph chunks for vector search within a budget, then write the vectors to Neo4j (ADR-0019)."""
    from dataclasses import asdict

    from . import vectors

    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    targets = vectors.targets_from_graph()
    st = vectors.embed_targets(targets, max_chunks=max_chunks, max_minutes=max_minutes, log=log)
    out = {"embed": asdict(st)}
    if push and st.dim:
        from .config import load_graph
        from .graph_sync import neo4j_target

        try:
            t = neo4j_target(load_graph())
            try:
                out["neo4j"] = asdict(vectors.push_to_neo4j(t, log=log))
            finally:
                t.close()
        except Exception as e:  # noqa: BLE001 - vectors stay cached; the next run pushes them
            out["neo4j"] = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out, indent=2))
    return 0 if "error" not in out.get("neo4j", {}) else 1


def _cmd_search(args: argparse.Namespace) -> int:
    from . import vectors

    kind = None if args.kind == "all" else args.kind
    if args.local:
        hits = vectors.search_local(args.query, k=args.k, kind=kind)
    else:
        from .config import load_graph
        from .graph_sync import neo4j_target

        t = neo4j_target(load_graph())
        try:
            hits = vectors.search(args.query, k=args.k, kind=kind, target=t)
        finally:
            t.close()
    print(json.dumps([vectors.hit_dict(h) for h in hits], indent=2, ensure_ascii=False))
    return 0


def _cmd_site(args: argparse.Namespace) -> int:
    from .site.build import build_single_file, build_site

    out = Path(args.out) if args.out else None
    if args.single_file:
        if args.edition != "public":
            raise SystemExit("--single-file is for sharing and always builds the public edition (add --edition public)")
        print(json.dumps(build_single_file(out, narrate=not args.no_narrate), indent=2))
        return 0
    print(json.dumps(build_site(out, edition=args.edition, narrate=not args.no_narrate,
                                log=lambda m: print(m, file=sys.stderr, flush=True)), indent=2))
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    import uvicorn

    from .site.build import build_site
    from .site.web import create_app

    if not args.no_build:
        print(json.dumps(build_site(log=lambda m: print(m, file=sys.stderr))), file=sys.stderr)
    print(f"Signal Radar console: http://127.0.0.1:{args.port}/  (Ctrl+C to stop)", file=sys.stderr, flush=True)
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def _cmd_models(args: argparse.Namespace) -> int:
    from . import evals

    if args.action == "list":
        print(json.dumps(evals.scores_table(), indent=2))
        return 0
    if args.action == "report":
        runs = sorted(evals.EVAL_DIR.glob("*.md"))
        if not runs:
            raise SystemExit("no evaluation yet: run `signal-radar models eval`")
        print((evals.EVAL_DIR / f"{args.run}.md").read_text() if args.run else runs[-1].read_text())
        return 0
    if args.action == "judge":
        if not args.judge:
            raise SystemExit("models judge needs --judge (e.g. jev or a catalog id)")
        print(json.dumps(evals.judge_run(args.run, args.judge, log=lambda m: print(m, file=sys.stderr)), indent=2))
        return 0
    if args.action == "blind":
        print(json.dumps(evals.blind_sheet(args.run), indent=2))
        return 0
    if args.action == "grades":
        print(json.dumps(evals.import_grades(args.run, grader=args.grader), indent=2))
        return 0
    cands = args.candidates.split(",") if args.candidates else list(load_models().catalog)
    roles = tuple(args.roles.split(","))

    def confirm(est: dict) -> bool:
        print(f"{est['tasks']} tasks, estimated cost ${est['total']:.2f}:", file=sys.stderr)
        for c, usd in est["estimate"].items():
            print(f"  {c:20} ${usd:.4f}", file=sys.stderr)
        if args.yes:
            return True
        return input("Run it? [y/N] ").strip().lower() == "y"

    rec = evals.run_eval(cands, roles=roles, n_themes=args.themes, max_usd=args.max_usd, confirm=confirm, judge=args.judge,
                         log=lambda m: print(m, file=sys.stderr, flush=True))
    print(json.dumps({k: rec[k] for k in rec if k != "tasks"}, indent=2))
    if not rec.get("cancelled"):
        print(f"report: data/model_eval/{rec['run_id']}.md", file=sys.stderr)
    return 0


def _cmd_brief(args: argparse.Namespace) -> int:
    from datetime import date as _date

    from .brief import build_brief

    end = _date.fromisoformat(args.end) if args.end else None
    editions = ("console", "public") if args.edition == "both" else (args.edition,)
    out = [build_brief(end=end, days=args.days, prose=not args.no_prose, edition=e) for e in editions]
    print(json.dumps(out, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="signal-radar")
    p.add_argument(
        "--model", action="append", metavar="ROLE=PROVIDER/MODEL",
        help=f"override a role for this run; roles: {', '.join(ROLES)}. Repeatable.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("config", help="validate config/*.yaml and show resolved roles").set_defaults(func=_cmd_config)

    llm = sub.add_parser("llm", help="send one prompt to a role (smoke test)")
    llm.add_argument("role", choices=ROLES)
    llm.add_argument("prompt")
    llm.set_defaults(func=_cmd_llm)

    cmp_ = sub.add_parser("compare", help="compare models that saw identical inputs (from the call log)")
    cmp_.add_argument("--input-hash")
    cmp_.set_defaults(func=_cmd_compare)

    auth = sub.add_parser("auth", help="sign in to a mail account (M365 device code) or test an IMAP login")
    auth.add_argument("account", help="mail account name from sources.yaml, e.g. work")
    auth.add_argument("--logout", action="store_true", help="remove cached tokens instead")
    auth.set_defaults(func=_cmd_auth)

    fetch = sub.add_parser("fetch", help="fetch one source and print its items (writes nothing)")
    fetch.add_argument("source")
    fetch.add_argument("--days", type=float, default=2, help="look back this many days (default 2)")
    fetch.add_argument("--json", action="store_true")
    fetch.set_defaults(func=_cmd_fetch)

    ing = sub.add_parser("ingest", help="fetch enabled sources since their cursor and write new items to corpus/")
    ing.add_argument("--source", action="append", help="limit to this source id (repeatable)")
    ing.add_argument("--days", type=float, help="ignore the cursor and look back this many days")
    ing.set_defaults(func=_cmd_ingest)

    bf = sub.add_parser("backfill", help="fill corpus/ from a source's public web archive (one page per issue day)")
    bf.add_argument("source", nargs="+")
    bf.add_argument("--days", type=int, default=90)
    bf.add_argument("--delay", type=float, default=2.0, help="seconds between requests (be polite)")
    bf.set_defaults(func=_cmd_backfill)

    ex = sub.add_parser("extract", help="extract entities/category/relevance for corpus items (resumable)")
    ex.add_argument("--ids", nargs="*", help="only these item ids")
    ex.add_argument("--redo", action="store_true", help="re-extract even if current")
    ex.add_argument("--limit", type=int)
    ex.add_argument("--workers", type=int, default=1)
    ex.set_defaults(func=_cmd_extract)

    sg = sub.add_parser("signals", help="signal store: rebuild, movers, trend, clusters, reading, categories")
    sg.add_argument("action", choices=["rebuild", "movers", "trend", "clusters", "reading", "categories"])
    sg.add_argument("name", nargs="?", help="entity name (for trend)")
    sg.add_argument("--days", type=int, default=14, help="window size in days (14: weekly counts are thin)")
    sg.add_argument("--type", help="entity type filter (movers)")
    sg.add_argument("--category", help="category filter (reading)")
    sg.add_argument("--limit", type=int, default=15)
    sg.add_argument("--down", action="store_true", help="fading instead of rising (movers)")
    sg.add_argument("--min-relevance", type=int, default=0, help="movers: count only items this relevant to my lens (0-3)")
    sg.set_defaults(func=_cmd_signals)

    gr = sub.add_parser("graph", help="build data/graqle/graqle.json (push), copy it into Neo4j (sync, ADR-0017), "
                                      "or embed its chunks for vector search (embed, ADR-0019)")
    gr.add_argument("action", nargs="?", choices=["push", "sync", "embed"], default="push")
    gr.add_argument("--max-chunks", type=int, help="embed: stop after embedding this many texts (budget)")
    gr.add_argument("--max-minutes", type=float, help="embed: stop after this many minutes (budget)")
    gr.add_argument("--no-push", action="store_true", help="embed: fill the local cache only, do not write to Neo4j")
    gr.add_argument("--dry-run", action="store_true", help="sync: show what would be added and deleted, write nothing")
    gr.add_argument("--min-mentions", type=int, default=2, help="only entities mentioned at least this often")
    gr.add_argument("--skip-docs", action="store_true", help="add entities to the existing graph only (no rebuild)")
    gr.set_defaults(func=_cmd_graph)

    se = sub.add_parser("search", help="vector search over news items and entities (Neo4j index, ADR-0019)")
    se.add_argument("query")
    se.add_argument("-k", type=int, default=10)
    se.add_argument("--kind", choices=["all", "item", "entity"], default="all")
    se.add_argument("--local", action="store_true", help="exact search over the local cache, without Neo4j")
    se.set_defaults(func=_cmd_search)

    site = sub.add_parser("site", help="build the static site (console or public edition)")
    site.add_argument("action", choices=["build"])
    site.add_argument("--edition", choices=["console", "public"], default="console")
    site.add_argument("--out", help="output folder (default data/site or data/site_public)")
    site.add_argument("--no-narrate", action="store_true", help="skip model-written theme text")
    site.add_argument("--single-file", action="store_true",
                      help="one self-contained HTML file for sharing (public edition; default data/share/)")
    site.set_defaults(func=_cmd_site)

    web = sub.add_parser("web", help="build and serve the private console on http://127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-build", action="store_true", help="serve the existing build")
    web.set_defaults(func=_cmd_web)

    md = sub.add_parser("models", help="model catalog, evaluation on our own tasks, last report (ADR-0013)")
    md.add_argument("action", choices=["list", "eval", "report", "judge", "blind", "grades"])
    md.add_argument("--run", help="run id for report/judge/blind/grades (default: latest)")
    md.add_argument("--grader", default="human", help="name for imported blind grades (grades)")
    md.add_argument("--candidates", help="comma-separated catalog ids (default: the whole catalog)")
    md.add_argument("--roles", default="writing,reasoning")
    md.add_argument("--themes", type=int, default=6, help="theme-writing tasks per candidate")
    md.add_argument("--max-usd", type=float, default=2.0, help="refuse to start if the estimate is higher")
    md.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    md.add_argument("--judge", help="judge: a catalog id (e.g. sonnet-5.5) or a configured judge (e.g. jev)")
    md.set_defaults(func=_cmd_models)

    br = sub.add_parser("brief", help="weekly signal brief: briefs/<week>.md + the console Brief page")
    br.add_argument("--end", help="last day of the window (YYYY-MM-DD); default the latest data")
    br.add_argument("--days", type=int, default=7)
    br.add_argument("--no-prose", action="store_true", help="computed sections only, no model call")
    br.add_argument("--edition", choices=("both", "console", "public"), default="both",
                    help="public = from the public store without excluded sources, own prose call (ADR-0015)")
    br.set_defaults(func=_cmd_brief)

    gm = sub.add_parser("graq-mcp", help="start the read-only graq MCP server with the Neo4j password from the keychain")
    gm.set_defaults(func=_cmd_graq_mcp)

    daily = sub.add_parser("daily", help="ingest + extract new items + rebuild signals (what the scheduler runs)")
    daily.add_argument("--catch-up", action="store_true",
                       help="skip if the last successful run is under 20 h old (what launchd runs at 07:30 and at login)")
    daily.set_defaults(func=_cmd_daily)

    dep = sub.add_parser("deploy", help="publish the public edition to Cloudflare Pages behind Access (ADR-0014)")
    dep.add_argument("--dry-run", action="store_true", help="build and check, print what would be published")
    dep.add_argument("--setup", action="store_true",
                     help="create/update the Pages project, one-time PIN login and viewer Access policy")
    dep.add_argument("--no-build", action="store_true", help="publish the existing data/site_public build")
    dep.add_argument("--no-narrate", action="store_true", help="skip model-written theme text")
    dep.set_defaults(func=_cmd_deploy)

    from .schedule import JOBS

    sch = sub.add_parser("schedule", help="run jobs on this Mac with launchd (install, uninstall, status, run)")
    sch.add_argument("action", choices=["install", "uninstall", "status", "run"])
    sch.add_argument("job", nargs="?", choices=list(JOBS), default="daily")
    sch.add_argument("--at", help="time of day HH:MM (default 07:30 for daily)")
    sch.set_defaults(func=_cmd_schedule)

    snd = sub.add_parser("mail-senders", help="group a mail folder by sender, with proposed match rules")
    snd.add_argument("account")
    snd.add_argument("folder")
    snd.add_argument("--days", type=float, default=7)
    snd.add_argument("--save-html", metavar="DIR", help="also save message bodies (parser test samples, keep out of Git)")
    snd.set_defaults(func=_cmd_mail_senders)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print(f"missing file: {e.filename} (config dir: {CONFIG_DIR})", file=sys.stderr)
        return 2
    except (FeedError, LLMError, MailError, SourceNotSupported) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyError as e:
        print(f"error: unknown id {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
