"""Execution-grounded, bounded SQL skill proposals; no selection or holdout input."""

import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .data import fingerprint
from .patches import (
    DocumentPatch,
    EditBudget,
    PatchError,
    apply_document_patch,
    editable_blocks,
    patch_budget_context,
)
from .skills import FULL_DOCUMENT_PROFILE, parse_document
from .skills import MAX_SKILL_CHARS as MAX_DOCUMENT_CHARS
from .skills import PROFILE as MARKDOWN_PROFILE

PROFILE = "evidence_v2"
MAX_SKILL_CHARS = 4000
POLICY = (
    "Database-local guidance. Apply a rule only when its condition holds. "
    "The current question, explicit evidence and schema take precedence. "
    "Do not add output columns, change bounds or return ties solely to follow a rule."
)


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=r"^r[1-9][0-9]?$", max_length=3)
    when: str = Field(min_length=1, max_length=140)
    instruction: str = Field(min_length=1, max_length=300)
    source_ids: list[str] = Field(min_length=1, max_length=4)


class RuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[Rule] = Field(min_length=1, max_length=6)


@dataclass
class Proposal:
    skill: str
    usage: dict | None
    details: dict


class MarkdownOptimizer:
    """Bootstrap a skill, then evolve it through bounded Markdown edits."""

    def __init__(self, complete, budget=None, bounded=True):
        self.complete = complete
        self.budget = budget if budget is not None else EditBudget.runtime_default()
        self.bounded = bounded

    def propose(self, schema, incumbent, examples, corrections, previous_attempt=None,
                rejected_updates=None):
        editable_document = False
        if incumbent:
            try:
                parse_document(incumbent)
                editable_document = True
            except ValueError:
                pass  # A legacy text skill needs one Markdown migration before local edits.
        mode = "patch" if self.bounded and editable_document else "bootstrap"
        profile = MARKDOWN_PROFILE if self.bounded else FULL_DOCUMENT_PROFILE
        output_instruction = (
            "Return a JSON document patch matching the supplied patch_schema. Copy the "
            "supplied base_sha256 exactly. Propose edits in descending priority order. "
            "Only the first max_edits will be selected. Prefer one focused edit, each "
            "addressing an observed process failure while preserving successful behavior. "
            "Use append, insert_after, replace or delete. For anchored edits use target_id "
            "from editable_blocks and leave target empty. IDs refer to whole paragraphs or "
            "headings in the supplied base document, even when earlier edits shift them. "
            "replace supplies the complete replacement block; insert_after adds a NEW block "
            "after the referenced block, so never repeat its text or continuation. "
            "Edit each block at most once. Legacy target is an exact unique text anchor, "
            "with insert_after inserting after its physical line. Prefer target_id. "
            "Keep frontmatter and marked protected regions unchanged. The sum of removed "
            "and inserted characters must fit effective_budget.character_limit across ALL "
            "selected edits. Replacement costs both the old and new block lengths, even "
            "when some words are unchanged. Budget is a ceiling, not a target. "
            "Include a short reasoning tied to development evidence. Review the rejected "
            "updates and their development outcomes before repeating an edit. "
            "Return edits=[] if the evidence supports keeping the current document."
            if mode == "patch" else
            "Return the complete Markdown file directly, starting with YAML frontmatter "
            "with name (lowercase hyphenated identifier) and description (when to use this "
            "skill). Establish a reusable operational workflow from development evidence. "
            "Preserve supported guidance when migrating legacy text. "
            "Return only the file, with no enclosing code fence or JSON wrapper."
        )
        messages = [{"role": "system", "content": (
            "Evolve the SKILL.md bound to this SQL workspace, preserving the imported "
            "skill's purpose and useful workflow. Its versions are scoped to this database. "
            "Learn from development tool calls, their "
            "results, final SQL, verified corrections and successful counterexamples. "
            "Identify where the process failed and revise how the agent plans, chooses "
            "tools, interprets observations, checks results and recovers from errors. "
            "Preserve useful behavior supported by successful cases. Include database "
            "knowledge, applicability boundaries and concise worked patterns where useful. "
            "Use the previous attempt's development outcomes to improve unsuccessful edits. "
            "Keep hypotheses distinguishable from observed facts. Reference SQL can be wrong. "
            "Experience records persist across attempts. An execution failure supports an "
            "executability diagnosis; successful execution alone does not verify semantics. "
            "A rejected or pending candidate does not invalidate the original failures. "
            "A patch-format or budget error supplies no evidence against its SQL hypothesis. "
            "The supplied document_valid, document_error and gate_feedback are authoritative "
            "final results. Prior patch reasoning is the model's hypothesis and may describe "
            "an earlier failed repair, not the final decision. If document_valid is true and "
            "gate_feedback reports no_gain, the patch was tested without sufficient benefit. "
            "Do not claim it failed its budget. Compare development_pairs to check whether "
            "the proposed change actually corrected the cited failures or caused regressions. "
            "A duplicate_candidate has already been tested. Propose a meaningfully revised "
            "instruction, narrow its applicability, or address a different evidenced failure. "
            "Attend to newly supplied development cases as well as unresolved historical "
            "failures. Repeating the same text will skip evaluation and consume the window. "
            "Return edits=[] when no supported revision is available; later windows can "
            "supply more evidence. Keep using the original experience after no_gain. "
            "Use its improvements and regressions to refine applicability conditions. "
            "Treat unverified failure causes as hypotheses. Keep schema and supplied business "
            "context external; include only scoped, evidence-supported guidance in the skill. "
            "Respect explicit question semantics and evidence. Generalize examples instead "
            "of copying complete benchmark question-answer pairs. "
            "For each proposed correction, identify the development case, the failed step "
            "and when the advice applies. Check successful counterexamples before making "
            "a general rule. Avoid blanket DISTINCT, tie, LIMIT or output-column policies. "
            "Available tools are query_values (up to 12 distinct values) and execute_sql "
            "(read-only SQLite with a bounded result preview). The agent receives the schema "
            "and has at most 6 model calls and 8 tool calls per question. "
            "The skill can guide tool use but cannot add executable tools or access files. "
            f"Keep the whole file within {MAX_DOCUMENT_CHARS} characters. "
            + output_instruction
        )}, {"role": "user", "content": json.dumps({
            "schema": schema, "current_skill_md": incumbent,
            "development_examples": examples, "reviewed_corrections": corrections,
            "previous_attempt": previous_attempt,
            "rejected_updates": rejected_updates or [],
            "update_mode": mode, "base_sha256": fingerprint(incumbent),
            "edit_budget": self.budget.model_dump(),
            "effective_budget": patch_budget_context(incumbent, self.budget)
            if mode == "patch" else None,
            "editable_blocks": editable_blocks(incumbent) if mode == "patch" else [],
            "patch_schema": DocumentPatch.model_json_schema() if mode == "patch" else None,
        }, ensure_ascii=False)}]
        attempts, usages = [], []
        for attempt in range(2):
            message, usage = self.complete(messages)
            content = message.get("content") or ""
            usages.append(usage)
            record = {"response": content, "usage": usage}
            try:
                patch, report = None, None
                skill = content
                if mode == "patch":
                    patch = DocumentPatch.model_validate_json(content)
                    skill, report = apply_document_patch(incumbent, patch, self.budget)
                metadata = parse_document(skill)
            except ValueError as exc:
                record["validation_error"] = str(exc)
                if isinstance(exc, PatchError):
                    record["edit_report"] = exc.reports
                attempts.append(record)
                if attempt == 0:
                    messages.extend([
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": (
                            "Repair the output format, anchors or edit budget using the SAME "
                            "current document and development evidence. Preserve the intended "
                            "local correction and return the required output format. "
                            "For anchor errors use a supplied target_id. For budget errors "
                            "keep only the highest-priority correction and shorten it to fit "
                            "the total character_limit. Do not repeat unchanged failed edits. "
                            + str(exc) + "\nEdit report: "
                            + json.dumps(record.get("edit_report", []), ensure_ascii=False)
                        )},
                    ])
                continue
            attempts.append(record)
            return Proposal(skill, total_usage(usages), {
                "profile": profile, "valid": True, "metadata": metadata, "mode": mode,
                "base_sha256": fingerprint(incumbent), "edit_budget": self.budget.model_dump(),
                "patch": patch.model_dump(exclude_unset=True) if patch else None,
                "edit_report": report,
                "attempts": attempts, "development_count": len(examples),
                "correction_count": len(corrections),
            })
        return Proposal("", total_usage(usages), {
            "profile": profile, "valid": False, "attempts": attempts, "mode": mode,
            "base_sha256": fingerprint(incumbent), "edit_budget": self.budget.model_dump(),
            "reason": "SKILL.md 校验失败：" + attempts[-1]["validation_error"],
        })


