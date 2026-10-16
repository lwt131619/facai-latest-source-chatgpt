"""Local appliance backup entry: consistent SQLite snapshot + isolated restore drill."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from facai.backup import create_backup, drill_backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path,
                        default=Path(os.environ.get('FACAI_DATA_DIR', str(ROOT / 'data'))))
    parser.add_argument('--dest', type=Path, help='New SQLite snapshot path; never overwritten')
    args = parser.parse_args(argv)
    try:
        snapshot = create_backup(args.data_dir, args.dest)
        report = drill_backup(args.data_dir, snapshot)
    except (OSError, ValueError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print('Database-only snapshot. Keep master.key / environment credentials, local-owner.json '
          'and workspace files in a separate private backup. Stop every FACAI process before restore.')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
