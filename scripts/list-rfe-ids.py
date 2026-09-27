#!/usr/bin/env python3
"""List RFE IDs from a config file or Jira JQL query.

Usage:
    # From config file (existing behavior)
    python3 scripts/list-rfe-ids.py --config config/road-to-production/batch-03.yaml

    # From Jira through the work type's intake gate (types/<type>/type.yaml; --type picks it, default rfe-strategy)
    python3 scripts/list-rfe-ids.py --jql-default

    # Same, from a legacy pipeline-settings YAML passed explicitly
    python3 scripts/list-rfe-ids.py --jql-default config/pipeline-settings.yaml

    # From Jira with raw JQL
    python3 scripts/list-rfe-ids.py --jql 'project = RHAIRFE AND labels = "strat-creator-3.5"'

    # Batching
    python3 scripts/list-rfe-ids.py --jql-default --batch-size 10
    python3 scripts/list-rfe-ids.py --jql-default --batch-size 10 --batch-offset 10
"""

import argparse
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(__file__))

import type_registry

# The work type's descriptor is the source of the intake gate, the skip rules and the batch size
# (--type picks it). A pipeline-settings YAML is used instead only when --jql-default names one.
_TYPES = type_registry.load()


def _settings(path):
    """The legacy settings YAML at ``path``, or None when no path was given (descriptor mode)."""
    if not path:
        return None
    with open(path) as f:
        return yaml.safe_load(f) or {}


def ids_from_config(config_path, baseline=None):
    """Read RFE IDs from a YAML config file."""
    with open(config_path) as f:
        data = yaml.safe_load(f)
    ids = []
    for rfe in data.get("test_rfes", []):
        is_baseline = rfe.get("baseline", False)
        if baseline is True and not is_baseline:
            continue
        if baseline is False and is_baseline:
            continue
        ids.append(rfe["id"])
    return ids


def ids_from_jql(jql):
    """Search Jira with a JQL query and return issue keys."""
    from jira_utils import require_env, search_issues

    server, user, token = require_env()
    if not all([server, user, token]):
        print("Error: JIRA_SERVER, JIRA_USER, JIRA_TOKEN required for "
              "--jql mode.", file=sys.stderr)
        sys.exit(2)
    issues = search_issues(server, user, token, jql, fields=["key"])
    return [issue["key"] for issue in issues]


def main():
    parser = argparse.ArgumentParser(description="List RFE IDs")

    source = parser.add_mutually_exclusive_group()
    source.add_argument("--config", default=None,
                        help="Path to batch config YAML")
    source.add_argument("--jql", default=None,
                        help="Raw JQL query string")
    source.add_argument("--jql-default", nargs="?", const="",
                        default=None, metavar="SETTINGS",
                        help="Build JQL from the work type's intake gate "
                             "(optionally pass a legacy pipeline-settings YAML instead)")

    baseline_group = parser.add_mutually_exclusive_group()
    baseline_group.add_argument("--baseline", action="store_true",
                                help="Only baseline RFEs (config mode)")
    baseline_group.add_argument("--no-baseline", action="store_true",
                                help="Exclude baseline RFEs (config mode)")

    parser.add_argument("--include-processed", action="store_true",
                        help="Include RFEs whose STRATs already have "
                             "skip labels (default: exclude them)")
    parser.add_argument("--verbose", action="store_true",
                        help="Log each excluded RFE individually")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="Limit output to N RFE IDs")
    parser.add_argument("--batch-offset", type=int, default=0,
                        help="Skip first N RFE IDs")
    parser.add_argument("--type", choices=_TYPES.choices(), default=None,
                        help="Work type for --jql-default's gate and the already-processed check "
                             "(default: rfe-strategy)")

    args = parser.parse_args()
    if args.type and args.jql is None and args.jql_default != "":
        parser.error("--type works with --jql-default (no settings file) or --jql")

    # rfe-creator's ladder: --type, else the default. The ids are this script's output, not a signal.
    # The TYPE RESOLVED line only for an explicit --type, so a run without it prints what it did before.
    resolution = type_registry.resolve(_TYPES, explicit_type=args.type)
    if resolution.rung != type_registry.LEGACY_DEFAULT_RUNG:
        print(resolution.line(), file=sys.stderr)
    work_type = resolution.desc

    # Determine source mode
    if args.jql is not None:
        ids = ids_from_jql(args.jql)
        print(f"JQL returned {len(ids)} RFE(s)", file=sys.stderr)

    elif args.jql_default is not None:
        from jira_utils import build_jql_from_config, build_jql_from_type
        settings = _settings(args.jql_default)
        jql = build_jql_from_config(args.jql_default) if args.jql_default else build_jql_from_type(work_type)
        print(f"JQL: {jql}", file=sys.stderr)
        ids = ids_from_jql(jql)
        print(f"JQL returned {len(ids)} RFE(s)", file=sys.stderr)

        # Batch size: CLI, else the settings file, else the descriptor
        if args.batch_size is None:
            args.batch_size = settings.get("batch_size") if args.jql_default else work_type.get("discovery.batch_size")

    else:
        config_path = args.config or str(
            Path(__file__).resolve().parent.parent / "config" / "test-rfes.yaml"
        )
        if not Path(config_path).exists():
            print(f"Error: {config_path} not found", file=sys.stderr)
            sys.exit(1)
        baseline = None
        if args.baseline:
            baseline = True
        elif args.no_baseline:
            baseline = False
        ids = ids_from_config(config_path, baseline)

    # Exclude already-processed RFEs (JQL modes only, unless --include-processed)
    in_jql_mode = args.jql is not None or args.jql_default is not None
    if not args.include_processed and in_jql_mode:
        from jira_utils import find_processed_rfe_ids, require_env
        settings = _settings(args.jql_default) if args.jql_default is not None else None
        if settings is not None:
            skip_labels = settings.get("skip_labels", [])
            excluded_strat_statuses = settings.get("excluded_strat_statuses", [])
        else:
            skip_labels = work_type.get("inputs.0.skip_if.labels_any")
            excluded_strat_statuses = work_type.get("inputs.0.skip_if.statuses")
        if skip_labels or excluded_strat_statuses:
            server, user, token = require_env()
            processed = find_processed_rfe_ids(
                server, user, token, skip_labels,
                excluded_strat_statuses=excluded_strat_statuses, desc=work_type)
            excluded_ids = [i for i in ids if i in processed]
            ids = [i for i in ids if i not in processed]
            if excluded_ids:
                if args.verbose:
                    for eid in excluded_ids:
                        print(f"  excluding {eid}", file=sys.stderr)
                print(f"Excluded {len(excluded_ids)} already-processed "
                      f"RFE(s), {len(ids)} remaining",
                      file=sys.stderr)

    # Apply batching
    ids = ids[args.batch_offset:]
    if args.batch_size is not None:
        ids = ids[:args.batch_size]

    for rfe_id in ids:
        print(rfe_id)


if __name__ == "__main__":
    main()
