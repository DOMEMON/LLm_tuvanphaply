"""Local immutable-file helpers; no dataset downloads during application startup."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f'IMMUTABLE_SNAPSHOT_EXISTS: {path.name}')
    else:
        with path.open('xb') as stream:
            stream.write(data)
