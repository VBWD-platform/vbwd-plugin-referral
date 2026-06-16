"""Unit tests for the referral coupon-code generation + masking rules (B0/B1)."""
import re

from plugins.referral.referral.services.coupon_code import (
    build_referral_code,
    generate_hex_token,
    mask_code,
    normalize_prefix,
)

_CODE_PATTERN = re.compile(r"^[A-Z0-9_]+_[A-Z0-9]+_[0-9A-F]{16}$")


def test_hex_token_is_16_uppercase_hex_chars():
    token = generate_hex_token()
    assert len(token) == 16
    assert re.fullmatch(r"[0-9A-F]{16}", token)


def test_normalize_prefix_uppercases_and_collapses():
    assert normalize_prefix("ref_user_") == "REF_USER"
    assert normalize_prefix("REF__USER___") == "REF_USER"
    assert normalize_prefix("ref user!") == "REFUSER"


def test_build_referral_code_matches_canonical_pattern():
    code = build_referral_code("REF_USER", "BOB", "F45E2A5DBB6677FF")
    assert code == "REF_USER_BOB_F45E2A5DBB6677FF"
    assert _CODE_PATTERN.match(code)


def test_mask_code_hides_middle_of_hex_token():
    full = "REF_USER_BOB_F45E2A5DBB6677FF"
    assert mask_code(full) == "REF_USER_BOB_F45ExxxxxxxxxxFF"


def test_mask_code_leaves_non_hex_suffix_untouched():
    # Defensive: never throws on an unexpected shape.
    assert mask_code("PLAIN") == "PLAIN"
