"""Validate/select a package for an EXISTING clean deployment; no rebuild or startup.

Default is validation only. --apply updates the compose bind mount and version,
after writing an exclusive backup. Restart backend separately when no chat is
running. The UI reads its profile over HTTP; reload after switching.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from g85.runtime.package_loader import read_package
from g85.runtime.extensions import POLICIES

ROOT = Path(__file__).resolve().parents[1]


def select_package(package_path, compose_path, *, apply=False):
    package_path, compose_path = Path(package_path).resolve(), Path(compose_path).resolve()
    package = read_package(package_path, ROOT / 'g85/schema/domain-profile.schema.json', POLICIES)
    payload = compose_path.read_bytes()
    compose = json.loads(payload)
    service = compose['services']['backend']
    if service.get('environment', {}).get('G85_PACKAGE_PATH') != '/run/g85-package':
        raise ValueError('EXISTING_CLEAN_DEPLOYMENT_REQUIRED')
    volumes = service.get('volumes', [])
    targets = [v for v in volumes if isinstance(v, dict) and v.get('target') == '/run/g85-package']
    if len(targets) != 1:
        raise ValueError('SINGLE_PACKAGE_MOUNT_REQUIRED')
    targets[0].update(type='bind', source=str(package_path), read_only=True)
    service['environment']['RAG_CORPUS_VERSION'] = package.version
    result = {'validated': True, 'profile': package.profile['id'], 'version': package.version,
              'entities': len(package.catalog), 'passages': len(package.passages),
              'configuration_updated': apply, 'runtime_restarted': False}
    if apply:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = compose_path.with_name('compose-before-package-' + stamp + '.json')
        with backup.open('xb') as stream:
            stream.write(payload)
        temporary = compose_path.with_name('compose-select-' + stamp + '.json')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(compose, indent=2) + '\n')
            # Do not overwrite an operator change made during validation.
            if compose_path.read_bytes() != payload:
                raise ValueError('COMPOSE_CHANGED_DURING_SELECTION')
            temporary.replace(compose_path)
        finally:
            temporary.unlink(missing_ok=True)
        result['backup'] = str(backup)  # Contents may contain secrets; never print them.
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--compose', type=Path, default=ROOT / '.runtime/g85-v1/compose.json')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(select_package(args.package, args.compose, apply=args.apply)))
