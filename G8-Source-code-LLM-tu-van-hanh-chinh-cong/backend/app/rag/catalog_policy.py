"""Project-owner catalog adjudications that preserve immutable source rows."""

PREFERRED_PROCEDURES = {
    # Workbook row 26 is a heading-only duplicate; row 28 is canonical.
    "d2_title_70d8a7c53c19": "d2_title_c98b4aa39036",
    # Workbook row 17 is the more complete cremation-support record.
    "d2_title_1df9a1a807f0": "d2_title_10ed2a9ea4a7",
}
# Verified against the public DVC search/detail identity on 2026-09-23/24.
# Local workbook labels remain unchanged; only the outbound lookup title uses
# this explicit mapping. No fuzzy fallback or expanded website allowlist.
CURRENT_LOOKUP_NAMES = {
    "d2_title_011f005278ef": "Đăng ký nội quy lao động của doanh nghiệp",
    # Public search identity checked 2026-09-25 (1.004873), not the overseas variant.
    "d2_title_ba78b5d39212": "Thủ tục cấp Giấy xác nhận tình trạng hôn nhân",
    # The local title has a "Thủ tục" prefix, while the live portal publishes
    # separate scholarship eligibility variants under this shared heading.
    "d2_title_ff5c71001a91": "Xét, cấp học bổng chính sách",
}
# Original conflict scope, extended below only for missing receiving locations.
# Approved cells still use the locked dataset.
MCP_REVIEW_FIELDS = {
    # Owner-authorized 2026-09-26: unresolved land fee / "theo quy định".
    "d2_title_c343c85df1af": frozenset({"fees"}),
    # Fieldwise-adjudicated records whose time field remains unresolved.
    "d2_title_7677bda9d6bd": frozenset({"processing_times"}),
    "d2_title_f29de859e25e": frozenset({"processing_times"}),
    # Row 42's document cell describes tuition waiver, not the named scholarship.
    "d2_title_ff5c71001a91": frozenset({"required_documents"}),
}

# Owner expanded lookup scope on 2026-09-25: these nine canonical records
# lack receiving_authority in the locked release. Only this field is added.
MISSING_RECEIVING_PROCEDURES = frozenset({
    "d2_title_011f005278ef", "d2_title_30228658fce3", "d2_title_55245834a3c0",
    "d2_title_61e34e3360a8", "d2_title_69915286e651", "d2_title_ba78b5d39212",
    "d2_title_c343c85df1af", "d2_title_eb5f9df122cd", "d2_title_f8c56de9462a",
})


def preferred_procedure_id(procedure_id: str | None) -> str | None:
    """Map a retained source record to its serving-time canonical procedure."""

    return PREFERRED_PROCEDURES.get(procedure_id, procedure_id)


def is_serving_candidate(procedure_id: str) -> bool:
    """Keep shadow records auditable while excluding them from routing choices."""

    return procedure_id not in PREFERRED_PROCEDURES


def mcp_review_fields(procedure_id: str | None) -> frozenset[str]:
    """Return owner-authorized conflict/missing-location lookup fields."""

    canonical = preferred_procedure_id(procedure_id)
    fields = MCP_REVIEW_FIELDS.get(canonical, frozenset())
    if canonical in MISSING_RECEIVING_PROCEDURES:
        fields = fields | {"receiving_authority"}
    return fields


def needs_realtime_offer(
    procedure_id: str | None,
    fields: set[str],
    *,
    local_missing: bool = False,
) -> bool:
    """Check the allowlist; the caller must also verify missing local locations."""

    return bool(fields & mcp_review_fields(procedure_id))
