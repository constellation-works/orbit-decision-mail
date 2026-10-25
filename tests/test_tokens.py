"""The rendered token is core's mint, checked against vectors from the documented construction.

The vectors were computed outside Python, with the construction core documents
(`base32(HMAC-SHA256(key, "request_id|task_id|epoch|expires_at_unix"))`, RFC 4648 alphabet,
no padding, first 26 characters):

    printf '%s' 'dr-0123456789abcdef|ORB-42|3|1793448000' \
      | openssl dgst -sha256 -mac HMAC -macopt hexkey:000102…1f -binary | base32 | cut -c1-26
"""

from __future__ import annotations

import pytest

from decision_mail.tokens import TokenError, fingerprint, mint, unix_seconds, well_formed
from fakes import KEY_HEX, core_token, pilot_request

VECTORS = [
    (3, "TZMGKITEFF35TIXW6EFN6IN6DR"),
    (1, "6WRFUGGYWIBIGGSD5ZHIXOKMYN"),
]


@pytest.mark.parametrize(("epoch", "expected"), VECTORS)
@pytest.mark.parametrize(
    "expires_at",
    ["2026-10-31T12:00:00Z", "2026-10-31T12:00:00.250Z", "2026-10-31T12:00:00.999999999+00:00"],
)
def test_mint_matches_the_documented_vector(epoch: int, expected: str, expires_at: str) -> None:
    token = mint(KEY_HEX, "dr-0123456789abcdef", "ORB-42", epoch, expires_at)
    assert token == expected
    assert well_formed(token)


def test_mint_agrees_with_an_independent_implementation() -> None:
    request = pilot_request(expires_at="2026-11-02T08:30:00+01:00", task_id="ORB-7")
    for epoch in (1, 2, 9):
        assert mint(KEY_HEX, request["id"], "ORB-7", epoch, request["expires_at"]) == core_token(
            KEY_HEX, request, epoch
        )


def test_expiry_is_floored_to_whole_seconds() -> None:
    assert unix_seconds("2026-10-31T12:00:00Z") == 1793448000
    assert unix_seconds("2026-10-31T12:00:00.999999999Z") == 1793448000
    assert unix_seconds("2026-10-31T13:00:00+01:00") == 1793448000


@pytest.mark.parametrize("key", ["", "zz", "abc", "0g11"])
def test_a_bad_key_refuses_without_echoing_it(key: str) -> None:
    with pytest.raises(TokenError) as raised:
        mint(key, "dr-0123456789abcdef", "ORB-42", 1, "2026-10-31T12:00:00Z")
    if key:
        assert key not in str(raised.value)


def test_a_bad_expiry_refuses() -> None:
    with pytest.raises(TokenError):
        mint(KEY_HEX, "dr-0123456789abcdef", "ORB-42", 1, "next tuesday")
    with pytest.raises(TokenError):
        unix_seconds("2026-10-31T12:00:00")  # no offset: ambiguous


def test_well_formed_is_the_core_alphabet_and_length() -> None:
    assert well_formed("A" * 26)
    assert not well_formed("A" * 25)
    assert not well_formed("a" * 26)
    assert not well_formed("1" * 26)
    assert fingerprint("A" * 26) == fingerprint("A" * 26)
    assert len(fingerprint("A" * 26)) == 64
