#!/usr/bin/env python3
"""pytest plugin: emit collected node IDs as JSON (Issue #1064 review).

Parsing ``pytest --collect-only -q`` stdout line-by-line is fragile: plugin banners,
warnings and summary lines contaminate the nodeid set. This plugin hooks
``pytest_collection_finish(session)`` and writes the authoritative ``session.items``
nodeids to the JSON file named by the ``COLLECT_NODEIDS_OUT`` environment variable.

Usage:
    COLLECT_NODEIDS_OUT=/tmp/n.json \
      uv run --locked pytest --collect-only -q -p no:cacheprovider \
        -p scripts.ci.collect_nodeids_plugin <scope argv>

(or load by file path via ``-p`` with the module importable on sys.path).
"""

from __future__ import annotations

import json
import os


def pytest_collection_finish(session) -> None:  # noqa: ANN001 - pytest hook
    out = os.environ.get("COLLECT_NODEIDS_OUT")
    if not out:
        return
    nodeids = [item.nodeid for item in session.items]
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"nodeids": sorted(nodeids), "count": len(nodeids)}, fh)


def pytest_configure(config) -> None:  # noqa: ANN001 - pytest hook
    """Record the resolved xdist worker count for AC9 evidence (Issue #1064 review).

    Writes the controller-side resolved ``numprocesses`` to the file named by
    ``XDIST_NUMPROCESSES_OUT``. With a fixed ``-n <N>`` plan this is exactly N, which is
    the authoritative resolved worker count (not a ``nproc`` proxy). Skipped on xdist
    worker subprocesses (which carry ``workerinput``).
    """
    out = os.environ.get("XDIST_NUMPROCESSES_OUT")
    if not out or hasattr(config, "workerinput"):
        return
    numprocesses = getattr(config.option, "numprocesses", None)
    dist = getattr(config.option, "dist", None)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"resolved_workers": numprocesses, "dist": dist}, fh)
