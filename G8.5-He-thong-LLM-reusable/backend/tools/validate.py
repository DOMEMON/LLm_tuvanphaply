"""Validate one package without activating or modifying a deployment."""
import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from app.rag.g85.extensions import POLICIES
from app.rag.g85.package_loader import read_package

SCHEMA = Path(__file__).resolve().parents[1] / 'g85-domain.schema.json'

def validate_profile(profile):
    Draft202012Validator(json.loads(SCHEMA.read_text(encoding='utf-8'))).validate(profile)
    ids = [a['id'] for a in profile['attributes']]
    if len(ids) != len(set(ids)):
        raise ValueError('DUPLICATE_ATTRIBUTE_ID')
    if profile['retrieval']['exact_lookup'] and not ids:
        raise ValueError('EXACT_LOOKUP_REQUIRES_ATTRIBUTE_SCHEMA')
    return profile

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    args = parser.parse_args()
    package = read_package(args.package, SCHEMA, POLICIES)
    print(json.dumps({'valid': True, 'version': package.version,
                      'entities': len(package.catalog), 'passages': len(package.passages),
                      'activated': False}))
