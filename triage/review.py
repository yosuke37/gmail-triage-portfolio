"""Pure filtering/sorting for the local review list."""
def filter_reviews(rows, *, statuses, source="All", label="All", search="", flagged=False, order="Newest first"):
    query = search.strip().casefold()
    result = [r for r in rows if
              r["status"] in statuses
              and (source == "All" or r["source"] == source)
              and (label == "All" or r["label"] == label)
              and (not flagged or r["needs_review"])
              and (not query or query in " ".join(str(r.get(k, "")) for k in ("subject", "sender", "reason", "account", "id")).casefold())]
    if order == "Needs attention first":
        return sorted(result, key=lambda r: (r["status"] != "pending", not r["needs_review"], -r["id"]))
    return sorted(result, key=lambda r: r["id"], reverse=order != "Oldest first")
