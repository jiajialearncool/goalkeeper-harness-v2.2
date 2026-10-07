"""Second-stage entry point. Does nothing without explicit inference authorization."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from goalkeeper.evaluation import GROUPS, evaluate, execution_identity
from goalkeeper.paths import PROJECT
from goalkeeper.storage import fingerprint, save_report, snapshot
from goalkeeper.readiness import package_identity


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["connect", "candidate"], required=True)
    parser.add_argument("--allow-real-model", action="store_true")
    limits = parser.add_mutually_exclusive_group(required=True)
    limits.add_argument("--authorized-token-limit", type=int)
    limits.add_argument("--unlimited-tokens", action="store_true")
    parser.add_argument("--candidate", choices=[name for name in package_identity() if name != "connect"], default="C1-v2")
    parser.add_argument("--readiness", type=Path)
    retries = parser.add_mutually_exclusive_group()
    retries.add_argument("--recover-transient-once", action="store_true",
                        help="Allow one staged retry per logical request within the original deadline")
    retries.add_argument("--transient-retries", type=int, choices=range(4), default=0)
    args = parser.parse_args()
    retry_count = 1 if args.recover_transient_once else args.transient_retries
    if not args.allow_real_model:
        raise SystemExit("当前阶段禁止真实模型调用。")
    os.environ["TIKTOKEN_CACHE_DIR"] = str(PROJECT / "work/tokenizer-cache")
    package = PROJECT / "evaluation/private" / ("connect" if args.mode == "connect" else args.candidate)
    result = await evaluate(
        package,
        authorized=True,
        shared_limit=args.authorized_token_limit,
        readiness=args.readiness,
        connectivity=args.mode == "connect",
        transient_retries=retry_count,
        unlimited_tokens=args.unlimited_tokens,
    )
    if args.mode == "connect":
        data = json.loads((result / "results.json").read_text())
        rows = data["results"]
        passed = [row["group"] for row in rows if isinstance(row, dict) and row["passed"]]
        frozen = json.loads((result / "freeze.json").read_text())
        identity = execution_identity(retry_count)
        identity_matches = all(frozen.get(k) == v for k, v in identity.items())
        package_matches = frozen["package_hash"] == fingerprint(snapshot(package))
        usage = json.loads((result / "transport-usage.json").read_text())
        accounting_complete = usage["in_flight"] == 0
        save_report(
            result / "connectivity.json",
            {
                "kind": "four_native_connectivity",
                "passed_groups": passed,
                "all_passed": passed == list(GROUPS) and identity_matches
                              and package_matches and accounting_complete,
                "execution_identity": {k: frozen[k] for k in identity},
                "execution_identity_matches": identity_matches,
                "package_unchanged": package_matches,
                "accounting_complete": accounting_complete,
                "usage_complete": usage["unknown_calls"] == 0,
                "unknown_calls": usage["unknown_calls"],
                "result_directory": str(result),
            },
        )
    print(str(result))
    data = json.loads((result / "results.json").read_text())
    if not all(row.get("passed") is True for row in data["results"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
