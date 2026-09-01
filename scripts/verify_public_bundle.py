"""Fail when configured server credentials appear in browser-delivered Next.js assets."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SECRET_ENV_NAMES = (
    "LEAKPROOF_OPERATOR_API_TOKEN",
    "LEAKPROOF_RAZORPAY_KEY_SECRET",
    "LEAKPROOF_RAZORPAY_WEBHOOK_SECRET",
    "LEAKPROOF_OPENAI_API_KEY",
    "LEAKPROOF_RESEND_API_KEY",
    "LEAKPROOF_RESEND_WEBHOOK_SECRET",
    "LEAKPROOF_RECOVERY_TOKEN_SECRET",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=Path("dashboard/.next"))
    parser.add_argument("--forbid", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    public_roots = [args.build_dir / "static", args.build_dir / "server" / "app"]
    roots = [path for path in public_roots if path.exists()]
    if not roots:
        print(f"no built browser assets found under {args.build_dir}", file=sys.stderr)
        return 2
    secrets_to_find = {
        value
        for name in SECRET_ENV_NAMES
        if len(value := os.environ.get(name, "")) >= 8
    }
    secrets_to_find.update(value for value in args.forbid if len(value) >= 8)
    if not secrets_to_find:
        print("no configured credential values or canaries were supplied", file=sys.stderr)
        return 2

    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            contents = path.read_bytes()
            if any(secret.encode() in contents for secret in secrets_to_find):
                print(f"credential value found in browser-delivered asset: {path}", file=sys.stderr)
                return 1
    print(f"browser bundle is clear of {len(secrets_to_find)} credential value(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
