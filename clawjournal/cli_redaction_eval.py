"""CLI entry points for the optional local evaluation workspace."""
import json
from pathlib import Path
import sys


def register_parser(sub):
    from .config import CONFIG_DIR
    parser = sub.add_parser("redaction-eval", help="Opt-in local capture, corrections and PII evaluation")
    parser.add_argument("--store", type=Path, default=CONFIG_DIR / "redaction-eval")
    commands = parser.add_subparsers(dest="eval_command", required=True)
    init = commands.add_parser("init", help="Opt in to retaining originals locally")
    init.add_argument("--retention-days", type=int, default=30)
    init.add_argument("--max-mib", type=int, default=100)
    capture = commands.add_parser("capture", help="Capture input and local redacted preview")
    source = capture.add_mutually_exclusive_group(required=True)
    source.add_argument("--text-file", type=Path)
    source.add_argument("--session-json", type=Path, help="One normalized ClawJournal session object")
    capture.add_argument("--group", required=True, help="Pseudonymous contributor/group; stays in one split")
    capture.add_argument("--split", choices=("train", "dev", "test"), default="dev")
    capture.add_argument("--source-kind", choices=("owned", "contributor-approved", "licensed", "synthetic"), required=True)
    capture.add_argument("--provenance", required=True, help="Ownership/permission/source note; avoid identifiers")
    commands.add_parser("list")
    commands.add_parser("status")
    commands.add_parser("disable", help="Stop new captures; retain review/delete access")
    commands.add_parser("prune", help="Delete expired captures and their managed review pages")
    for name in ("review", "delete"):
        commands.add_parser(name).add_argument("capture_id")
    commands.add_parser("import-corrections").add_argument("file", type=Path)
    annotate = commands.add_parser("annotate", help="Record a selected-span correction")
    annotate.add_argument("capture_id")
    annotate.add_argument("--unit", required=True, help="JSON pointer, e.g. /messages/0/content")
    annotate.add_argument("--start", type=int, required=True)
    annotate.add_argument("--end", type=int, required=True)
    annotate.add_argument("--decision", choices=("redact", "preserve", "uncertain"), required=True)
    annotate.add_argument("--entity-type", default="")
    annotate.add_argument("--note", default="")
    annotate.add_argument("--human-reviewed", action="store_true", help="Attest that a human checked this label")
    run = commands.add_parser("run", help="Repeat a local benchmark against saved annotations")
    run.add_argument("--engine", choices=("rules", "gliner"), default="rules")
    run.add_argument("--model-dir", type=Path)
    run.add_argument("--threshold", type=float, default=0.3)
    run.add_argument("--split", choices=("train", "dev", "test"), default="dev")
    run.add_argument("--include-drafts", action="store_true")

    contribute = commands.add_parser("contribute", help="Review and opt in to contributing an invented-identifier snippet")
    actions = contribute.add_subparsers(dest="contribution_command", required=True)
    prepare = actions.add_parser("prepare", help="Prepare up to 800 characters for explicit contribution review")
    prepare.add_argument("capture_id")
    prepare.add_argument("--unit", required=True)
    prepare.add_argument("--start", type=int, required=True)
    prepare.add_argument("--end", type=int, required=True)
    prepare.add_argument("--contributor-id", help="Optional assigned UUID on first use; reuse on other devices")
    for name in ("review", "export", "withdraw"):
        actions.add_parser(name).add_argument("proposal_id")
    actions.add_parser("approve", help="Import the exact approval downloaded after human review").add_argument("file", type=Path)
    collect = actions.add_parser("collect", help="Maintainer: import an approved bundle with contributor split isolation")
    collect.add_argument("file", type=Path)
    collect.add_argument("--collection", type=Path, required=True)
    collect.add_argument("--contributor-id", required=True, help="Stable maintainer-assigned UUID for this person, across all their devices")
    actions.add_parser("settings", help="Create local contribution preference page (does not change consent)")
    permission = actions.add_parser("consent", help="Manage ongoing permission separately from label review")
    permission_actions = permission.add_subparsers(dest="consent_command", required=True)
    permission_actions.add_parser("status")
    permission_actions.add_parser("disable")
    permission_actions.add_parser("import").add_argument("file", type=Path)
    process = actions.add_parser("process", help="Prepare local unreviewed bundles from eligible future captures")
    process.add_argument("--limit", type=int, default=5, help="Maximum candidates per run (1–5)")
    pairs = commands.add_parser("pairs", help="Separate opt-in original/redacted conversations, only for redaction improvement")
    pair_actions = pairs.add_subparsers(dest="pair_command", required=True)
    pair_settings = pair_actions.add_parser("settings")
    pair_settings.add_argument("--destination", default="https://data.rayward.ai")
    pair_actions.add_parser("consent").add_argument("file", type=Path)
    pair_actions.add_parser("status")
    pair_actions.add_parser("disable")
    pair_actions.add_parser("export").add_argument("capture_id")
    pair_upload = pair_actions.add_parser("upload")
    pair_upload.add_argument("capture_id")
    pair_upload.add_argument("--upload-token-file", type=Path, required=True)
    pair_withdraw = pair_actions.add_parser("withdraw")
    pair_withdraw.add_argument("bundle_id")
    pair_withdraw.add_argument("--upload-token-file", type=Path, required=True)


