import json

import pytest

from evogate.data import fingerprint
from evogate.optimizer import MarkdownOptimizer
from evogate.patches import (
    EditBudget,
    PatchError,
    apply_document_patch,
    editable_blocks,
    patch_budget_context,
    verify_document_update,
)
from evogate.skills import FULL_DOCUMENT_PROFILE

DOCUMENT = """---
name: query-workflow
description: Plan and verify queries on this database.
---

# Query workflow

## Plan

Identify the requested business entity, output columns and filters.
For order counts after joining line items, count physical rows.
For item revenue, sum amounts without removing repeated item rows.

## Inspect

Guess uncertain stored codes from their names.
Use execute_sql to check the proposed query before the final answer.
Keep the evidence's numerator and denominator populations explicit.

## Legacy advice

This sentence is obsolete.
"""


def patch(*edits, base=DOCUMENT):
    return {"base_sha256": fingerprint(base), "reasoning": "Correct observed tool behavior",
            "edits": list(edits)}


def test_sequential_document_edits_preserve_unmodified_sections():
    operations = patch(
        {"op": "replace", "target": "count physical rows", "content": "count unique orders"},
        {"op": "insert_after", "target": "## Inspect", "content": "Inspect stored values first."},
        {"op": "delete", "target": "This sentence is obsolete.\n"},
        {"op": "append", "content": "## Recovery\nRepair queries using observed errors."},
    )
    result, report = apply_document_patch(
        DOCUMENT, operations, EditBudget(max_edits=4, max_change_ratio=1),
    )
    assert "count unique orders" in result
    assert "## Inspect\n\nInspect stored values first.\n" in result
    assert "obsolete" not in result and result.endswith("using observed errors.\n")
    assert result.split("# Query workflow")[0] == DOCUMENT.split("# Query workflow")[0]
    assert "For item revenue, sum amounts without removing repeated item rows." in result
    assert all(row["status"] == "applied" for row in report["edits"])


def test_priority_selection_keeps_top_budget_and_reports_dropped_edits():
    result, report = apply_document_patch(DOCUMENT, patch(
        {"op": "replace", "target": "count physical rows", "content": "count unique orders"},
        {"op": "replace", "target": "Guess uncertain stored codes from their names.",
         "content": "Inspect uncertain stored codes with query_values."},
        {"op": "append", "content": "x" * 10000},
    ), EditBudget(max_edits=2, max_change_ratio=1))
    assert "count unique orders" in result and "query_values" in result
    assert report["selected_indices"] == [0, 1]
    assert report["edits"][2]["status"] == "not_selected"


def test_stale_base_is_rejected():
    with pytest.raises(ValueError, match="base_sha256"):
        apply_document_patch(DOCUMENT, patch(base="older version"), EditBudget())


@pytest.mark.parametrize("target", ["missing text", "the"])
def test_invalid_anchor_rolls_back_preceding_edits(target):
    original = DOCUMENT
    with pytest.raises(PatchError) as failure:
        apply_document_patch(DOCUMENT, patch(
            {"op": "replace", "target": "count physical rows", "content": "count unique orders"},
            {"op": "delete", "target": target},
        ), EditBudget())
    assert failure.value.reports[0]["status"] == "rolled_back"
    assert failure.value.reports[1]["status"] == "rejected"
    assert DOCUMENT == original


@pytest.mark.parametrize("target", [
    "description: Plan and verify queries on this database.",
    "database.\n---\n\n# Query workflow",
])
def test_frontmatter_and_cross_boundary_edits_are_protected(target):
    with pytest.raises(PatchError, match="protected"):
        apply_document_patch(DOCUMENT, patch(
            {"op": "replace", "target": target, "content": "Changed"},
        ), EditBudget())


def test_protected_blocks_are_preserved_and_append_goes_before_them():
    protected = "<!-- SLOW_UPDATE_START -->\nKeep this block.\n<!-- SLOW_UPDATE_END -->\n"
    document = DOCUMENT + protected
    result, _ = apply_document_patch(document, patch(
        {"op": "append", "content": "Additional local guidance."}, base=document,
    ), EditBudget())
    assert result.endswith(protected)
    assert result.index("Additional local guidance.") < result.index("<!-- SLOW_UPDATE_START -->")
    with pytest.raises(PatchError, match="protected"):
        apply_document_patch(document, patch(
            {"op": "delete", "target": "This sentence is obsolete.\n" + protected},
            base=document,
        ), EditBudget(max_change_ratio=1))


def test_insert_after_cannot_fall_back_to_append():
    with pytest.raises(PatchError, match="match existing"):
        apply_document_patch(DOCUMENT, patch(
            {"op": "insert_after", "target": "Unknown heading", "content": "New advice"},
        ), EditBudget())


@pytest.mark.parametrize("budget", [
    EditBudget(max_changed_chars=10), EditBudget(max_change_ratio=0.01),
])
def test_edit_character_budgets_are_enforced(budget):
    with pytest.raises(PatchError, match="changed-character budget"):
        apply_document_patch(DOCUMENT, patch(
            {"op": "replace", "target": "count physical rows", "content": "count unique orders"},
        ), budget)


def test_whole_body_replacement_cannot_hide_inside_one_operation():
    body = DOCUMENT[DOCUMENT.index("# Query workflow"):]
    with pytest.raises(PatchError, match="changed-character budget"):
        apply_document_patch(DOCUMENT, patch(
            {"op": "replace", "target": body, "content": "# Entirely new workflow\nNew steps."},
        ), EditBudget())


