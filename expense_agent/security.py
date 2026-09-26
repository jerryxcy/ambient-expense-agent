"""Security controls for scrubbing PII and defending against prompt injection."""

from __future__ import annotations

import re

# ------------------------------------------------------------------------------
# PII Detection & Redaction Patterns
# ------------------------------------------------------------------------------

# SSN regex: standard 3-2-4 digit format with optional dashes/spaces
SSN_REGEX = re.compile(
    r"\b(?!000|666)\d{3}[- ]?(?!00)\d{2}[- ]?(?!0000)\d{4}\b"
)

# Credit Card regex: matches 13 to 19 digit card numbers (Visa, Mastercard, Amex, Discover)
# formatted with spaces, dashes, or contiguous digits.
CREDIT_CARD_REGEX = re.compile(
    r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|3(?:0[0-5]|[68][0-9])[0-9]{11}|6(?:011|5[0-9]{2})[0-9]{12})\b|"
    r"\b(?:\d{4}[ -]){3}\d{4}\b|"
    r"\b3[47]\d{2}[ -]\d{6}[ -]\d{5}\b"
)


def luhn_checksum_valid(card_num_str: str) -> bool:
    """Validates credit card number digits using the Luhn algorithm."""
    digits = [int(c) for c in card_num_str if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    reverse_digits = digits[::-1]
    for i, d in enumerate(reverse_digits):
        if i % 2 == 1:
            doubled = d * 2
            checksum += doubled - 9 if doubled > 9 else doubled
        else:
            checksum += d
    return checksum % 10 == 0


def scrub_pii(text: str) -> tuple[str, list[str]]:
    """Scrubs SSNs and credit card numbers from text.

    Returns:
        tuple[str, list[str]]: The sanitized text and a list of redacted category names.
    """
    redacted_categories: list[str] = []
    sanitized = text

    # 1. Scrub SSNs
    if SSN_REGEX.search(sanitized):
        sanitized = SSN_REGEX.sub("[REDACTED_SSN]", sanitized)
        if "SSN" not in redacted_categories:
            redacted_categories.append("SSN")

    # 2. Scrub Credit Cards
    matches = list(CREDIT_CARD_REGEX.finditer(sanitized))
    if matches:
        for match in matches:
            matched_text = match.group(0)
            digits_only = re.sub(r"\D", "", matched_text)
            # If formatted as card blocks or passes Luhn algorithm, scrub it
            if "-" in matched_text or " " in matched_text or luhn_checksum_valid(digits_only):
                sanitized = sanitized.replace(matched_text, "[REDACTED_CREDIT_CARD]")
                if "CREDIT_CARD" not in redacted_categories:
                    redacted_categories.append("CREDIT_CARD")

    return sanitized, redacted_categories


# ------------------------------------------------------------------------------
# Prompt Injection Detection
# ------------------------------------------------------------------------------

# Patterns aimed at hijacking system instructions, forcing approval, or bypassing rules
PROMPT_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "instruction_override",
        re.compile(
            r"(ignore|disregard|forget|override|bypass)\s+(all\s+)?(previous|prior|system|above|the)\s+(instructions|rules|prompts|guidelines)",
            re.IGNORECASE,
        ),
    ),
    (
        "forced_approval",
        re.compile(
            r"(auto[- ]?approve|must\s+approve|immediately\s+approve|force\s+approval|always\s+approve|mark\s+as\s+approved|you\s+must\s+say\s+approved)",
            re.IGNORECASE,
        ),
    ),
    (
        "rule_bypass",
        re.compile(
            r"(bypass|skip|ignore|circumvent)\s+(the\s+)?(review|approval|policy|rules|threshold|checks)",
            re.IGNORECASE,
        ),
    ),
    (
        "system_role_impersonation",
        re.compile(
            r"(\[system\]|<system>|role:\s*system|system\s+prompt|developer\s+mode|admin\s+override|dan\s+mode)",
            re.IGNORECASE,
        ),
    ),
    (
        "json_injection_attempt",
        re.compile(
            r'(\"status\"\s*:\s*\"APPROVED\"|\"recommended_action\"\s*:\s*\"APPROVE\"|\"risk_level\"\s*:\s*\"LOW\")',
            re.IGNORECASE,
        ),
    ),
]


def detect_prompt_injection(text: str) -> tuple[bool, list[str]]:
    """Detects adversarial instructions attempting to force approval or bypass review policies.

    Returns:
        tuple[bool, list[str]]: True if prompt injection is suspected, and the list of matched pattern names.
    """
    matched: list[str] = []
    for name, pattern in PROMPT_INJECTION_PATTERNS:
        if pattern.search(text):
            matched.append(name)

    is_injection = len(matched) > 0
    return is_injection, matched
