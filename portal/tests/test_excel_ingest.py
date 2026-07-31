"""Excel/CSV template ingest, including the CRS AddressFix and name columns."""
from __future__ import annotations

import csv
import io
from decimal import Decimal

from portal.excel_ingest import parse_excel_upload

BASE_HEADER = ["HolderName", "ResidenceCountry", "Address", "AccountNumber", "Currency"]
BASE_ROW = ["Amina Yusuf", "GB", "10 High Street, London", "ACC000001", "GBP"]


def _csv(header: list[str], *rows: list[str]) -> bytes:
    """Build a CSV upload. Values are quoted properly, so cells may contain
    commas — as a real address column routinely does."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def _parse(header: list[str], *rows: list[str]):
    return parse_excel_upload(_csv(header, *rows), "template.csv")


class TestRequiredColumns:
    def test_minimal_template_parses(self):
        result = _parse(BASE_HEADER, BASE_ROW)
        assert result.ok, result.errors
        assert len(result.records) == 1
        assert result.records[0].holder_name == "Amina Yusuf"

    def test_missing_required_column_is_reported(self):
        result = _parse(["HolderName", "ResidenceCountry"], ["Amina", "GB"])
        assert not result.ok
        assert "Missing required column(s)" in result.errors[0]

    def test_header_matching_ignores_case_and_spacing(self):
        result = _parse(
            ["holder name", "RESIDENCE_COUNTRY", "Address", "account number", "Currency"],
            BASE_ROW,
        )
        assert result.ok, result.errors


class TestNameColumns:
    def test_first_and_last_name_are_read(self):
        result = _parse(
            BASE_HEADER + ["FirstName", "LastName"], BASE_ROW + ["Amina", "Yusuf"]
        )
        assert result.ok, result.errors
        record = result.records[0]
        assert record.holder_first_name == "Amina"
        assert record.holder_last_name == "Yusuf"

    def test_name_columns_are_optional(self):
        result = _parse(BASE_HEADER, BASE_ROW)
        assert result.ok
        assert result.records[0].holder_last_name == ""


class TestAddressFixColumns:
    def test_address_components_are_read(self):
        result = _parse(
            BASE_HEADER + ["Street", "City", "PostCode", "CountrySubentity"],
            BASE_ROW + ["10 High Street", "London", "EC1A 1BB", "Greater London"],
        )
        assert result.ok, result.errors
        record = result.records[0]
        assert record.holder_street == "10 High Street"
        assert record.holder_city == "London"
        assert record.holder_post_code == "EC1A 1BB"
        assert record.holder_country_subentity == "Greater London"


class TestAccountAttributes:
    def test_closed_and_dormant_flags(self):
        result = _parse(
            BASE_HEADER + ["ClosedAccount", "DormantAccount"], BASE_ROW + ["TRUE", "no"]
        )
        assert result.ok, result.errors
        assert result.records[0].closed_account is True
        assert result.records[0].dormant_account is False

    def test_invalid_flag_is_reported(self):
        result = _parse(BASE_HEADER + ["ClosedAccount"], BASE_ROW + ["maybe"])
        assert not result.ok
        assert any("ClosedAccount must be TRUE or FALSE" in error for error in result.errors)

    def test_acct_number_type_enumeration(self):
        result = _parse(BASE_HEADER + ["AcctNumberType"], BASE_ROW + ["OECD601"])
        assert result.ok, result.errors
        assert result.records[0].acct_number_type == "OECD601"

    def test_invalid_acct_number_type_is_reported(self):
        result = _parse(BASE_HEADER + ["AcctNumberType"], BASE_ROW + ["IBAN"])
        assert not result.ok
        assert any("AcctNumberType must be one of" in error for error in result.errors)


class TestBirthInfoColumns:
    def test_birth_place_columns(self):
        result = _parse(
            BASE_HEADER + ["BirthDate", "BirthCity", "BirthCountry"],
            BASE_ROW + ["1980-05-04", "Kano", "ng"],
        )
        assert result.ok, result.errors
        record = result.records[0]
        assert str(record.birth_date) == "1980-05-04"
        assert record.birth_city == "Kano"
        assert record.birth_country_code == "NG"

    def test_country_info_is_a_choice_not_both(self):
        result = _parse(
            BASE_HEADER + ["BirthCountry", "BirthFormerCountryName"],
            BASE_ROW + ["RU", "USSR"],
        )
        assert not result.ok
        assert any("not both" in error for error in result.errors)

    def test_invalid_birth_country_length(self):
        result = _parse(BASE_HEADER + ["BirthCountry"], BASE_ROW + ["NGA"])
        assert not result.ok
        assert any("BirthCountry must be a 2-letter ISO code" in e for e in result.errors)


class TestControllingPersonColumns:
    def test_cp_name_derived_from_parts(self):
        result = _parse(
            BASE_HEADER + ["HolderType", "AcctHolderType", "CPFirstName", "CPLastName", "CPResidence"],
            ["Acme Ltd", "GB", "10 High St", "ACC1", "GBP", "ORGANISATION", "CRS101", "John", "Owner", "GB"],
        )
        assert result.ok, result.errors
        record = result.records[0]
        assert record.cp_name == "John Owner"
        assert record.cp_first_name == "John"
        assert record.cp_last_name == "Owner"

    def test_cp_requires_residence(self):
        result = _parse(
            BASE_HEADER + ["HolderType", "AcctHolderType", "CPName"],
            ["Acme Ltd", "GB", "10 High St", "ACC1", "GBP", "ORGANISATION", "CRS101", "John Owner"],
        )
        assert not result.ok
        assert any("CPResidence is required" in error for error in result.errors)

    def test_cp_only_allowed_for_passive_nfe(self):
        result = _parse(
            BASE_HEADER + ["CPName", "CPResidence"], BASE_ROW + ["John Owner", "GB"]
        )
        assert not result.ok
        assert any("Passive NFE" in error for error in result.errors)


class TestAmounts:
    def test_thousands_separators_accepted(self):
        result = _parse(BASE_HEADER + ["Balance"], BASE_ROW + ["1,250,000.50"])
        assert result.ok, result.errors
        assert result.records[0].balance == Decimal("1250000.50")

    def test_invalid_amount_is_reported(self):
        result = _parse(BASE_HEADER + ["Balance"], BASE_ROW + ["not-a-number"])
        assert not result.ok
        assert any("is not a valid amount" in error for error in result.errors)
