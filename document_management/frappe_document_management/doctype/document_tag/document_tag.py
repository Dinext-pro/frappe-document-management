# Copyright (c) 2026, ernestoruiz89 and contributors
# For license information, please see license.txt

import re

import frappe
from frappe.model.document import Document

MAX_REGEX_PATTERN_LENGTH = 500


class DocumentTag(Document):
    def validate(self):
        if self.matching_algorithm == "Regular expression":
            pattern = (self.match_pattern or "").strip()
            if not pattern:
                frappe.throw("Match Pattern is required for Regular expression matching.")
            if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
                frappe.throw(
                    f"Regular expression pattern cannot exceed {MAX_REGEX_PATTERN_LENGTH} characters."
                )
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                frappe.throw(f"Invalid regular expression pattern: {exc}")
