"""Fargate one-shot worker: python -m worker --job-id … --stage …"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from services.job_pipeline import VALID_STAGES, build_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("worker")


async def _run(job_id: str, stage: str) -> int:
    pipeline = build_pipeline()
    try:
        await pipeline.run_stage(job_id, stage)
        logger.info("Stage %s finished for job %s", stage, job_id)
        return 0
    except Exception:
        logger.exception("Stage %s failed for job %s", stage, job_id)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autocaption Fargate job worker")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--stage", required=True, choices=sorted(VALID_STAGES))
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.job_id, args.stage))


if __name__ == "__main__":
    sys.exit(main())
