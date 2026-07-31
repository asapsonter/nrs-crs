"""Central configuration for regulatory constants used across the platform.

All deadline, partner jurisdiction, and risk-rating values live here so the
demo can be retuned without touching application logic.
"""
import hashlib
from decimal import Decimal

# Reporting year presented throughout the demo.
CURRENT_REPORTING_YEAR: int = 2025

# Domestic filing deadline: RFIs must file with the NRS by 31 May of the year
# following the reporting year.
DOMESTIC_DEADLINE_MONTH: int = 5
DOMESTIC_DEADLINE_DAY: int = 31

# International exchange deadline: the NRS must exchange with partner
# jurisdictions by 30 September of the year following the reporting year.
EXCHANGE_DEADLINE_MONTH: int = 9
EXCHANGE_DEADLINE_DAY: int = 30

# Sending jurisdiction code for MessageRefID construction.
SENDING_JURISDICTION: str = "NG"

# Competent-authority identity used in the outbound MessageSpec. This is the
# NRS as transmitting competent authority, distinct from each RFI's own
# Sending Company IN carried at ReportingFI level.
NRS_SENDING_COMPANY_IN: str = "NRS-NG-CA"
NRS_CONTACT: str = "Automatic Exchange of Information Unit, Nigeria Revenue Service"

