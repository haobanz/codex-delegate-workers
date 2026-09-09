#!/usr/bin/env python3
"""Minimal pre-capability management entry-point fixture."""

import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home")
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
