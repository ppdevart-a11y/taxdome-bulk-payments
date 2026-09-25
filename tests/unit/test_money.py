import pytest

from bulk_payments.money import format_cents, parse_amount


@pytest.mark.parametrize(
    ("amount", "cents"),
    [
        ("6250", 625_000),
        ("5800.5", 580_050),
        ("1200.75", 120_075),
        ("0.01", 1),
        ("0.1", 10),
        ("1.0", 100),
        ("1.00", 100),
        ("007.5", 750),
        ("999999999999999.99", 99_999_999_999_999_999),
    ],
)
def test_parses_valid_amounts_exactly(amount: str, cents: int) -> None:
    assert parse_amount(amount) == cents


@pytest.mark.parametrize(
    "amount",
    [
        "",
        "0",
        "0.00",
        "-5",
        "+5",
        "1.",
        ".5",
        "1.234",
        "1e3",
        "1E3",
        " 5",
        "5 ",
        "5\n",
        "1,000",
        "1_000",
        "0x10",
        "NaN",
        "Infinity",
        "\u0661\u0662\u0663",  # Arabic-Indic digits: int() accepts them, we must not
        "\uff11\uff12",  # full-width digits
        "1000000000000000",  # 16 integer digits
    ],
)
def test_rejects_invalid_amounts(amount: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - message is covered below
        parse_amount(amount)


def test_zero_has_a_specific_message() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        parse_amount("0.00")


@pytest.mark.parametrize(
    ("cents", "formatted"),
    [
        (0, "0.00"),
        (5, "0.05"),
        (100, "1.00"),
        (580_050, "5800.50"),
        (1_325_125, "13251.25"),
        (-1_325_125, "-13251.25"),
    ],
)
def test_formats_cents(cents: int, formatted: str) -> None:
    assert format_cents(cents) == formatted


def test_round_trip() -> None:
    for cents in (1, 99, 100, 12_345, 3_674_875):
        assert parse_amount(format_cents(cents)) == cents
