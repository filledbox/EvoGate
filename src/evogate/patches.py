"""Bounded Markdown edits inspired by SkillOpt's patch, clip and scheduler modules.

Reference commit e2edfa9721ce41c98e50fa129da87cf1f53c2f7f. This implementation
uses exact, unique anchors and atomic rejection instead of upstream fallbacks.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .data import fingerprint
from .skills import parse_document


class EditBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_edits: int = Field(default=3, ge=1, strict=True)
    max_changed_chars: int = Field(default=4000, ge=1, strict=True)
    max_change_ratio: float = Field(default=0.3, gt=0, le=1)
    # Missing in saved v1 protocols, which must retain their original ratio-only limit.
    min_char_budget: int = Field(default=0, ge=0, strict=True)

    @classmethod
    def runtime_default(cls):
        return cls(min_char_budget=1200)


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["append", "insert_after", "replace", "delete"]
    target: str = ""
    target_id: str = ""
    content: str = ""


class DocumentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reasoning: str = ""
    edits: list[Edit]


PROTECTED_MARKERS = (
    ("<!-- SLOW_UPDATE_START -->", "<!-- SLOW_UPDATE_END -->"),
    ("<!-- APPENDIX_START -->", "<!-- APPENDIX_END -->"),
)


def protected_ranges(text):
    """Protect frontmatter and any SkillOpt-style slow-update/appendix blocks."""
    lines = text.splitlines(keepends=True)
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    ranges = [(0, sum(len(line) for line in lines[:end + 1]))]
    for start_marker, end_marker in PROTECTED_MARKERS:
        start, end = text.find(start_marker), text.find(end_marker)
        if start == end == -1:
            continue
        if (start < 0 or end < start or text.count(start_marker) != 1
                or text.count(end_marker) != 1):
            raise ValueError("Protected region markers must form one complete ordered pair")
        ranges.append((start, end + len(end_marker)))
    return sorted(ranges)


def patch_budget_context(text, budget):
    body_chars = len(text) - protected_ranges(text)[0][1]
    return {
        "body_chars": body_chars,
        "character_limit": min(budget.max_changed_chars, max(
            budget.min_char_budget, int(body_chars * budget.max_change_ratio),
        )),
        "cost_rule": "removed characters + inserted characters, including separators",
    }


def editable_blocks(text):
    """Give base-document paragraphs IDs so models need not reproduce wrapped anchors."""
    ranges = protected_ranges(text)
    blocks = []
    for match in re.finditer(r"(?:[^\r\n]*\S[^\r\n]*(?:\r?\n|$))+", text):
        content = match.group().rstrip("\r\n")
        start, end = match.start(), match.start() + len(content)
        if not any(start < right and end > left for left, right in ranges):
            blocks.append({"id": f"b{len(blocks) + 1}", "text": content,
                           "start": start, "end": end})
    return blocks


class PatchError(ValueError):
    def __init__(self, message, reports):
        super().__init__(message)
        self.reports = [{**report, "status": "rolled_back"}
                        if report["status"] == "applied" else report for report in reports]


def apply_document_patch(text, raw, budget):
    """Select priority-ordered edits and apply all selected edits or reject all."""
    parse_document(text)
    patch = DocumentPatch.model_validate(raw)
    if patch.base_sha256 != fingerprint(text):
        raise ValueError("Patch base_sha256 does not match the current SKILL.md")
    char_limit = patch_budget_context(text, budget)["character_limit"]
    block_ranges = {block["id"]: (block["start"], block["end"])
                    for block in editable_blocks(text)}
    reports = []
    candidate, changed_chars = text, 0
    for index, edit in enumerate(patch.edits, 1):
        report = {"index": index, "op": edit.op, "status": "pending"}
        reports.append(report)
        if index > budget.max_edits:
            report["status"] = "not_selected"
            continue
        try:
            ranges = protected_ranges(candidate)
            if any(marker in edit.content for pair in PROTECTED_MARKERS for marker in pair):
                raise ValueError("Edits cannot add protected region markers")
            newline = "\r\n" if "\r\n" in candidate else "\n"
            if edit.op == "append":
                if edit.target or edit.target_id or not edit.content.strip():
                    raise ValueError("append requires content and an empty target")
                start = ranges[1][0] if len(ranges) > 1 else len(candidate)
                end = start
                replacement = newline + newline + edit.content + newline
            else:
                if edit.target_id:
                    if edit.target:
                        raise ValueError("Use target_id or target, not both")
                    span = block_ranges.get(edit.target_id)
                    if span is None:
                        raise ValueError(
                            "Unknown or already modified target_id; use a supplied block ID")
                    start, end = span
                else:
                    start = candidate.find(edit.target) if edit.target else -1
                    if start < 0:
                        raise ValueError(
                            "Edit target must match existing text exactly; use target_id")
                    if candidate.find(edit.target, start + 1) >= 0:
                        raise ValueError("Edit target is ambiguous; include more surrounding text")
                    end = start + len(edit.target)
                if any(start < right and end > left for left, right in ranges):
                    raise ValueError("Edit overlaps protected frontmatter or a protected region")
                if edit.op == "delete":
                    if edit.content:
                        raise ValueError("delete requires empty content")
                    replacement = ""
                else:
                    if not edit.content.strip():
                        raise ValueError("replace and insert_after require nonempty content")
                    replacement = edit.content
                    if edit.op == "insert_after":
                        line_end = candidate.find("\n", end)
                        start = end = line_end + 1 if line_end >= 0 else len(candidate)
                        if any(left < start < right for left, right in ranges):
                            raise ValueError("Insertion falls inside a protected region")
                        replacement = newline + edit.content + newline
            cost = end - start + len(replacement)
            if candidate[start:end] == replacement:
                raise ValueError("Edit has no effect; return an empty edits list to keep the skill")
            if changed_chars + cost > char_limit:
                raise ValueError(
                    f"Edit {index}: selected edits exceed changed-character budget {char_limit}; "
                    f"used={changed_chars}, edit_cost={cost}, total={changed_chars + cost}. "
                    "Shorten content or remove lower-priority edits."
                )
            for block_id, span in list(block_ranges.items()):
                left, right = span
                if (start < right and end > left) or left < start < right:
                    del block_ranges[block_id]
                elif left >= end:
                    shift = len(replacement) - (end - start)
                    block_ranges[block_id] = (left + shift, right + shift)
            candidate = candidate[:start] + replacement + candidate[end:]
            changed_chars += cost
            report.update({"status": "applied", "changed_chars": cost})
        except ValueError as exc:
            report.update({"status": "rejected", "reason": str(exc)})
            raise PatchError(str(exc), reports) from exc
    try:
        parse_document(candidate)
    except ValueError as exc:
        raise PatchError(str(exc), reports) from exc
    return candidate, {
        "edits": reports, "selected_indices": list(range(min(len(patch.edits), budget.max_edits))),
        "changed_chars": changed_chars, "character_limit": char_limit,
    }


def verify_document_update(incumbent, candidate, details, budget):
    """Recheck a generated or cached update before saving its candidate artifact."""
    metadata = parse_document(candidate)
    try:
        parse_document(incumbent)
    except ValueError:
        if details.get("mode") != "bootstrap":
            raise ValueError("An empty or legacy skill requires Markdown bootstrap") from None
        return metadata
    if details.get("mode") != "patch" or details.get("patch") is None:
        raise ValueError("An existing SKILL.md requires a bounded document patch")
    replayed, _ = apply_document_patch(incumbent, details["patch"], budget)
    if replayed != candidate:
        raise ValueError("Candidate SKILL.md differs from the applied document patch")
    return metadata


def add_edit_arguments(parser):
    parser.add_argument("--max-edits", type=int, default=3, help="每轮局部编辑数量预算")
    parser.add_argument("--max-edit-chars", type=int, default=4000, help="每轮增删字符总量上限")
    parser.add_argument("--max-edit-ratio", type=float, default=0.3,
                        help="每轮增删字符预算占原正文的比例，短文档可使用额度下限")
    parser.add_argument("--min-edit-chars", type=int, default=1200,
                        help="短文档编辑额度下限，仍受 max-edit-chars 限制；0 恢复旧比例预算")


def budget_from_arguments(args):
    return EditBudget(max_edits=args.max_edits, max_changed_chars=args.max_edit_chars,
                      max_change_ratio=args.max_edit_ratio, min_char_budget=args.min_edit_chars)