# Activated CRS MCAA exchange relationships seeded into the demo: the
# CRS-participating jurisdictions (the United States is absent by design —
# it exchanges under FATCA, not the CRS). Activation dates are demo values;
# the displayed public-key fingerprint is derived deterministically per
# jurisdiction so the list stays maintainable.
_PARTNERS: list[tuple[str, str, str]] = [
    # Europe
    ("AD", "Andorra", "2020-01-01"),
    ("AT", "Austria", "2019-04-01"),
    ("BE", "Belgium", "2019-04-01"),
    ("BG", "Bulgaria", "2019-09-01"),
    ("HR", "Croatia", "2019-09-01"),
    ("CY", "Cyprus", "2019-04-01"),
    ("CZ", "Czechia", "2019-09-01"),
    ("DK", "Denmark", "2019-04-01"),
    ("EE", "Estonia", "2019-09-01"),
    ("FI", "Finland", "2019-04-01"),
    ("FR", "France", "2019-04-01"),
    ("DE", "Germany", "2019-09-01"),
    ("GI", "Gibraltar", "2020-01-01"),
    ("GR", "Greece", "2019-09-01"),
    ("GG", "Guernsey", "2019-09-01"),
    ("HU", "Hungary", "2019-09-01"),
    ("IS", "Iceland", "2020-01-01"),
    ("IE", "Ireland", "2019-04-01"),
    ("IM", "Isle of Man", "2019-09-01"),
    ("IT", "Italy", "2019-04-01"),
    ("JE", "Jersey", "2019-09-01"),
    ("LV", "Latvia", "2019-09-01"),
    ("LI", "Liechtenstein", "2020-01-01"),
    ("LT", "Lithuania", "2019-09-01"),
    ("LU", "Luxembourg", "2019-04-01"),
    ("MT", "Malta", "2019-04-01"),
    ("MC", "Monaco", "2020-01-01"),
    ("NL", "Netherlands", "2019-04-01"),
    ("NO", "Norway", "2019-04-01"),
    ("PL", "Poland", "2019-09-01"),
    ("PT", "Portugal", "2019-04-01"),
    ("RO", "Romania", "2019-09-01"),
    ("SM", "San Marino", "2020-01-01"),
    ("SK", "Slovakia", "2019-09-01"),
    ("SI", "Slovenia", "2019-09-01"),
    ("ES", "Spain", "2019-04-01"),
    ("SE", "Sweden", "2019-04-01"),
    ("CH", "Switzerland", "2020-01-01"),
    ("TR", "Türkiye", "2021-06-01"),
    ("GB", "United Kingdom", "2019-04-01"),
    # Americas and Caribbean
    ("AI", "Anguilla", "2020-06-01"),
    ("AG", "Antigua and Barbuda", "2020-06-01"),
    ("AR", "Argentina", "2019-09-01"),
    ("AW", "Aruba", "2020-06-01"),
    ("BS", "Bahamas", "2020-06-01"),
    ("BB", "Barbados", "2020-06-01"),
    ("BZ", "Belize", "2020-06-01"),
    ("BM", "Bermuda", "2020-01-01"),
    ("BR", "Brazil", "2019-09-01"),
    ("VG", "British Virgin Islands", "2020-01-01"),
    ("CA", "Canada", "2020-01-01"),
    ("KY", "Cayman Islands", "2020-01-01"),
    ("CL", "Chile", "2020-06-01"),
    ("CO", "Colombia", "2019-09-01"),
    ("CR", "Costa Rica", "2020-06-01"),
    ("CW", "Curaçao", "2020-06-01"),
    ("GD", "Grenada", "2020-06-01"),
    ("MX", "Mexico", "2019-09-01"),
    ("PA", "Panama", "2020-06-01"),
    ("KN", "Saint Kitts and Nevis", "2020-06-01"),
    ("LC", "Saint Lucia", "2020-06-01"),
    ("VC", "Saint Vincent and the Grenadines", "2020-06-01"),
    ("TC", "Turks and Caicos Islands", "2020-06-01"),
    ("UY", "Uruguay", "2019-09-01"),
    # Middle East and Asia-Pacific
    ("AU", "Australia", "2019-09-01"),
    ("AZ", "Azerbaijan", "2020-06-01"),
    ("BH", "Bahrain", "2020-06-01"),
    ("BN", "Brunei Darussalam", "2021-01-01"),
    ("CN", "China", "2019-09-01"),
    ("CK", "Cook Islands", "2020-06-01"),
    ("HK", "Hong Kong (China)", "2019-09-01"),
    ("IN", "India", "2019-09-01"),
    ("ID", "Indonesia", "2019-09-01"),
    ("IL", "Israel", "2020-01-01"),
    ("JP", "Japan", "2019-09-01"),
    ("KZ", "Kazakhstan", "2021-01-01"),
    ("KR", "Korea", "2019-09-01"),
    ("KW", "Kuwait", "2020-06-01"),
    ("LB", "Lebanon", "2020-06-01"),
    ("MO", "Macau (China)", "2020-06-01"),
    ("MY", "Malaysia", "2019-09-01"),
    ("MV", "Maldives", "2021-01-01"),
    ("MH", "Marshall Islands", "2020-06-01"),
    ("NZ", "New Zealand", "2019-09-01"),
    ("PK", "Pakistan", "2020-01-01"),
    ("QA", "Qatar", "2020-06-01"),
    ("WS", "Samoa", "2020-06-01"),
    ("SA", "Saudi Arabia", "2020-01-01"),
    ("SG", "Singapore", "2019-09-01"),
    ("TH", "Thailand", "2023-09-01"),
    ("AE", "United Arab Emirates", "2020-06-01"),
    ("VU", "Vanuatu", "2020-06-01"),
    # Africa
    ("GH", "Ghana", "2019-09-01"),
    ("KE", "Kenya", "2022-09-01"),
    ("LR", "Liberia", "2021-01-01"),
    ("MU", "Mauritius", "2021-01-01"),
    ("SC", "Seychelles", "2020-01-01"),
    ("ZA", "South Africa", "2019-04-01"),
]


def _fingerprint(code: str) -> str:
    """Deterministic demo public-key fingerprint for a partner jurisdiction."""
    digest = hashlib.md5(f"NRS-CRS-{code}".encode()).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, 32, 2))


# (code, name, activated_since, public key fingerprint shown in the UI)
PARTNER_JURISDICTIONS: list[tuple[str, str, str, str]] = [
    (code, name, since, _fingerprint(code)) for code, name, since in _PARTNERS
]

