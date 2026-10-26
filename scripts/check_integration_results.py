"""Fail CI when the dedicated database suite has no cases, skips, errors or failures."""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path


def check_results(path: Path) -> int:
    cases = ET.parse(path).getroot().findall(".//testcase")
    failed = not cases or any(
        any(case.find(tag) is not None for tag in ("skipped", "error", "failure")) for case in cases
    )
    print(f"Database integration gate: {len(cases)} cases, {'FAIL' if failed else 'PASS'}")
    return int(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path)
    raise SystemExit(check_results(parser.parse_args().junit))
