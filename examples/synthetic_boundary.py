"""From repo root: python -m examples.synthetic_boundary /tmp/nori-new-fixture"""
import argparse
import json
from nori.boundary import run_synthetic_demo


def main():
    parser = argparse.ArgumentParser(description='Run only a disposable synthetic Nori boundary fixture')
    parser.add_argument('directory', help='New or empty directory, never a business project')
    args = parser.parse_args()
    print(json.dumps(run_synthetic_demo(args.directory), indent=2))


if __name__ == '__main__':
    main()
