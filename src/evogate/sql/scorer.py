"""Status handling around the unmodified official BIRD EX scorer."""

from evogate._vendor.bird_ex import calculate_ex

from .contracts import Evaluation, Execution

SCORER = {
    "adapter": "bird-mini-dev.calculate_ex",
    "upstream_commit": "abd11b6db92a1c9f809b32f7564c7c71b34d67f0",
    "upstream_url": "https://github.com/bird-bench/mini_dev/blob/abd11b6db92a1c9f809b32f7564c7c71b34d67f0/evaluation/evaluation_ex.py",
    "upstream_sha256": "da1bbcd4530be83692d7c650c814ea9704bb710d0c953eb75d02ccb38233cf89",
    "function_sha256": "4a777188f2924dd194dd84d84dc85a5b9ec5631de0c8514dddb7990347528b92",
}


def evaluate_result(prediction: Execution, reference: Execution) -> Evaluation:
    if reference.status != "success":
        return Evaluation(reason="reference_" + reference.status)
    if prediction.status in {"rejected", "sql_error", "timeout"}:
        return Evaluation(score=0, reason=prediction.status)
    if prediction.status == "resource_limit" and prediction.detail in {
        "row_limit", "result_bytes_limit", "sqlite_value_limit",
    }:
        return Evaluation(score=0, reason=prediction.detail)
    if prediction.status != "success":
        return Evaluation(reason=prediction.status)
    return Evaluation(score=calculate_ex(prediction.rows, reference.rows), reason="bird_ex")
