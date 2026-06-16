"""Token-rate source for percent-of-sale commission (D-Commission).

Converts a money amount (in the system default currency) into tokens at the
configured token-bundle rate, defined as ``token_amount / price`` (tokens per
one currency unit). We use the **best-value active bundle** — the one giving the
most tokens per currency unit — so a percent-of-sale commission is credited at
the most generous published rate. With no active bundle the rate is zero
(commission falls to zero rather than guessing a rate).
"""
from decimal import Decimal


def tokens_per_currency_unit(token_bundle_repository) -> Decimal:
    """Return tokens-per-one-currency-unit from the best active token bundle.

    Returns ``Decimal("0")`` when no priced active bundle exists.
    """
    best_rate = Decimal("0")
    for bundle in token_bundle_repository.find_active():
        price = Decimal(str(bundle.price or 0))
        if price <= 0:
            continue
        rate = Decimal(str(bundle.token_amount)) / price
        if rate > best_rate:
            best_rate = rate
    return best_rate
