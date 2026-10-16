"""Read-only design/profile and administrative-release checks; no serving side effects."""
import argparse
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate_profile(profile):
    schema = read_json(ROOT / "schema/domain-profile.schema.json")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(profile)
    ids = [a["id"] for a in profile["attributes"]]
    if len(ids) != len(set(ids)):
        raise ValueError("DUPLICATE_ATTRIBUTE_ID")
    if profile["retrieval"]["exact_lookup"] and not ids:
        raise ValueError("EXACT_LOOKUP_REQUIRES_ATTRIBUTE_SCHEMA")
    return profile


def contained_file(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("RELEASE_FILE_MISSING_OR_OUTSIDE_ROOT: " + relative)
    return path


def jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()]


def unique_index(rows, key):
    index = {row[key]: row for row in rows}
    if len(index) != len(rows):
        raise ValueError("DUPLICATE_ID: " + key)
    return index


def audit_release(root):
    """Check transport integrity and references, NOT legal correctness or serving approval."""
    root = Path(root).resolve()
    manifest = read_json(contained_file(root, "release-manifest.json"))
    artifacts = unique_index(manifest["artifacts"], "path")
    required = {"source_manifest.jsonl", "normalized/procedures.jsonl",
                "normalized/fragments.jsonl", "field-decisions.jsonl",
                "quarantine/conflicts.jsonl", "review-closure.jsonl"}
    if not required.issubset(artifacts):
        raise ValueError("REQUIRED_REVIEW_ARTIFACT_MISSING")
    for name, artifact in artifacts.items():
        path = contained_file(root, name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
            raise ValueError("CHECKSUM_MISMATCH: " + name)
        if "record_count" in artifact and len(jsonl(path)) != artifact["record_count"]:
            raise ValueError("RECORD_COUNT_MISMATCH: " + name)
    sources = unique_index(jsonl(root / "source_manifest.jsonl"), "source_id")
    records = unique_index(jsonl(root / "normalized/procedures.jsonl"), "procedure_id")
    fragments = unique_index(jsonl(root / "normalized/fragments.jsonl"), "fragment_id")
    decisions = jsonl(root / "field-decisions.jsonl")
    versions = {row["dataset_version"] for row in
                [*sources.values(), *records.values(), *fragments.values(), *decisions]}
    if len(versions) != 1:
        raise ValueError("MIXED_OR_EMPTY_DATASET_VERSION")
    for fragment in fragments.values():
        if fragment["source_id"] not in sources or fragment["procedure_id"] not in records:
            raise ValueError("DANGLING_FRAGMENT_REFERENCE")
        if hashlib.sha256(fragment["text"].encode("utf-8")).hexdigest() != fragment["content_sha256"]:
            raise ValueError("FRAGMENT_TEXT_CHECKSUM_MISMATCH")
    for record in records.values():
        if not set(record["source_ids"]).issubset(sources):
            raise ValueError("DANGLING_RECORD_SOURCE")
        for field_fragments in record["field_evidence"].values():
            if any(fid not in fragments or fragments[fid]["procedure_id"] != record["procedure_id"]
                   for fid in field_fragments):
                raise ValueError("RECORD_EVIDENCE_BINDING_MISMATCH")
    seen = set()
    for decision in decisions:
        key = (decision["procedure_id"], decision["field"])
        if key in seen:
            raise ValueError("DUPLICATE_FIELD_DECISION")
        seen.add(key)
        if key[0] not in records or not set(decision["source_ids"]).issubset(sources):
            raise ValueError("DANGLING_DECISION_REFERENCE")
        if any(fid not in fragments or fragments[fid]["procedure_id"] != key[0]
               for fid in decision["fragment_ids"]):
            raise ValueError("DECISION_EVIDENCE_BINDING_MISMATCH")
    return {
        "integrity": "PASS", "serving_approval": "NOT_EVALUATED",
        "dataset_version": next(iter(versions)), "sources": len(sources),
        "records": len(records), "fragments": len(fragments),
        "field_decisions": len(decisions),
        "conflicts": len(jsonl(root / "quarantine/conflicts.jsonl")),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path)
    args = parser.parse_args()
    profiles = [validate_profile(read_json(p))["id"] for p in sorted((ROOT / "profiles").glob("*.json"))]
    result = {"profile_schema": "PASS", "profiles": profiles,
              "validation_scope": "profile schema; optional source release integrity",
              "semantic_accuracy_evaluated": False, "runtime_readiness_evaluated": False}
    if args.release:
        result["administrative_release"] = audit_release(args.release)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
