"""Offline, evidence-bound audit of committed lake partitions. Never repairs data."""
import argparse
from pathlib import Path
from quant_lake import ROOT
from quant_data_quality import run_quality_check
from quant_inventory_health import inventory_health, health_summary
from quant_data_quality import inventory


def audit(root=ROOT):
    report = run_quality_check(root)
    count = sum(f['severity'] in ('Error', 'Warning') for f in report['findings'])
    print(f"Audit {report['audit_version']}: {len(report['streams'])} streams, {count} findings requiring review.")
    print(f"Snapshot stable: {report['stable_snapshot']}. Report: {Path(root) / 'quality_report.json'}")
    summary=health_summary(inventory_health(root),inventory(root))
    if summary['error']:print('Required-window session readiness: not verified; inspect the audit findings.')
    else:print(f"Required-window session readiness: {summary['percent']:.1f}% ({summary['ready']}/{summary['stored']} instrument-sessions); {summary['excluded']} exclusions.")
    print('Historical calendar coverage and options identity are separate from required-window readiness.')
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    raise SystemExit(bool(audit(parser.parse_args().root)))
