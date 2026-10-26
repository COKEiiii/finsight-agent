import argparse
import json

from finsight.service import FinSight


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    try:
        result = FinSight(args.data_dir).run_job(args.job)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0 if result["publishable"] else 2
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