def execution_view(execution):
    """Keep shape/count facts and a bounded preview, not an asserted diagnosis."""
    rows = execution.get("rows", [])
    return {
        "status": execution.get("status", "unknown"),
        "detail": str(execution.get("detail", ""))[:400],
        "row_count": execution.get("row_count", len(rows)),
        "column_count": len(rows[0]) if rows else None,
        "first_rows_repr": repr(rows[:5])[:1200],
        "preview_is_partial": execution.get("truncated", False) or len(rows) > 5,
    }


def comparison(prediction, reference, score):
    if reference["status"] != "success" or score is None:
        kind = "unknown"
    elif prediction["status"] != "success":
        kind = "execution_failure"
    elif score == 1:
        kind = "reference_match"
    elif (prediction["column_count"] is not None and reference["column_count"] is not None
          and prediction["column_count"] != reference["column_count"]):
        kind = "output_shape_mismatch"
    else:
        kind = "result_mismatch"
    return {"kind": kind, "row_count_delta": (
        prediction["row_count"] - reference["row_count"]
        if prediction["status"] == reference["status"] == "success" else None
    )}


def render(rules):
    lines = [POLICY]
    for rule in rules:
        lines.append(f"[{rule.id}] When: {rule.when}\nDo: {rule.instruction}")
    skill = "\n\n".join(lines)
    if len(skill) > MAX_SKILL_CHARS:
        raise ValueError("Rendered skill exceeds 4000 characters")
    return skill


