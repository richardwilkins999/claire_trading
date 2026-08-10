#!/bin/sh
# DESIGN.md §18 — money and authorization layers must run offline, sub-second.
cd "$(dirname "$0")/.." && exec ./venv/bin/pytest -q tests "$@"