# Type of party enrolling on the portal.
ENROLMENT_TYPES: list[tuple[str, str]] = [
    ("FINANCIAL_ENTITY", "Financial Entities"),
    ("STAKEHOLDER", "Financial Entity Stakeholders"),
    ("INDIVIDUAL", "Individuals"),
]

# FI categories offered on the enrolment form: the four classes of Financial
# Institution under the Common Reporting Standard.
FI_CATEGORIES: list[tuple[str, str]] = [
    ("INVESTMENT_ENTITY", "Investment Entities"),
    ("DEPOSITORY_INSTITUTION", "Depository Institutions"),
    ("CUSTODIAL_INSTITUTION", "Custodial Institutions"),
    ("SPECIFIED_INSURANCE", "Specified Insurance Companies"),
]

# Telephone country dial codes offered on the enrolment form. Nigeria first,
# then the activated partner jurisdictions.
DIAL_CODES: list[tuple[str, str]] = [
    ("+234", "NG +234"),
    ("+44", "GB +44"),
    ("+1", "CA/US +1"),
    ("+33", "FR +33"),
    ("+49", "DE +49"),
    ("+971", "AE +971"),
    ("+27", "ZA +27"),
    ("+91", "IN +91"),
    ("+41", "CH +41"),
    ("+230", "MU +230"),
]