def _read_contribution(path):
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Contribution/approval file exceeds 1 MiB")
    return json.loads(raw)


def _contribute(args, store):
    from .redaction import contributions as c
    command = args.contribution_command
    if command in ("settings", "consent", "process"):
        from .redaction import ongoing_contributions as ongoing
        if command == "settings":
            return {"settings_file": str(ongoing.settings_page(store)), "permission_changed": False}
        if command == "process":
            return ongoing.process(store, args.limit)
        if args.consent_command == "status":
            return ongoing.status(store)
        if args.consent_command == "disable":
            return ongoing.disable(store)
        return ongoing.import_permission(store, _read_contribution(args.file))
    if command == "collect":
        return c.collect(args.collection, _read_contribution(args.file), args.contributor_id)
    if command == "prepare":
        proposal_id, path = c.prepare(store, args.capture_id, args.unit, args.start, args.end, assigned_id=args.contributor_id)
        return {"proposal_id": proposal_id, "review_file": str(path), "approved": False}
    if command == "review":
        return {"review_file": str(c.review(store, args.proposal_id))}
    if command == "approve":
        return {"approved_proposal": c.approve(store, _read_contribution(args.file)), "uploaded": False}
    if command == "export":
        return {"local_bundle": str(c.export_bundle(store, args.proposal_id)), "uploaded": False}
    if command == "withdraw":
        c.withdraw(store, args.proposal_id)
        return {"withdrawn": args.proposal_id, "previously_delivered_copies_recalled": False}
    raise ValueError("Unknown contribution command")