def parse_rules(content, allowed_sources, existing_rules):
    text = content.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    parsed = RuleSet.model_validate_json(text)
    ids = [rule.id for rule in parsed.rules]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate rule ids")
    existing = {rule["id"]: Rule.model_validate(rule) for rule in existing_rules}
    candidate = {rule.id: rule for rule in parsed.rules}
    for rule in parsed.rules:
        if len(set(rule.source_ids)) != len(rule.source_ids):
            raise ValueError("Duplicate source ids")
        unchanged = rule.id in existing and rule == existing[rule.id]
        if not unchanged and not set(rule.source_ids) <= allowed_sources:
            raise ValueError("New or changed rules must cite supplied development source ids")
    if existing:
        changes = sum(
            candidate.get(key) != existing.get(key) for key in set(existing) | set(candidate)
        )
        if changes > 3:
            raise ValueError("Change at most 3 rules; retain the other existing rules verbatim")
    return parsed.rules, render(parsed.rules)


def total_usage(usages):
    if not usages or any(not isinstance(item, dict) or any(
        type(item.get(key)) is not int or item[key] < 0
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    ) for item in usages):
        return None
    return {key: sum(item[key] for item in usages)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")}


class EvidenceOptimizer:
    def __init__(self, complete):
        self.complete = complete

    def propose(self, schema, incumbent, examples, corrections, existing_rules=None):
        existing_rules = existing_rules or []
        allowed_sources = {row["task_id"] for row in examples}
        allowed_sources.update(row["source_id"] for row in corrections)
        messages = [{"role": "system", "content": (
            "Update a database-local SQL skill using only the supplied development evidence. "
            "Return ONE JSON object matching the schema below, no prose. Keep <=6 rules. "
            "Each rule needs a specific applicability condition, a concise instruction and "
            "1-4 source_ids from supplied examples/corrections. Do not memorize answers. "
            "Use execution shape/count differences as observations, not semantic proof. "
            "Reference SQL may be wrong; do not overrule explicit question/evidence for EX. "
            "Prefer specific recurring failures and useful database knowledge over generic "
            "SQL advice. Include successful counterexamples when deciding rule scope. "
            "Avoid universal LIMIT 1, case folding or name-column conventions. "
            "When existing structured rules are supplied, retain useful rules VERBATIM "
            "including ids/sources and change at most 3 ids (add/replace/delete combined). "
            "If migrating a legacy skill, preserve its supported guidance in compact rules. "
            "Available target tools: query_values (distinct values only) and execute_sql. "
            "Use execute_sql, not query_values, for cardinality or join-duplication checks. "
            "JSON schema: " + json.dumps(RuleSet.model_json_schema())
        )}, {"role": "user", "content": json.dumps({
            "schema": schema, "current_skill": incumbent, "current_rules": existing_rules,
            "development_examples": examples, "reviewed_corrections": corrections,
        }, ensure_ascii=False)}]
        attempts, usages, last_error = [], [], ""
        for attempt in range(2):
            message, usage = self.complete(messages)
            content = message.get("content") or ""
            usages.append(usage)
            record = {"response": content, "usage": usage}
            try:
                rules, skill = parse_rules(content, allowed_sources, existing_rules)
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)[:1200]
                record["validation_error"] = last_error
                attempts.append(record)
                if attempt == 0:
                    messages.extend([
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": (
                            "Repair this JSON/length/source/edit-count error only; do not add "
                            "new business hypotheses or search for another candidate. "
                            "Return the complete corrected JSON. Validation error: " + last_error
                        )},
                    ])
                continue
            attempts.append(record)
            return Proposal(skill, total_usage(usages), {
                "profile": PROFILE, "valid": True,
                "rules": [rule.model_dump() for rule in rules], "attempts": attempts,
                "development_count": len(examples), "correction_count": len(corrections),
            })
        return Proposal("", total_usage(usages), {
            "profile": PROFILE, "valid": False, "rules": [], "attempts": attempts,
            "reason": "Candidate format validation failed after one repair: " + last_error,
            "development_count": len(examples), "correction_count": len(corrections),
        })
