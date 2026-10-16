"""Local G8.5 multi-dataset API with immutable uploads and dataset-scoped keys.

Run: python -m uvicorn g85.tabular_lab.service:app --host 127.0.0.1 --port 8086
POST raw CSV/XLSX bytes to /v1/datasets?filename=table.csv.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import uuid
import io
import zipfile
import tempfile

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .datasets import ROOT, save
from .grounded import explicit_units, run_grounded
from .reusable import run_reusable
from .methods import Model, autoprofile
from .table import Table, sha
from . import conversation

MAX_BYTES = 20 * 1024 * 1024


class Version(BaseModel):
    version: str = Field(min_length=64, max_length=64, pattern=r'^[a-f0-9]{64}$')


class Query(Version):
    question: str = Field(min_length=1, max_length=6000)
    conversation_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    revision: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode='after')
    def context_pair(self):
        if (self.conversation_id is None) != (self.revision is None):
            raise ValueError('CONVERSATION_AND_REVISION_REQUIRED_TOGETHER')
        return self


class DatasetStore:
    def __init__(self, root, model_factory=Model, query_engine='g85_package'):
        if query_engine not in ('grounded', 'reusable_experimental', 'g85_package'):
            raise ValueError('INVALID_QUERY_ENGINE')
        self.query_engine = query_engine
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.model_factory = model_factory

    def ingest(self, content, filename, sheet=None, missing_values=('',)):
        suffix = Path(filename).suffix.lower()
        if suffix not in ('.csv', '.xlsx') or not content or len(content) > MAX_BYTES:
            raise ValueError('INVALID_FILE_OR_SIZE')
        dataset_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        folder = self.root / dataset_id
        source = folder / ('table' + suffix)
        save(source, content)
        table = Table(source, sheet=sheet, missing_values=missing_values)
        try:
            if len(table.columns) > 32:
                raise ValueError('SUPPORTED_MAX_32_COLUMNS')
            model = self.model_factory()
            try:
                proposal, timing = autoprofile(table, model)
                package_info = self.build(table, folder, model, proposal, filename) if self.query_engine == 'g85_package' else None
            finally:
                if hasattr(model, 'client'):
                    model.client.close()
            rejected_units = []
            for column in proposal['columns']:
                unit = column.get('unit', '')
                column['unit_proposal'] = unit
                if unit and not (explicit_units(unit) & explicit_units(column['name'])):
                    rejected_units.append(column['name'])
                    column['unit'] = ''
            profile = {'physical': table.profile, 'semantic_proposal': proposal,
                       'semantic_usage': 'display_only_not_used_for_query_planning',
                       'removed_unsupported_unit_proposals': rejected_units, 'timing': timing}
            if package_info:
                profile['semantic_usage'] = 'package_field_descriptions_with_source_cell_binding'
                profile['package'] = {k: v for k, v in package_info.items() if k not in ('path', 'mapping')}
                profile['package']['mapping'] = {k: package_info['mapping'][k] for k in ('title_columns', 'fields', 'semantic_accuracy_certified')}
            profile['contract'] = {
                'format': 'single_table_v1', 'source_sha256': table.hash,
                'candidate_keys': [c['name'] for c in table.columns if c['unique_non_null'] and c['null_count'] == 0],
                'missing_cells': sum(c['null_count'] for c in table.columns),
                'empty_columns': [c['name'] for c in table.columns if c['null_count'] == len(table.rows)],
                'supported': ['lookup', 'filter', 'count', 'sum', 'avg', 'min', 'max', 'distinct', 'up_to_8_intents', 'two_recent_rows'],
                'unsupported': ['join', 'group_by', 'top_n', 'unit_conversion', 'codebook_decoding'],
                'semantic_verification': 'not_performed',
                'experimental_disabled': ['semantic_text_filter'],
            }
            record = {'dataset_id': dataset_id, 'version': table.hash, 'status': 'ready',
                      'file': source.name, 'sheet': sheet, 'missing_values': list(missing_values),
                      'token_hash': hashlib.sha256(token.encode()).hexdigest(), 'profile': profile}
            if package_info:
                record['package_version'] = package_info['version']
            save(folder / 'dataset.json', (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode())
            return {**self.public(record), 'dataset_token': token}
        finally:
            table.close()

    def build(self, table, folder, model, proposal=None, title=None):
        from g85.package_builder import build_package, ROOT
        from g85.runtime.package_loader import read_package
        info = None
        if not (folder / 'package/manifest.json').is_file():
            info = build_package(table, folder / 'package', model, proposal=proposal, title=title)
        checked = read_package(folder / 'package', ROOT / 'schema/domain-profile.schema.json')
        try:
            if checked.table.hash != table.hash:
                raise ValueError('PACKAGE_SOURCE_MISMATCH')
            return info or {'path': str(folder / 'package'), 'version': checked.version,
                'source_sha256': table.hash, 'rows': len(table.rows), 'cells': len(checked.passages),
                'mapping': checked.mapping}
        finally:
            checked.table.close()

    def read(self, dataset_id, token):
        if not re.fullmatch(r'[a-f0-9]{32}', dataset_id):
            raise KeyError('DATASET_NOT_FOUND')
        path = self.root / dataset_id / 'dataset.json'
        if not path.is_file():
            raise KeyError('DATASET_NOT_FOUND')
        record = json.loads(path.read_text(encoding='utf-8'))
        if not secrets.compare_digest(record['token_hash'], hashlib.sha256(token.encode()).hexdigest()):
            raise KeyError('DATASET_NOT_FOUND')
        return record

    @staticmethod
    def public(record):
        return {k: record[k] for k in ('dataset_id', 'version', 'status', 'profile')}

    def create_conversation(self, dataset_id, token, version):
        record = self.read(dataset_id, token)
        if version != record['version']:
            raise ValueError('DATASET_VERSION_MISMATCH')
        return conversation.create(self.root / dataset_id, version)

    def query(self, dataset_id, token, version, question, conversation_id=None, revision=None):
        record = self.read(dataset_id, token)
        if version != record['version']:
            raise ValueError('DATASET_VERSION_MISMATCH')
        state = conversation.read(self.root / dataset_id, conversation_id, version, revision) if conversation_id else None
        source = self.root / dataset_id / record['file']
        if source.stat().st_size > MAX_BYTES or sha(source.read_bytes()) != version:
            raise ValueError('SOURCE_CHECKSUM_CHANGED')
        table = None
        try:
            model = self.model_factory()
            try:
                if self.query_engine == 'g85_package':
                    from g85.package_runtime import run_package
                    folder = self.root / dataset_id
                    if not record.get('package_version'):
                        # Upgrade an existing upload without removing its source or history.
                        table = Table(source, sheet=record['sheet'], missing_values=record['missing_values'])
                        info = self.build(table, folder, model)
                        record['package_version'] = info['version']
                        record['profile']['package'] = {k: v for k, v in info.items() if k not in ('path', 'mapping')}
                        record['profile']['semantic_usage'] = 'package_field_descriptions_with_source_cell_binding'
                        # Metadata upgrade is atomic; the uploaded source remains immutable.
                        pending_path = None
                        try:
                            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder,
                                                             suffix='.tmp', delete=False) as stream:
                                pending_path = Path(stream.name)
                                json.dump(record, stream, ensure_ascii=False, indent=2)
                                stream.flush()
                                os.fsync(stream.fileno())
                            os.replace(pending_path, folder / 'dataset.json')
                        finally:
                            if pending_path and pending_path.exists():
                                pending_path.unlink()
                    if sha((folder / 'package/manifest.json').read_bytes()) != record['package_version']:
                        raise ValueError('PACKAGE_CHECKSUM_MISMATCH')
                    response = run_package(folder / 'package', question, model,
                        raw_state=state.get('runtime_state') if state else None,
                        context=state['recent_entities'] if state else ())
                else:
                    table = Table(source, sheet=record['sheet'], missing_values=record['missing_values'])
                    if table.hash != version:
                        raise ValueError('SOURCE_CHECKSUM_CHANGED')
                    runner = run_reusable if self.query_engine == 'reusable_experimental' else run_grounded
                    response = runner(table, question, model, context=state['recent_entities'] if state else ())
            finally:
                if hasattr(model, 'client'):
                    model.client.close()
            result = {'dataset_id': dataset_id, 'version': version, 'results': response['results'],
                      'seconds': response['seconds']}
            if state is not None:
                state['revision'] += 1
                state['recent_entities'] = response.get('recent_entities', state['recent_entities'])
                if 'runtime_state' in response:
                    state['runtime_state'] = response['runtime_state']
                conversation.write(self.root / dataset_id, state)
                result['conversation'] = state
            return result
        finally:
            if table is not None:
                table.close()


def create_app(root=None, model_factory=Model, query_engine=None):
    store = DatasetStore(root or os.environ.get('G85_DATA_DIR') or ROOT / '.runtime/g85-tabular-service/datasets', model_factory,
                         query_engine or os.environ.get('G85_TABULAR_QUERY_ENGINE', 'g85_package'))

    @asynccontextmanager
    async def lifespan(app):
        app.state.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='g85-tabular')
        app.state.pending = 0
        yield
        app.state.executor.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(title='G8.5 — Automatic Knowledge Packages', version='0.7.0', lifespan=lifespan)

    def reserve():
        if app.state.pending >= 8:
            raise HTTPException(429, 'QUEUE_FULL')
        app.state.pending += 1

    async def submit(fn, *args, reserved=False):
        if not reserved:
            reserve()
        future = asyncio.get_running_loop().run_in_executor(app.state.executor, fn, *args)
        # Keep capacity reserved even if the requesting client disconnects.
        future.add_done_callback(lambda _: setattr(app.state, 'pending', app.state.pending - 1))
        try:
            return await asyncio.shield(future)
        except KeyError:
            raise HTTPException(404, 'DATASET_NOT_FOUND')
        except ValueError as exc:
            status = 409 if str(exc) in ('DATASET_VERSION_MISMATCH', 'SOURCE_CHECKSUM_CHANGED', 'CONVERSATION_REVISION_MISMATCH') else 422
            raise HTTPException(status, str(exc))
        except Exception:
            raise HTTPException(503, 'PROCESSING_UNAVAILABLE')

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'workers': 1, 'capacity_including_active': 8, 'pending': app.state.pending,
                'query_engine': store.query_engine, 'semantic_text_filter_enabled': False,
                'runtime_release': 'package-quality-20261014', 'lookup_review_enabled': False}

    static = Path(__file__).parent / 'static'
    app.mount('/workspace', StaticFiles(directory=static), name='workspace')

    @app.get('/', include_in_schema=False)
    async def workspace():
        return FileResponse(static / 'index.html', headers={'Cache-Control': 'no-store'})

    @app.post('/v1/datasets', status_code=201)
    async def upload(request: Request, filename: str, sheet: str | None = None, missing_values: str = '[""]'):
        # Raw streaming avoids buffering/parsing arbitrary multipart bodies.
        if Path(filename).suffix.lower() not in ('.csv', '.xlsx'):
            raise HTTPException(422, 'SUPPORTED_FORMATS_CSV_XLSX')
        try:
            markers = json.loads(missing_values)
            if not isinstance(markers, list) or len(markers) > 16 or any(not isinstance(v, str) or len(v) > 32 for v in markers):
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(422, 'INVALID_MISSING_MARKERS')
        reserve()
        try:
            content = bytearray()
            async for chunk in request.stream():
                if len(content) + len(chunk) > MAX_BYTES:
                    raise HTTPException(413, 'FILE_TOO_LARGE')
                content.extend(chunk)
        except BaseException:
            app.state.pending -= 1
            raise
        return await submit(store.ingest, bytes(content), filename, sheet, markers, reserved=True)

    @app.get('/v1/datasets/{dataset_id}')
    async def profile(dataset_id: str, x_dataset_token: str = Header(default='')):
        try:
            return store.public(store.read(dataset_id, x_dataset_token))
        except KeyError:
            raise HTTPException(404, 'DATASET_NOT_FOUND')

    @app.post('/v1/datasets/{dataset_id}/query')
    async def query(dataset_id: str, body: Query, x_dataset_token: str = Header(default='')):
        return await submit(store.query, dataset_id, x_dataset_token, body.version, body.question,
                            body.conversation_id, body.revision)

    @app.get('/v1/datasets/{dataset_id}/package')
    async def download_package(dataset_id: str, x_dataset_token: str = Header(default='')):
        def archive():
            record = store.read(dataset_id, x_dataset_token)
            if not record.get('package_version'):
                raise ValueError('PACKAGE_NOT_BUILT_YET')
            from g85.package_builder import ROOT
            from g85.runtime.package_loader import read_package
            folder = store.root / dataset_id / 'package'
            if sha((folder / 'manifest.json').read_bytes()) != record['package_version']:
                raise ValueError('PACKAGE_CHECKSUM_MISMATCH')
            package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
            package.table.close()
            manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
                for name in ['manifest.json', *manifest['files']]:
                    z.write(folder / name, name)
            return buffer.getvalue()
        content = await submit(archive)
        return Response(content, media_type='application/zip', headers={
            'Content-Disposition': 'attachment; filename="g85-package.zip"', 'Cache-Control': 'no-store'})

    @app.post('/v1/datasets/{dataset_id}/conversations', status_code=201)
    async def start_conversation(dataset_id: str, body: Version, x_dataset_token: str = Header(default='')):
        return await submit(store.create_conversation, dataset_id, x_dataset_token, body.version)

    @app.get('/v1/datasets/{dataset_id}/conversations/{conversation_id}')
    async def get_conversation(dataset_id: str, conversation_id: str, x_dataset_token: str = Header(default='')):
        def load():
            record = store.read(dataset_id, x_dataset_token)
            return conversation.read(store.root / dataset_id, conversation_id, record['version'])
        return await submit(load)

    return app


app = create_app()