# Account currencies offered on the filing record form. No default is applied;
# the preparer must select the currency of each reported account. The naira
# and the majors lead the list; the rest of the active ISO 4217 currencies
# follow alphabetically.
CURRENCIES: list[tuple[str, str]] = [
    ("NGN", "NGN - Nigerian Naira"),
    ("USD", "USD - US Dollar"),
    ("EUR", "EUR - Euro"),
    ("GBP", "GBP - Pound Sterling"),
    ("AED", "AED - UAE Dirham"),
    ("AFN", "AFN - Afghan Afghani"),
    ("ALL", "ALL - Albanian Lek"),
    ("AMD", "AMD - Armenian Dram"),
    ("ANG", "ANG - Netherlands Antillean Guilder"),
    ("AOA", "AOA - Angolan Kwanza"),
    ("ARS", "ARS - Argentine Peso"),
    ("AUD", "AUD - Australian Dollar"),
    ("AWG", "AWG - Aruban Florin"),
    ("AZN", "AZN - Azerbaijani Manat"),
    ("BAM", "BAM - Bosnia and Herzegovina Convertible Mark"),
    ("BBD", "BBD - Barbadian Dollar"),
    ("BDT", "BDT - Bangladeshi Taka"),
    ("BGN", "BGN - Bulgarian Lev"),
    ("BHD", "BHD - Bahraini Dinar"),
    ("BIF", "BIF - Burundian Franc"),
    ("BMD", "BMD - Bermudian Dollar"),
    ("BND", "BND - Brunei Dollar"),
    ("BOB", "BOB - Bolivian Boliviano"),
    ("BRL", "BRL - Brazilian Real"),
    ("BSD", "BSD - Bahamian Dollar"),
    ("BTN", "BTN - Bhutanese Ngultrum"),
    ("BWP", "BWP - Botswana Pula"),
    ("BYN", "BYN - Belarusian Ruble"),
    ("BZD", "BZD - Belize Dollar"),
    ("CAD", "CAD - Canadian Dollar"),
    ("CDF", "CDF - Congolese Franc"),
    ("CHF", "CHF - Swiss Franc"),
    ("CLP", "CLP - Chilean Peso"),
    ("CNY", "CNY - Chinese Yuan"),
    ("COP", "COP - Colombian Peso"),
    ("CRC", "CRC - Costa Rican Colón"),
    ("CUP", "CUP - Cuban Peso"),
    ("CVE", "CVE - Cape Verdean Escudo"),
    ("CZK", "CZK - Czech Koruna"),
    ("DJF", "DJF - Djiboutian Franc"),
    ("DKK", "DKK - Danish Krone"),
    ("DOP", "DOP - Dominican Peso"),
    ("DZD", "DZD - Algerian Dinar"),
    ("EGP", "EGP - Egyptian Pound"),
    ("ERN", "ERN - Eritrean Nakfa"),
    ("ETB", "ETB - Ethiopian Birr"),
    ("FJD", "FJD - Fijian Dollar"),
    ("FKP", "FKP - Falkland Islands Pound"),
    ("GEL", "GEL - Georgian Lari"),
    ("GHS", "GHS - Ghanaian Cedi"),
    ("GIP", "GIP - Gibraltar Pound"),
    ("GMD", "GMD - Gambian Dalasi"),
    ("GNF", "GNF - Guinean Franc"),
    ("GTQ", "GTQ - Guatemalan Quetzal"),
    ("GYD", "GYD - Guyanese Dollar"),
    ("HKD", "HKD - Hong Kong Dollar"),
    ("HNL", "HNL - Honduran Lempira"),
    ("HTG", "HTG - Haitian Gourde"),
    ("HUF", "HUF - Hungarian Forint"),
    ("IDR", "IDR - Indonesian Rupiah"),
    ("ILS", "ILS - Israeli New Shekel"),
    ("INR", "INR - Indian Rupee"),
    ("IQD", "IQD - Iraqi Dinar"),
    ("IRR", "IRR - Iranian Rial"),
    ("ISK", "ISK - Icelandic Króna"),
    ("JMD", "JMD - Jamaican Dollar"),
    ("JOD", "JOD - Jordanian Dinar"),
    ("JPY", "JPY - Japanese Yen"),
    ("KES", "KES - Kenyan Shilling"),
    ("KGS", "KGS - Kyrgyzstani Som"),
    ("KHR", "KHR - Cambodian Riel"),
    ("KMF", "KMF - Comorian Franc"),
    ("KRW", "KRW - South Korean Won"),
    ("KWD", "KWD - Kuwaiti Dinar"),
    ("KYD", "KYD - Cayman Islands Dollar"),
    ("KZT", "KZT - Kazakhstani Tenge"),
    ("LAK", "LAK - Lao Kip"),
    ("LBP", "LBP - Lebanese Pound"),
    ("LKR", "LKR - Sri Lankan Rupee"),
    ("LRD", "LRD - Liberian Dollar"),
    ("LSL", "LSL - Lesotho Loti"),
    ("LYD", "LYD - Libyan Dinar"),
    ("MAD", "MAD - Moroccan Dirham"),
    ("MDL", "MDL - Moldovan Leu"),
    ("MGA", "MGA - Malagasy Ariary"),
    ("MKD", "MKD - Macedonian Denar"),
    ("MMK", "MMK - Myanmar Kyat"),
    ("MNT", "MNT - Mongolian Tögrög"),
    ("MOP", "MOP - Macanese Pataca"),
    ("MRU", "MRU - Mauritanian Ouguiya"),
    ("MUR", "MUR - Mauritian Rupee"),
    ("MVR", "MVR - Maldivian Rufiyaa"),
    ("MWK", "MWK - Malawian Kwacha"),
    ("MXN", "MXN - Mexican Peso"),
    ("MYR", "MYR - Malaysian Ringgit"),
    ("MZN", "MZN - Mozambican Metical"),
    ("NAD", "NAD - Namibian Dollar"),
    ("NIO", "NIO - Nicaraguan Córdoba"),
    ("NOK", "NOK - Norwegian Krone"),
    ("NPR", "NPR - Nepalese Rupee"),
    ("NZD", "NZD - New Zealand Dollar"),
    ("OMR", "OMR - Omani Rial"),
    ("PAB", "PAB - Panamanian Balboa"),
    ("PEN", "PEN - Peruvian Sol"),
    ("PGK", "PGK - Papua New Guinean Kina"),
    ("PHP", "PHP - Philippine Peso"),
    ("PKR", "PKR - Pakistani Rupee"),
    ("PLN", "PLN - Polish Złoty"),
    ("PYG", "PYG - Paraguayan Guaraní"),
    ("QAR", "QAR - Qatari Riyal"),
    ("RON", "RON - Romanian Leu"),
    ("RSD", "RSD - Serbian Dinar"),
    ("RUB", "RUB - Russian Ruble"),
    ("RWF", "RWF - Rwandan Franc"),
    ("SAR", "SAR - Saudi Riyal"),
    ("SBD", "SBD - Solomon Islands Dollar"),
    ("SCR", "SCR - Seychellois Rupee"),
    ("SDG", "SDG - Sudanese Pound"),
    ("SEK", "SEK - Swedish Krona"),
    ("SGD", "SGD - Singapore Dollar"),
    ("SHP", "SHP - Saint Helena Pound"),
    ("SLE", "SLE - Sierra Leonean Leone"),
    ("SOS", "SOS - Somali Shilling"),
    ("SRD", "SRD - Surinamese Dollar"),
    ("SSP", "SSP - South Sudanese Pound"),
    ("STN", "STN - São Tomé and Príncipe Dobra"),
    ("SVC", "SVC - Salvadoran Colón"),
    ("SYP", "SYP - Syrian Pound"),
    ("SZL", "SZL - Swazi Lilangeni"),
    ("THB", "THB - Thai Baht"),
    ("TJS", "TJS - Tajikistani Somoni"),
    ("TMT", "TMT - Turkmenistani Manat"),
    ("TND", "TND - Tunisian Dinar"),
    ("TOP", "TOP - Tongan Paʻanga"),
    ("TRY", "TRY - Turkish Lira"),
    ("TTD", "TTD - Trinidad and Tobago Dollar"),
    ("TWD", "TWD - New Taiwan Dollar"),
    ("TZS", "TZS - Tanzanian Shilling"),
    ("UAH", "UAH - Ukrainian Hryvnia"),
    ("UGX", "UGX - Ugandan Shilling"),
    ("UYU", "UYU - Uruguayan Peso"),
    ("UZS", "UZS - Uzbekistani Soʻm"),
    ("VES", "VES - Venezuelan Bolívar"),
    ("VND", "VND - Vietnamese Đồng"),
    ("VUV", "VUV - Vanuatu Vatu"),
    ("WST", "WST - Samoan Tālā"),
    ("XAF", "XAF - Central African CFA Franc"),
    ("XCD", "XCD - East Caribbean Dollar"),
    ("XOF", "XOF - West African CFA Franc"),
    ("XPF", "XPF - CFP Franc"),
    ("YER", "YER - Yemeni Rial"),
    ("ZAR", "ZAR - South African Rand"),
    ("ZMW", "ZMW - Zambian Kwacha"),
    ("ZWG", "ZWG - Zimbabwe Gold"),
]

# Inbound taxpayer-matching risk bands. An inbound record matched to a
# Nigerian taxpayer is risk-profiled by account balance (in the record's own
# currency, kept simple for the demo): at or above the enhanced threshold it
# is routed to an enhanced review, at or above the specific threshold to a
# specific review, otherwise to general compliance monitoring.
RISK_ENHANCED_THRESHOLD: Decimal = Decimal("100000")
RISK_SPECIFIC_THRESHOLD: Decimal = Decimal("50000")

# Portal account lockout threshold.
PORTAL_MAX_FAILED_LOGINS: int = 5

# Supervision Centre working hours (local time, Africa/Lagos). Credential
# access is available from the start hour up to (not including) the end hour.
WORK_DAY_START_HOUR: int = 8
WORK_DAY_END_HOUR: int = 18

# Longest credential validity the Super Admin may grant, in months.
CREDENTIAL_MAX_MONTHS: int = 12

# Issuance validity window: an issued credential must be used within this
# many hours of issuance or it lapses unused.
CREDENTIAL_UNUSED_VALIDITY_HOURS: int = 24
