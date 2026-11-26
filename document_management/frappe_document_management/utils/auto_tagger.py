import re

import frappe


MAX_REGEX_PATTERN_LENGTH = 500
MAX_REGEX_TEXT_LENGTH = 200_000


def apply_auto_tags(doc, extracted_text):
    if not extracted_text:
        return False

    text_lower = extracted_text.lower()

    tags = frappe.get_all(
        "Document Tag",
        filters={"is_active": 1},
        fields=["name", "matching_algorithm", "match_pattern"],
        limit_page_length=0,
    )

    existing_tags = {t.tag for t in doc.get("tags", [])}
    tags_added = False
    regex_text = (
        extracted_text[:MAX_REGEX_TEXT_LENGTH]
        if len(extracted_text) > MAX_REGEX_TEXT_LENGTH
        else extracted_text
    )

    for tag in tags:
        if tag.name in existing_tags:
            continue

        algorithm = tag.matching_algorithm
        pattern = tag.match_pattern

        if algorithm == "None" or not algorithm:
            continue

        is_match = False

        if algorithm == "Auto (Machine Learning)":
            continue

        if not pattern:
            continue

        if algorithm == "Exact match":
            is_match = pattern.lower() in text_lower

        elif algorithm == "Any word":
            words = pattern.lower().split()
            is_match = any(word in text_lower for word in words)

        elif algorithm == "All words":
            words = pattern.lower().split()
            is_match = all(word in text_lower for word in words)

        elif algorithm == "Regular expression":
            if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
                frappe.log_error(
                    title="Auto Tagger Regex Rejected",
                    message=f"Tag: {tag.name}\nPattern exceeds maximum length of {MAX_REGEX_PATTERN_LENGTH}.",
                )
                continue
            try:
                if re.search(pattern, regex_text, re.IGNORECASE):
                    is_match = True
            except Exception as exc:
                frappe.log_error(
                    title="Auto Tagger Regex Error",
                    message=f"Tag: {tag.name}\nPattern: {pattern}\nError: {exc}",
                )

        if is_match:
            doc.append("tags", {"tag": tag.name})
            existing_tags.add(tag.name)
            tags_added = True

    return tags_added
