"""Public information scope of the current five-column administrative dataset.

Legacy wire/schema fields stay readable for saved conversations and older stages.
They are not offered or retrieved in the current G6 release.
"""
CURRENT_RELEASE = "company-tthc-9c38dde8-v1-adjudicated-fees-v2"
DATASET_FIELDS = frozenset({
    "required_documents", "receiving_authority", "fees",
    "processing_times", "submission_methods",
})
PAUSED_FIELDS = frozenset({"applicant_scope", "legal_bases", "steps"})
FIELD_CHOICES = (
    "Bạn muốn hỏi về hồ sơ, nơi nộp, lệ phí, thời gian giải quyết, hình thức nộp "
    "hay muốn xem tất cả thông tin về thủ tục này?"
)


def serving_fields(fields, corpus_version):
    if corpus_version == CURRENT_RELEASE:
        return [field for field in fields if field in DATASET_FIELDS]
    return list(fields)
