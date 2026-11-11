"""Refresh or inspect the model database without opening the GUI."""
import argparse
import json
import model_catalog as catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true', help='Check all configured online model sources')
    args = parser.parse_args()
    state = catalog.refresh_sources({}) if args.refresh else catalog.load_state()
    print(json.dumps(catalog.catalog_status(state), indent=2))
    return 1 if state.get('warnings') else 0


if __name__ == '__main__':
    raise SystemExit(main())
