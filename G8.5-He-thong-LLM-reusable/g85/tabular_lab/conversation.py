"""Small durable conversation state, scoped by dataset directory and version.

Only the two most recently resolved source rows are retained. Writes are atomic;
revision checks are serialized by the API's single worker (not a multi-process lock).
"""
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


def path_for(folder, conversation_id):
    if not re.fullmatch(r'[a-f0-9]{32}', conversation_id):
        raise KeyError('CONVERSATION_NOT_FOUND')
    return Path(folder) / 'conversations' / (conversation_id + '.json')


def read(folder, conversation_id, version, revision=None):
    path = path_for(folder, conversation_id)
    if not path.is_file():
        raise KeyError('CONVERSATION_NOT_FOUND')
    state = json.loads(path.read_text(encoding='utf-8'))
    if state['version'] != version or (revision is not None and state['revision'] != revision):
        raise ValueError('CONVERSATION_REVISION_MISMATCH')
    return state


def write(folder, state):
    path = path_for(folder, state['conversation_id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         suffix='.tmp', delete=False) as stream:
            name = stream.name
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


def create(folder, version):
    state = {'conversation_id': uuid.uuid4().hex, 'version': version,
             'revision': 0, 'recent_entities': []}
    write(folder, state)
    return state
