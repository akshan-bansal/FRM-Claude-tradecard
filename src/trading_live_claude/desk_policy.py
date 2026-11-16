"""Desk venue policy: which instrument class trades on which broker.

The desk runs two brokers (decided 2026-11-16, when Questrade was retired):

  * **Crypto** ("currencies") on Kraken.
  * **Everything else** on Interactive Brokers: North American equities (US, TSX, TSX-V), overseas
    listings (LSE, ASX, Tokyo, Hong Kong), and futures, options and other derivatives.

``brokers/ib_access.py`` picks the IB transport per region. ``ib_futures_only`` in trading.yaml can still
narrow an IB book to futures. FX is still not sourced from IB: a basket spanning more than one currency is
refused, so a session runs one book per currency (a GBP book for LSE, a JPY book for Tokyo + OSE futures, …)
rather than one commingled book normalized through spot FX.
"""
from __future__ import annotations

from .venues import currency_of


class VenuePolicyError(ValueError):
    """An instrument was routed to a broker the desk policy does not allow for its class."""


def is_future(symbol: str) -> bool:
    return symbol.startswith("/")


def is_crypto(symbol: str) -> bool:
    return "/" in symbol and not symbol.startswith("/")


def is_equity(symbol: str) -> bool:
    return not is_future(symbol) and not is_crypto(symbol)


def assert_ib_futures_only(symbols: list[str]) -> None:
    """With ``ib_futures_only`` set, refuse any equity handed to the IB book."""
    equities = sorted(s for s in symbols if is_equity(s))
    if equities:
        raise VenuePolicyError(
            f"ib_futures_only narrows this IB book to futures, so these equities are refused: "
            f"{', '.join(equities)}. Run them in an IB book without ib_futures_only."
        )


def assert_no_crypto_on_ib(symbols: list[str]) -> None:
    """Crypto trades on Kraken only; IB carries equities and derivatives."""
    crypto = sorted(s for s in symbols if is_crypto(s))
    if crypto:
        raise VenuePolicyError(f"Crypto trades on Kraken, not IB (desk venue split): {', '.join(crypto)}.")


def assert_kraken_crypto_only(symbols: list[str]) -> None:
    """Kraken carries crypto only; equities and derivatives trade on IB."""
    other = sorted(s for s in symbols if not is_crypto(s))
    if other:
        raise VenuePolicyError(f"Kraken carries crypto only; trade these on IB: {', '.join(other)}.")


def require_explicit_book_sizing(numeraire: str, paper_equity: float | None,
                                 min_ticket: float | None) -> None:
    """A non-CAD/USD book must state its equity and min ticket in its own currency.

    The defaults (100,000 equity, 100 min ticket) are CAD/USD-sized. Read as yen they would be a
    ~US$650 book with a ~US$0.65 min ticket, which silently disables the min-ticket gate. Fail
    closed instead of guessing an FX rate.
    """
    if numeraire.upper() in ("CAD", "USD"):
        return
    missing = [flag for flag, v in (("--paper-equity", paper_equity), ("--min-ticket", min_ticket))
               if v is None]
    if missing:
        raise VenuePolicyError(
            f"A {numeraire.upper()} book needs {' and '.join(missing)} set explicitly in "
            f"{numeraire.upper()}; the defaults are CAD/USD-sized."
        )


def assert_single_currency(symbols: list[str], numeraire: str) -> None:
    """Refuse a basket that would need FX to normalize — FX is no longer sourced from IB.

    A single-currency book needs no conversion. A mixed-currency basket used to be triangulated
    through IB spot pairs; with FX pulled off IB there is no rate source, so run one book per
    currency (``--account-currency`` matching the contracts) instead of commingling them.
    """
    numeraire = numeraire.upper()
    foreign = sorted({currency_of(s) for s in symbols} - {numeraire})
    if foreign:
        raise VenuePolicyError(
            f"Basket spans {', '.join([numeraire, *foreign])}; FX conversion was pulled off IB, so "
            f"there is no rate source to normalize into {numeraire}. Run one book per currency: set "
            f"--account-currency to the contracts' currency (e.g. USD for CME micros), or drop the "
            f"non-{numeraire} names."
        )
