#!/usr/bin/env python3
"""Minimal pre-capability configuration reader used by lifecycle tests."""

import argparse
import json
from pathlib import Path


CONFIG = Path(__file__).resolve().parent.parent / "workers.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    args = parser.parse_args(argv)
    if args.command == "show":
        print(json.dumps(json.loads(CONFIG.read_text(encoding="utf-8")),
                         indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
