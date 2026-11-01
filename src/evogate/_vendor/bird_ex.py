"""BIRD Mini-Dev calculate_ex, copied verbatim; see NOTICE.md."""


def calculate_ex(predicted_res, ground_truth_res):
    res = 0
    if set(predicted_res) == set(ground_truth_res):
        res = 1
    return res
