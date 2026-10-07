"""Import reviewed JSONL/CSV documents with a column mapping; never activate.

python -m tools.ingest --input records.jsonl --profile profile.json
  --mapping mapping.json --version example-v1 --output data/packages/example-v1 --reviewed

Mapping: {"id": "id", "title": "title", "text": "text"}.
Optional mapping key "url" names a source URL column. This baseline importer
preserves each record's text; large documents must be split upstream explicitly.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

from app.rag.g85.package_loader import read_package
from tools.validate import validate_profile

ROOT = Path(__file__).resolve().parents[1]


def ingest(input_path, profile, mapping, version, output, *, reviewed=False):
    if not reviewed:
        raise ValueError('SOURCE_REVIEW_REQUIRED')
    validate_profile(profile)
    if profile['ingestion']['adapter'] != 'documents_v1' or profile['attributes'] or profile['retrieval']['exact_lookup']:
        raise ValueError('DOCUMENT_IMPORT_REQUIRES_EMPTY_ATTRIBUTES_AND_SEARCH_MODE')
    if not version or len(version) > 90:
        raise ValueError('INVALID_DATASET_VERSION')
    input_path, output = Path(input_path), Path(output)
    if output.exists():
        raise ValueError('OUTPUT_ALREADY_EXISTS_CHOOSE_NEW_VERSION')
    payload = input_path.read_bytes()
    source_hash = hashlib.sha256(payload).hexdigest()
    if input_path.suffix.lower() == '.csv':
        rows = list(csv.DictReader(io.StringIO(payload.decode('utf-8-sig'))))
    elif input_path.suffix.lower() == '.jsonl':
        rows = [json.loads(line) for line in payload.decode('utf-8-sig').splitlines() if line.strip()]
    else:
        raise ValueError('SUPPORTED_INPUTS_JSONL_OR_CSV')
    entities, passages, sources, seen = [], [], [], set()
    for i,row in enumerate(rows,1):
        key, title, text = (row[mapping[k]] for k in ('id','title','text'))
        if not all(isinstance(v,str) and v.strip() for v in (key,title,text)) or len(key)>150 or len(text)>12000:
            raise ValueError('INVALID_OR_OVERSIZED_RECORD: ' + str(i))
        if key in seen:
            raise ValueError('DUPLICATE_SOURCE_ID')
        seen.add(key)
        sid = 'source-' + hashlib.sha256(key.encode()).hexdigest()[:24]
        entities.append({'key':key,'title':title,'domains':['collection']})
        sources.append({'id':sid,'title':title,'url':row.get(mapping.get('url')),
                        'content_sha256':source_hash})
        passages.append({'id':sid+'-text','source_id':sid,'entity':key,'attribute':'','title':title,
            'text':text,'content_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),'review_status':'APPROVED',
            'source_locator':{'filename':input_path.name,'record_index':i,'source_sha256':source_hash}})
    if not passages:
        raise ValueError('EMPTY_DATASET')
    values = {'profile.json':profile,'groups.json':{'collection':profile['title']},
              'entities.jsonl':entities,'sources.jsonl':sources,'passages.jsonl':passages}
    files = {name: (''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in value) if name.endswith('.jsonl')
                   else json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8') for name,value in values.items()}
    manifest = {'schema_version':'g85-package-v1','dataset_version':version,'approved_passages':len(passages),
        'source_sha256':source_hash,'review_basis':'operator explicitly marked input reviewed',
        'files':{name:{'sha256':hashlib.sha256(data).hexdigest(),**({'count':len(values[name])} if name.endswith('.jsonl') else {})}
                 for name,data in files.items()}}
    output.mkdir(parents=True)
    for name,data in files.items():
        (output/name).write_bytes(data)
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    package = read_package(output,ROOT/'g85-domain.schema.json')
    return {'package':str(output),'records':len(rows),'version':package.version,'activated':False}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('input','profile','mapping','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--version',required=True)
    parser.add_argument('--reviewed',action='store_true')
    args=parser.parse_args()
    print(json.dumps(ingest(args.input,json.loads(args.profile.read_text(encoding='utf-8')),
        json.loads(args.mapping.read_text(encoding='utf-8')),args.version,args.output,reviewed=args.reviewed)))
