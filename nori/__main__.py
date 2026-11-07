import argparse
import json
import sys
from .demo import run_demo
from .store import Store, NoriError


def main():
    parser = argparse.ArgumentParser(description='Nori P1 (mock-only)')
    sub = parser.add_subparsers(dest='action', required=True)
    demo = sub.add_parser('demo', help='Run the failure/rework/acceptance example in a NEW directory')
    demo.add_argument('directory')
    status = sub.add_parser('status', help='Read recorded state; does not claim files remain unchanged')
    status.add_argument('home')
    recover = sub.add_parser('recover', help='Explicitly restore valid backup, always blocked for manual review')
    recover.add_argument('home')
    args = parser.parse_args()
    try:
        if args.action == 'demo':
            result = run_demo(args.directory)
        else:
            with Store(args.home, recover=args.action == 'recover') as store:
                if store.state is None:
                    raise NoriError('No project state')
                result = store.state
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (NoriError, OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
