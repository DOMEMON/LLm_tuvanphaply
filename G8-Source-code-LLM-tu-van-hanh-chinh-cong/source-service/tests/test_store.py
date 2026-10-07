import json

import pytest

from app.store import SourceStoreError, SourceVersionStore


def _corpus(tmp_path):
    corpus = tmp_path / "corpus"
    path = corpus / "normalized/procedures.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "procedure_id": "procedure-alpha",
                "review": {"status": "APPROVED_FOR_DEMO"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return corpus


def _store(tmp_path):
    return SourceVersionStore(
        data_dir=tmp_path / "state",
        corpus_dir=_corpus(tmp_path),
        review_token="review-secret-token",
    )


def test_snapshot_requires_review_before_publish_and_supports_rollback(tmp_path):
    source = _store(tmp_path)
    first = source.stage(
        review_token="review-secret-token",
        procedure_id="procedure-alpha",
        field="fees",
        text="Không thu phí.",
        title="Nguồn phí",
        source_url="https://example.invalid/source",
        effective_from="2026-01-01",
        effective_to=None,
    )
    with pytest.raises(SourceStoreError, match="G5_INVALID_STATE_TRANSITION"):
        source.transition(
            action="PUBLISH",
            snapshot_id=first["snapshot_id"],
            reviewer="Hoan",
            review_token="review-secret-token",
        )
    source.transition(
        action="APPROVE",
        snapshot_id=first["snapshot_id"],
        reviewer="Hoan",
        review_token="review-secret-token",
    )
    source.transition(
        action="PUBLISH",
        snapshot_id=first["snapshot_id"],
        reviewer="Hoan",
        review_token="review-secret-token",
    )
    active = source.get_active(procedure_id="procedure-alpha", field="fees")
    assert active["text"] == "Không thu phí."
    second = source.stage(
        review_token="review-secret-token",
        procedure_id="procedure-alpha",
        field="fees",
        text="Lệ phí 10.000 đồng.",
        title="Nguồn phí mới",
        source_url=None,
        effective_from="2026-02-01",
        effective_to=None,
    )
    for action in ("APPROVE", "PUBLISH"):
        source.transition(
            action=action,
            snapshot_id=second["snapshot_id"],
            reviewer="Hoan",
            review_token="review-secret-token",
        )
    active = source.get_active(procedure_id="procedure-alpha", field="fees")
    assert active["text"] == "Lệ phí 10.000 đồng."
    source.transition(
        action="ROLLBACK",
        snapshot_id=first["snapshot_id"],
        reviewer="Hoan",
        review_token="review-secret-token",
    )
    active = source.get_active(procedure_id="procedure-alpha", field="fees")
    assert active["text"] == "Không thu phí."
    source.transition(
        action="ROLLBACK",
        snapshot_id=first["snapshot_id"],
        reviewer="Hoan",
        review_token="review-secret-token",
    )
    assert source.get_active(procedure_id="procedure-alpha", field="fees") is None


def test_store_rejects_pii_unknown_procedure_and_invalid_token(tmp_path):
    source = _store(tmp_path)
    with pytest.raises(SourceStoreError, match="G5_PROCEDURE_NOT_ALLOWED"):
        source.stage(
            review_token="review-secret-token",
            procedure_id="unknown",
            field="fees",
            text="Không thu phí.",
            title="Nguồn",
            source_url=None,
            effective_from=None,
            effective_to=None,
        )
    with pytest.raises(SourceStoreError, match="G5_SOURCE_PII_REJECTED"):
        source.stage(
            review_token="review-secret-token",
            procedure_id="procedure-alpha",
            field="fees",
            text="Liên hệ test@example.com để biết phí.",
            title="Nguồn",
            source_url=None,
            effective_from=None,
            effective_to=None,
        )
    with pytest.raises(SourceStoreError, match="G5_SOURCE_PII_REJECTED"):
        source.stage(
            review_token="review-secret-token",
            procedure_id="procedure-alpha",
            field="fees",
            text="Không thu phí.",
            title="Liên hệ test@example.com",
            source_url=None,
            effective_from=None,
            effective_to=None,
        )
    with pytest.raises(SourceStoreError, match="G5_EFFECTIVE_DATE_INVALID"):
        source.stage(
            review_token="review-secret-token",
            procedure_id="procedure-alpha",
            field="fees",
            text="Không thu phí.",
            title="Nguồn",
            source_url=None,
            effective_from="not-a-date",
            effective_to=None,
        )
    staged = source.stage(
        review_token="review-secret-token",
        procedure_id="procedure-alpha",
        field="fees",
        text="Không thu phí.",
        title="Nguồn",
        source_url=None,
        effective_from=None,
        effective_to=None,
    )
    with pytest.raises(SourceStoreError, match="G5_REVIEW_TOKEN_INVALID"):
        source.transition(
            action="APPROVE",
            snapshot_id=staged["snapshot_id"],
            reviewer="Hoan",
            review_token="wrong-token",
        )

    with pytest.raises(SourceStoreError, match="G5_REVIEW_TOKEN_INVALID"):
        source.stage(
            review_token="wrong-token",
            procedure_id="procedure-alpha",
            field="fees",
            text="Không thu phí.",
            title="Nguồn",
            source_url=None,
            effective_from=None,
            effective_to=None,
        )
