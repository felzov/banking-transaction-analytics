"""Response bodies built from domain values (ADR 0008 money format), without a database."""

from datetime import date

from transaction_analytics.api.schemas import CurrencyTotalsOut, MonthTotalsOut
from transaction_analytics.domain.analytics import CurrencyTotals, MonthTotals
from transaction_analytics.domain.money import Currency

GBP_TOTALS = CurrencyTotals(
    currency=Currency.GBP, transactions=3, inflow_minor=250_000, outflow_minor=-95_320
)
MONEY_FIELDS = {
    "currency": "GBP",
    "transactions": 3,
    "inflow_minor": 250_000,
    "inflow": "2500.00",
    "outflow_minor": -95_320,
    "outflow": "-953.20",
    "net_minor": 154_680,
    "net": "1546.80",
}


def test_currency_totals_out() -> None:
    assert CurrencyTotalsOut.of(GBP_TOTALS).model_dump(mode="json") == MONEY_FIELDS


def test_month_totals_out_is_built_by_its_own_of() -> None:
    # Review fix: MonthTotalsOut inherited CurrencyTotalsOut.of, which type-checked for it but
    # could only fail (a MonthTotals has no currency; the result would lack month).
    month = MonthTotals(month=date(2026, 9, 1), totals=GBP_TOTALS)

    assert MonthTotalsOut.of(month).model_dump(mode="json") == {"month": "2026-09"} | MONEY_FIELDS


def test_month_label_pads_years_below_1000() -> None:
    month = MonthTotals(month=date(99, 1, 1), totals=GBP_TOTALS)

    assert MonthTotalsOut.of(month).month == "0099-01"
