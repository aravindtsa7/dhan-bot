"""Offline, evidence-bound audit of committed lake partitions. Never repairs data."""
import argparse
from pathlib import Path
from quant_lake import ROOT
from quant_data_quality import run_quality_check


def audit(root=ROOT):
    report = run_quality_check(root)
    count = sum(f['severity'] in ('Error', 'Warning') for f in report['findings'])
    print(f"Audit {report['audit_version']}: {len(report['streams'])} streams, {count} findings requiring review.")
    print(f"Snapshot stable: {report['stable_snapshot']}. Report: {Path(root) / 'quality_report.json'}")
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    raise SystemExit(bool(audit(parser.parse_args().root)))
