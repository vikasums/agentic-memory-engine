#!/usr/bin/env python3
"""CLI interface for Agentic Memory Engine."""

import asyncio
import json
import sys
from datetime import datetime, timedelta
from typing import Optional
import argparse

from .engine import create_engine, MemoryEngine
from .models import Scope


class MemoryCLI:
    def __init__(self):
        self.engine: Optional[MemoryEngine] = None

    def init_engine(self):
        if self.engine is None:
            self.engine = create_engine()

    async def ingest(self, file_path: str, user_id: str, scope: str = "user"):
        """Ingest memory from file."""
        self.init_engine()
        try:
            with open(file_path, 'r') as f:
                text = f.read()

            scope_enum = Scope[scope.upper()] if scope.upper() in Scope.__members__ else Scope.USER
            mem_ids = await self.engine.process_paragraph_async(text, user_id, scope_enum)
            print(f"✓ Ingested {len(mem_ids)} facts from {file_path}")
            for mem_id in mem_ids:
                print(f"  - {mem_id}")
        except FileNotFoundError:
            print(f"✗ File not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)

    def retrieve(self, query: str, user_id: str, top_k: int = 5, scope: str = "user"):
        """Retrieve memories for query."""
        self.init_engine()
        try:
            scope_enum = Scope[scope.upper()] if scope.upper() in Scope.__members__ else Scope.USER
            records = self.engine.retrieve_memories(query, user_id, top_k, scope=scope_enum)

            if not records:
                print(f"No memories found for: {query}")
                return

            print(f"\n📚 Retrieved {len(records)} memories:\n")
            for i, r in enumerate(records, 1):
                age_str = f"{r.age_days:.1f}d old" if r.age_days > 0 else "just now"
                print(f"{i}. {r.text}")
                print(f"   Score: {r.score:.3f} | Similarity: {r.similarity:.3f} | Decay: {r.decay_factor:.3f}")
                print(f"   Age: {age_str} | Source: {r.source}\n")
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)

    def profile(self, user_id: str, recent_days: int = 7):
        """Generate and display user profile."""
        self.init_engine()
        try:
            profile_data = self.engine.generate_user_profile(user_id, recent_days)

            print(f"\n👤 User Profile: {user_id}\n")
            print(f"Generated: {datetime.fromtimestamp(profile_data['profile_timestamp']).isoformat()}")

            print(f"\n📌 Stable Facts ({len(profile_data['stable_facts'])}):")
            for fact in profile_data['stable_facts'][:10]:
                print(f"  • {fact}")
            if len(profile_data['stable_facts']) > 10:
                print(f"  ... and {len(profile_data['stable_facts']) - 10} more")

            print(f"\n⏱️  Recent Activity (last {recent_days}d) ({len(profile_data['recent_activity'])}):")
            for activity in profile_data['recent_activity'][:10]:
                print(f"  • {activity}")
            if len(profile_data['recent_activity']) > 10:
                print(f"  ... and {len(profile_data['recent_activity']) - 10} more")
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)

    def metrics(self):
        """Display storage metrics."""
        self.init_engine()
        try:
            footprint = self.engine.get_storage_footprint()
            print(f"\n📊 Storage Metrics:\n")
            print(f"SQLite DB:        {footprint['sqlite_file_size_kb']} KB")
            print(f"LanceDB Folder:   {footprint['lancedb_folder_size_kb']} KB")
            print(f"Active Memories:  {footprint['active_memories']}")
            print(f"Inactive (old):   {footprint['inactive_memories']}")
            print(f"LanceDB Rows:     {footprint['lancedb_total_rows']}")
            total_kb = footprint['sqlite_file_size_kb'] + footprint['lancedb_folder_size_kb']
            print(f"\nTotal Size:       {total_kb} KB")
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)

    def cleanup(self, inactive_days: float = 7.0, max_age_days: float = 180.0):
        """Clean up expired and old facts."""
        self.init_engine()
        try:
            from .pruner import MemoryPruner
            pruner = MemoryPruner(self.engine)
            result = pruner.prune_now(inactive_days, max_age_days)
            print(f"✓ Cleanup complete:")
            print(f"  Deleted: {result.get('deleted_count', 0)} facts")
            print(f"  Expired: {result.get('expired_count', 0)} facts")
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)

    def set_expiry(self, subject: str, predicate: str, expires_in_days: int, user_id: str):
        """Set expiry for a fact (example utility)."""
        self.init_engine()
        try:
            from .models import FactRecord
            expires_at = (datetime.now() + timedelta(days=expires_in_days)).timestamp()
            fact = FactRecord(
                subject=subject,
                predicate=predicate,
                object_value="",
                expires_at=expires_at
            )
            print(f"✓ Set expiry for '{subject} {predicate}' to {expires_in_days} days from now")
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Agentic Memory Engine CLI - Manage long-term memory for AI agents"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Ingest command
    ingest_parser = subparsers.add_parser("ingest", help="Ingest memory from file")
    ingest_parser.add_argument("file", help="File to ingest")
    ingest_parser.add_argument("--user-id", required=True, help="User ID")
    ingest_parser.add_argument("--scope", default="user", choices=["user", "global"], help="Memory scope")

    # Retrieve command
    retrieve_parser = subparsers.add_parser("retrieve", help="Retrieve memories for query")
    retrieve_parser.add_argument("query", help="Query string")
    retrieve_parser.add_argument("--user-id", required=True, help="User ID")
    retrieve_parser.add_argument("--top-k", type=int, default=5, help="Number of results")
    retrieve_parser.add_argument("--scope", default="user", choices=["user", "global"], help="Memory scope")

    # Profile command
    profile_parser = subparsers.add_parser("profile", help="Generate user profile")
    profile_parser.add_argument("--user-id", required=True, help="User ID")
    profile_parser.add_argument("--recent-days", type=int, default=7, help="Recent activity window (days)")

    # Metrics command
    metrics_parser = subparsers.add_parser("metrics", help="Display storage metrics")

    # Cleanup command
    cleanup_parser = subparsers.add_parser("cleanup", help="Clean up expired facts")
    cleanup_parser.add_argument("--inactive-days", type=float, default=7.0, help="Inactive cutoff (days)")
    cleanup_parser.add_argument("--max-age-days", type=float, default=180.0, help="Max age cutoff (days)")

    # Expiry command
    expiry_parser = subparsers.add_parser("set-expiry", help="Set fact expiry")
    expiry_parser.add_argument("subject", help="Fact subject")
    expiry_parser.add_argument("predicate", help="Fact predicate")
    expiry_parser.add_argument("--expires-in-days", type=int, default=30, help="Days until expiry")
    expiry_parser.add_argument("--user-id", required=True, help="User ID")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    cli = MemoryCLI()

    if args.command == "ingest":
        asyncio.run(cli.ingest(args.file, args.user_id, args.scope))
    elif args.command == "retrieve":
        cli.retrieve(args.query, args.user_id, args.top_k, args.scope)
    elif args.command == "profile":
        cli.profile(args.user_id, args.recent_days)
    elif args.command == "metrics":
        cli.metrics()
    elif args.command == "cleanup":
        cli.cleanup(args.inactive_days, args.max_age_days)
    elif args.command == "set-expiry":
        cli.set_expiry(args.subject, args.predicate, args.expires_in_days, args.user_id)


if __name__ == "__main__":
    main()