def test_empty_patch_keeps_exact_document_bytes():
    document = DOCUMENT.replace("\n", "\r\n")
    result, report = apply_document_patch(document, patch(base=document), EditBudget())
    assert result == document and report["changed_chars"] == 0


def test_existing_document_requires_patch_output_and_repairs_once():
    attempts, replies = [], iter([DOCUMENT, json.dumps(patch(
        {"op": "replace", "target": "count physical rows", "content": "count unique orders"},
    ))])

    def complete(messages):
        attempts.append(json.loads(json.dumps(messages)))
        return {"content": next(replies)}, None

    result = MarkdownOptimizer(complete).propose("schema", DOCUMENT, [], [])
    assert result.details["valid"] and result.details["mode"] == "patch"
    assert "count unique orders" in result.skill
    assert len(result.details["attempts"]) == 2
    context = json.loads(attempts[0][1]["content"])
    assert context["base_sha256"] == fingerprint(DOCUMENT)
    assert context["edit_budget"]["max_edits"] == 3
    assert result.details["edit_report"]["edits"][0]["status"] == "applied"


def test_repeated_full_rewrite_is_rejected_without_a_candidate():
    result = MarkdownOptimizer(lambda messages: ({"content": DOCUMENT}, None)).propose(
        "schema", DOCUMENT, [], [],
    )
    assert not result.details["valid"] and result.skill == ""


def test_legacy_text_gets_one_markdown_bootstrap():
    result = MarkdownOptimizer(lambda messages: ({"content": DOCUMENT}, None)).propose(
        "schema", "Old plain text guidance", [], [],
    )
    assert result.details["valid"] and result.details["mode"] == "bootstrap"


def test_old_markdown_profile_retains_full_document_generation():
    result = MarkdownOptimizer(
        lambda messages: ({"content": DOCUMENT}, None), bounded=False,
    ).propose("schema", DOCUMENT, [], [])
    assert result.details["valid"] and result.details["profile"] == FULL_DOCUMENT_PROFILE


def test_short_document_budget_floor_respects_cap_and_legacy_protocol():
    budget = EditBudget.runtime_default()
    assert patch_budget_context(DOCUMENT, budget)["character_limit"] == 1200
    budget.max_changed_chars = 100
    assert patch_budget_context(DOCUMENT, budget)["character_limit"] == 100
    legacy = EditBudget.model_validate({"max_change_ratio": 0.3})
    assert legacy.min_char_budget == 0
    assert patch_budget_context(DOCUMENT, legacy)["character_limit"] < 1200


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_block_insertion_preserves_wrapped_paragraph_and_replays(newline):
    document = DOCUMENT.replace(
        "Identify the requested business entity, output columns and filters.",
        "Identify the requested business entity,\noutput columns and filters.",
    )
    document = document.replace("\n", newline)
    block = next(b for b in editable_blocks(document) if b["text"].startswith("Identify"))
    raw = patch({"op": "insert_after", "target_id": block["id"],
                 "content": "Check fact-table grain."}, base=document)
    result, _ = apply_document_patch(document, raw, EditBudget.runtime_default())
    assert block["text"] + newline + newline + "Check fact-table grain." in result
    assert result.count(block["text"]) == 1
    verify_document_update(document, result, {"mode": "patch", "patch": raw},
                           EditBudget.runtime_default())


def test_block_ids_disambiguate_duplicates_and_survive_prior_offset_changes():
    document = DOCUMENT + "\nRepeated advice.\n\nRepeated advice.\n"
    blocks = editable_blocks(document)
    repeated = [b for b in blocks if b["text"] == "Repeated advice."]
    result, _ = apply_document_patch(document, patch(
        {"op": "insert_after", "target_id": blocks[0]["id"], "content": "New paragraph."},
        {"op": "replace", "target_id": repeated[0]["id"], "content": "Scoped advice."},
        {"op": "delete", "target_id": repeated[1]["id"]}, base=document,
    ), EditBudget.runtime_default())
    assert "New paragraph." in result and "Scoped advice." in result
    assert "Repeated advice." not in result


def test_block_ids_exclude_protected_regions_and_reject_overlapping_edits():
    document = DOCUMENT + "\n<!-- APPENDIX_START -->\nPrivate block.\n<!-- APPENDIX_END -->\n"
    blocks = editable_blocks(document)
    assert all("Private" not in b["text"] and "description:" not in b["text"] for b in blocks)
    with pytest.raises(PatchError, match="already modified"):
        apply_document_patch(document, patch(
            {"op": "replace", "target_id": blocks[0]["id"], "content": "# Revised workflow"},
            {"op": "delete", "target_id": blocks[0]["id"]}, base=document,
        ), EditBudget.runtime_default())


def test_optimizer_receives_computed_limit_block_catalog_and_actionable_repair():
    requests = []

    def complete(messages):
        requests.append(json.loads(json.dumps(messages)))
        context = json.loads(messages[1]["content"])
        edits = [{"op": "append", "content": "x" * 1300}] if len(requests) == 1 else [
            {"op": "insert_after", "target_id": context["editable_blocks"][-1]["id"],
             "content": "Check output grain."},
        ]
        return {"content": json.dumps(patch(*edits))}, None

    proposal = MarkdownOptimizer(complete).propose("schema", DOCUMENT, [], [])
    context = json.loads(requests[0][1]["content"])
    assert context["effective_budget"]["character_limit"] == 1200
    assert proposal.details["valid"] and len(requests) == 2
    repair = requests[1][-1]["content"]
    assert "edit_cost=1303" in repair and "total=1303" in repair
    assert "highest-priority" in repair and "target_id" in repair
