"""命令行入口：

    python3 -m market_index_bureau.restatement \
        --input <JSON> --database <DB> --output <JSON>

输出 JSON 含 publication_id、vintages、contributions、bridge_status、
reconciliation；重复运行同一输入走幂等重放，不产生新版本行。
"""
from __future__ import annotations

import argparse
import json
import sys

from market_index_bureau.restatement.domain import RestatementError
from market_index_bureau.restatement.service import analyze


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="市场价格指数重述与桥接分析")
    parser.add_argument("--input", required=True, help="分析输入 JSON 路径")
    parser.add_argument("--database", required=True, help="SQLite 版本库路径")
    parser.add_argument("--output", required=True, help="分析结果输出 JSON 路径")
    args = parser.parse_args(argv)

    try:
        result = analyze(args.input, args.database)
    except RestatementError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(
        json.dumps(
            {
                "publication_id": result["publication_id"],
                "output": args.output,
                "all_balanced": result["reconciliation"]["all_balanced"],
                "replayed": result["replayed"],
                "storage": result["storage"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
