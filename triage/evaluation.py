async def evaluate_cases(cases, classify):
    if not cases:
        raise ValueError("The evaluation set is empty.")
    from triage.core import Email

    results = []
    confusion = {}
    for case in cases:
        suggestion = await classify(Email(**case["email"]))
        expected = case["expected_label"]
        key = f"{expected} -> {suggestion.label}"
        confusion[key] = confusion.get(key, 0) + 1
        results.append({
            "id": case["email"]["id"], "expected": expected, "actual": suggestion.label,
            "correct": suggestion.label == expected,
            "review_expected": case.get("expected_review", False),
            "review_actual": suggestion.needs_review,
        })
    return {
        "count": len(results),
        "label_accuracy": sum(r["correct"] for r in results) / len(results),
        "review_flag_accuracy": sum(r["review_expected"] == r["review_actual"] for r in results) / len(results),
        "confusion": confusion, "results": results,
        "note": "Small synthetic development set; these results do not establish production accuracy.",
    }
