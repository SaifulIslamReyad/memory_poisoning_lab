#!/usr/bin/env python3


# psql -h /var/run/postgresql -U saif-reyad -d postgres -c "CREATE DATABASE memory_poisoning_lab;"
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).parent
LOCAL_DB = os.environ.get("LOCAL_DB") or "memory_poisoning_lab"
LOCAL_USER = os.environ.get("LOCAL_USER") or "saif-reyad"
LOCAL_HOST = os.environ.get("LOCAL_HOST") or "/var/run/postgresql"
CLOUD_URL = os.environ.get("CLOUD_DB_URL") or "postgresql://neondb_owner:npg_h5QNHc0pPKaR@ep-summer-cherry-b4i27tw1-pooler.c-6.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
SCHEMA = "sandbox_exp_c61f1af5a75244d7b9ae5a2bbb25d1bb"


def run_cmd(cmd):
    print(f"Running: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    res = subprocess.run(cmd, shell=isinstance(cmd, str))
    if res.returncode != 0:
        print(f"Command failed with return code {res.returncode}", file=sys.stderr)
        return False
    return True


def push():
    print("\n=== PUSHING LOCAL -> CLOUD (Neon) ===")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_file = PROJECT_ROOT / f"local_{LOCAL_DB}_{ts}.dump"
    print(f"Dumping local DB '{LOCAL_DB}' to {dump_file}...")
    cmd = ["pg_dump", "-h", LOCAL_HOST, "-U", LOCAL_USER, "-Fc", "-f", str(dump_file), LOCAL_DB]
    if not run_cmd(cmd):
        sys.exit(1)
    print(f"Done. Dump size: {dump_file.stat().st_size} bytes")
    
    print("\nStep 1: Drop the existing schema (if overwriting)")
    drop_cmd = f'psql "{CLOUD_URL}" -c "DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;"'
    if not run_cmd(drop_cmd):
        print("Note: drop schema may have failed or schema didn't exist, continuing...", file=sys.stderr)
    
    print("\nStep 2: Restore the dump to Neon")
    restore_cmd = ["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges", "-d", CLOUD_URL, str(dump_file)]
    if not run_cmd(restore_cmd):
        print("Note: pg_restore had warnings/errors, continuing...", file=sys.stderr)
    
    print("\nStep 3: Verify your data")
    run_cmd(f'psql "{CLOUD_URL}" -c "\\dn"')
    run_cmd(f'psql "{CLOUD_URL}" -c "\\dt {SCHEMA}.*"')
    print(f"\n✓ Push complete. Dump saved to {dump_file}")


def pull():
    print("\n=== PULLING CLOUD (Neon) -> LOCAL ===")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_file = PROJECT_ROOT / f"cloud_{LOCAL_DB}_{ts}.dump"
    print(f"Dumping from Neon to {dump_file}...")
    cmd = ["pg_dump", "-Fc", "-f", str(dump_file), CLOUD_URL]
    if not run_cmd(cmd):
        sys.exit(1)
    print(f"Done. Dump size: {dump_file.stat().st_size} bytes")
    
    print(f"\nRestoring to local database '{LOCAL_DB}'...")
    run_cmd(["dropdb", "-h", LOCAL_HOST, "-U", LOCAL_USER, "--if-exists", LOCAL_DB])
    if not run_cmd(["createdb", "-h", LOCAL_HOST, "-U", LOCAL_USER, LOCAL_DB]):
        sys.exit(1)
    
    restore_cmd = ["pg_restore", "--no-owner", "--no-privileges", "-h", LOCAL_HOST, "-U", LOCAL_USER, "-d", LOCAL_DB, str(dump_file)]
    if not run_cmd(restore_cmd):
        print("Note: pg_restore had warnings, continuing...", file=sys.stderr)
    
    print(f"\n✓ Pull complete. Dump saved to {dump_file}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Push/Pull between local Postgres and Neon cloud DB")
    parser.add_argument("--push", action="store_true", help="Push local DB to cloud (overwrite cloud)")
    parser.add_argument("--pull", action="store_true", help="Pull cloud DB to local (overwrite local)")
    parser.add_argument("--sync", action="store_true", help="Push then pull")
    args = parser.parse_args()
    
    if args.sync:
        args.push = True
        args.pull = True
    
    if args.push:
        push()
    if args.pull:
        pull()
    if not any([args.push, args.pull, args.sync]):
        parser.print_help()
        print("\nExamples:")
        print("  python3 cloudpush.py --push   # Local -> Cloud")
        print("  python3 cloudpush.py --pull   # Cloud -> Local")
        print("  python3 cloudpush.py --sync   # Push then pull")


if __name__ == "__main__":
    main()
