"""Audit a saved research manifest offline: python -m assay.audit --help."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from assay.research_audit import audit_manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,help='New JSON output file; otherwise print the report')
    parser.add_argument('--offset',type=int,default=0)
    parser.add_argument('--limit',type=int,default=20)
    parser.add_argument('--expected-manifest-hash')
    args=parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error('output already exists; choose a new report file')
    try:
        manifest=json.loads(args.manifest.read_text())
        result=audit_manifest(manifest,offset=args.offset,limit=args.limit,
                              expected_manifest_hash=args.expected_manifest_hash)
    except (ValueError,OSError) as exc:
        parser.error(str(exc))
    content=json.dumps(result,indent=2,allow_nan=False)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        # Exclusive creation also protects against a concurrent report writer.
        with args.output.open('x') as stream:
            stream.write(content)
        print(json.dumps({k:result[k] for k in ['status','record_integrity','manifest_hash','finding_counts']}))
    else:
        print(content,end='')
    return 1 if result['record_integrity']=='failed' else 0


if __name__=='__main__':
    raise SystemExit(main())
