"""
cleanup_memory.py - Cleanup script for expired visitor memory graphs in RingWatch.
Deletes memory_visitor-* graphs whose (:Meta) created_at is older than max_age (default 24h = 86400s).
"""

import argparse
from datetime import datetime, timezone
import re
from typing import List
import db as db_module


def cleanup_visitor_graphs(max_age_seconds: float = 86400.0) -> List[str]:
    """
    Find and delete memory_visitor-* graphs that are older than max_age_seconds.
    Uses the created_at property stored in the (:Meta) node of each memory graph.
    Returns the list of deleted graph names.
    """
    client = db_module.get_client()
    now = datetime.now(timezone.utc)
    all_graphs = client.list_graphs()
    deleted_graphs = []

    for g_name in all_graphs:
        if not g_name.startswith("memory_visitor-"):
            continue

        try:
            g = client.select_graph(g_name)
            # Query created_at from (:Meta) node
            rs = g.query("MATCH (m:Meta) RETURN m.created_at LIMIT 1").result_set
            if not rs or not rs[0] or not rs[0][0]:
                continue

            created_at_str = str(rs[0][0])
            created_at = datetime.fromisoformat(created_at_str)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            age_seconds = (now - created_at).total_seconds()
            if age_seconds > max_age_seconds:
                g.delete()
                deleted_graphs.append(g_name)
        except Exception:
            # Skip errors on individual graphs
            continue

    return deleted_graphs


def main():
    parser = argparse.ArgumentParser(description="Cleanup expired visitor memory graphs.")
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=86400.0,
        help="Max age in seconds (default 86400 = 24 hours)",
    )
    args = parser.parse_args()

    deleted = cleanup_visitor_graphs(max_age_seconds=args.max_age_seconds)
    print(f"Cleaned up {len(deleted)} expired visitor memory graph(s): {deleted}")


if __name__ == "__main__":
    main()
