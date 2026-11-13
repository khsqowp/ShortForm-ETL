#!/usr/bin/env python3
"""Set category_group on archived places. Defaults to a read-only dry run."""
import argparse
import os
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from place_groups import classify_group

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='write category_group values')
    parser.add_argument('--db', default=os.environ.get('LINKCOLLECT_DB_PATH', str(Path(__file__).resolve().parents[1] / 'data' / 'linkcollect.sqlite3')))
    args = parser.parse_args()
    db = sqlite3.connect(args.db)
    columns = {row[1] for row in db.execute('PRAGMA table_info(places)')}
    if not {'name', 'category', 'category_group'} <= columns:
        raise SystemExit('places table must have name, category, and category_group columns')
    rows = db.execute('SELECT id,name,category,category_group FROM places').fetchall()
    changes = [(classify_group(name, category), place_id) for place_id, name, category, _ in rows]
    changed = sum(group != current for (group, _), (_, _, _, current) in zip(changes, rows))
    print('{} places; {} category_group values would change'.format(len(rows), changed))
    if args.apply:
        db.executemany('UPDATE places SET category_group=? WHERE id=?', changes)
        db.commit()
        print('Applied {} category_group updates'.format(changed))
    else:
        print('Dry run only; pass --apply to write')
    db.close()

if __name__ == '__main__':
    main()
