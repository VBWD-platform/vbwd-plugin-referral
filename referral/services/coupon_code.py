"""Referral coupon-code generation + masking (the exact S92 B.1 rules).

Pure helpers (no DB, no I/O) so they unit-test trivially and stay the single
home for the canonical pattern and the privacy mask.
"""
import re
import secrets

#: 8 random bytes → 16 uppercase hex chars (e.g. ``F45E2A5DBB6677FF``).
HEX_BYTES = 8

#: Masking reveals the first 4 / last 2 hex chars and hides the rest. With the
#: ~40 hidden bits the middle cannot be guessed (S92 B.1 / Risk: masking).
_MASK_REVEAL_PREFIX = 4
_MASK_REVEAL_SUFFIX = 2
_MASK_HIDDEN_CHARS = 10
_HEX_TOKEN_LENGTH = HEX_BYTES * 2


def generate_hex_token() -> str:
    """A fresh ``secrets.token_hex(8)`` upper-cased — 16 uppercase hex chars."""
    return secrets.token_hex(HEX_BYTES).upper()


def normalize_prefix(raw_prefix: str) -> str:
    """Normalize a user-supplied ``--coupon <PREFIX>``.

    Upper-cases, strips a trailing ``_``, collapses repeated underscores, and
    drops characters outside ``[A-Z0-9_]`` so the assembled code is clean.
    """
    upper = (raw_prefix or "").upper()
    cleaned = re.sub(r"[^A-Z0-9_]", "", upper)
    collapsed = re.sub(r"_+", "_", cleaned)
    return collapsed.strip("_")


def build_referral_code(prefix: str, nickname: str, hex_token: str) -> str:
    """Assemble ``<PREFIX>_<NICKNAME>_<HEX16>`` from already-clean parts."""
    nickname_upper = re.sub(r"[^A-Z0-9]", "", (nickname or "").upper())
    return f"{prefix}_{nickname_upper}_{hex_token}"


def mask_code(coupon_code: str) -> str:
    """Mask the trailing hex token of a referral code (privacy / anti-harvest).

    ``REF_USER_BOB_F45E2A5DBB6677FF`` → ``REF_USER_BOB_F45ExxxxxxxxxxFF``.
    Only the final ``_<HEX16>`` segment is masked; the prefix + nickname stay
    intact. If the code does not end in a 16-hex segment it is returned
    unchanged (defensive — never throws).
    """
    last_separator = coupon_code.rfind("_")
    if last_separator == -1:
        return coupon_code
    head = coupon_code[:last_separator]
    hex_token = coupon_code[last_separator + 1 :]
    if len(hex_token) != _HEX_TOKEN_LENGTH:
        return coupon_code
    masked_token = (
        hex_token[:_MASK_REVEAL_PREFIX]
        + ("x" * _MASK_HIDDEN_CHARS)
        + hex_token[-_MASK_REVEAL_SUFFIX:]
    )
    return f"{head}_{masked_token}"