def run(args):
    from .redaction.evaluation import (MAX_INPUT, POLICY, Store, digest, evaluate,
                                      gliner_predictor, private_write, rules_predictor)
    store = None
    try:
        if args.eval_command == "contribute" and args.contribution_command == "collect":
            print(json.dumps(_contribute(args, None), indent=2))
            return
        store = Store(args.store, initialize=args.eval_command == "init",
                      retention_days=getattr(args, "retention_days", 30),
                      max_mib=getattr(args, "max_mib", 100))
        command = args.eval_command
        if command == "pairs":
            from .redaction import conversation_pairs as pairs
            if args.pair_command == "settings":
                result = {"settings_file": str(pairs.settings_page(store, args.destination)), "permission_changed": False}
            elif args.pair_command == "consent":
                result = pairs.enable(store, _read_contribution(args.file))
            elif args.pair_command == "status":
                result = pairs.status(store)
            elif args.pair_command == "disable":
                result = pairs.disable(store)
            elif args.pair_command == "export":
                result = {"local_pair": str(pairs.export_pair(store, args.capture_id)), "uploaded": False}
            elif args.pair_command == "withdraw":
                result = pairs.withdraw_hosted(store, args.bundle_id, args.upload_token_file)
            else:
                result = pairs.upload(store, args.capture_id, args.upload_token_file)
        elif command == "contribute":
            result = _contribute(args, store)
        elif command in ("init", "status"):
            result = {"store": str(store.root), **store.settings(), "captures": len(store.list())}
        elif command == "capture":
            path = args.text_file or args.session_json
            # Bounded read also applies to pipes/files that grow during capture.
            with path.open("rb") as stream:
                raw = stream.read(MAX_INPUT + 1)
            if len(raw) > MAX_INPUT:
                raise ValueError("Capture input exceeds 16 MiB")
            text = raw.decode("utf-8")
            original = ({"messages": [{"role": "user", "content": text}]} if args.text_file else json.loads(text))
            capture_id = store.capture(original, group=args.group, split=args.split,
                                       source_kind=args.source_kind, provenance=args.provenance)
            result = {"capture_id": capture_id, "stage": "local-share-preview-before-final-gate"}
        elif command == "list":
            result = store.list()
        elif command == "disable":
            store.disable()
            result = {"enabled": False}
        elif command == "prune":
            # Store startup already purged expired rows.
            result = {"expired_captures_remaining": store.prune()}
        elif command == "delete":
            store.delete(args.capture_id)
            result = {"deleted": args.capture_id}
        elif command == "review":
            result = {"review_file": str(store.review(args.capture_id))}
        elif command == "import-corrections":
            with args.file.open("rb") as stream:
                raw = stream.read(MAX_INPUT + 1)
            if len(raw) > MAX_INPUT:
                raise ValueError("Correction file exceeds 16 MiB")
            result = {"annotations": store.import_corrections(json.loads(raw))}
        elif command == "annotate":
            capture = store.get(args.capture_id)
            capture["annotations"].append({"unit": args.unit, "start": args.start, "end": args.end,
                                            "decision": args.decision, "entity_type": args.entity_type,
                                            "note": args.note, "review_status": "human_reviewed" if args.human_reviewed else "assistant_draft"})
            result = {"annotations": store.import_corrections({"capture_id": args.capture_id,
                      "pair_hash": capture["pair_hash"], "revision": capture["revision"],
                      "policy": POLICY, "annotations": capture["annotations"]})}
        elif command == "run":
            # This is a dedicated CLI process: disallow Python socket egress in
            # addition to local-files-only model loading and offline hub settings.
            def offline(event, _args):
                if event in ("socket.connect", "socket.getaddrinfo", "socket.sendto"):
                    raise RuntimeError("Evaluation is offline; network access refused")
            sys.addaudithook(offline)
            predict, info = (gliner_predictor(args.model_dir, args.threshold) if args.engine == "gliner" else rules_predictor())
            report = evaluate(store, predict, info, split=args.split, include_drafts=args.include_drafts)
            name = "report-" + args.engine + "-" + digest(report)[:16] + ".json"
            output = store.root / name
            private_write(output, json.dumps(report, indent=2) + "\n")
            result = {"report": str(output), "targets": report["targets"],
                      "include_drafts": report["include_drafts"], "detection_metrics": report["detection_metrics"],
                      "sensitive_span_coverage": report["sensitive_span_coverage"]}
        else:
            raise ValueError("Unknown evaluation command")
        print(json.dumps(result, indent=2))
    except (ValueError, OSError, RuntimeError) as exc:
        # No source text or arbitrary scanner error payload in the terminal.
        print("Redaction evaluation failed: " + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__), file=sys.stderr)
        raise SystemExit(2) from None
    finally:
        if store is not None:
            store.close()
