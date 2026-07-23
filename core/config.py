"""Central configuration for regulatory constants used across the platform.

All deadline, partner jurisdiction, and risk-rating values live here so the
demo can be retuned without touching application logic.
"""
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

# Activated CRS MCAA exchange relationships seeded into the demo.
# (code, name, activated_since, public key fingerprint shown in the UI)
PARTNER_JURISDICTIONS: list[tuple[str, str, str, str]] = [
    ("GB", "United Kingdom", "2019-04-01", "A3:9F:12:6B:44:E0:8C:71:2D:5A:B9:03:F4:66:1E:C8"),
    ("FR", "France", "2019-04-01", "7C:02:E5:91:AB:34:D8:0F:63:27:9B:CC:15:F0:48:6D"),
    ("DE", "Germany", "2019-09-01", "5E:B1:70:2C:98:DF:41:A6:33:8B:E2:19:74:0C:D5:FF"),
    ("CA", "Canada", "2020-01-01", "D4:88:36:AF:52:C1:6E:90:1B:73:24:E8:BD:07:59:AC"),
    ("AE", "United Arab Emirates", "2020-06-01", "91:3D:C7:20:84:FB:5A:E3:47:0E:B6:6C:D9:12:A8:35"),
    ("ZA", "South Africa", "2019-04-01", "68:F2:0B:D5:31:9C:A7:44:EE:58:C3:82:16:BF:70:D1"),
    ("IN", "India", "2019-09-01", "B7:45:E9:1C:63:0A:D2:8F:39:74:AE:55:C0:96:2B:E4"),
    ("CH", "Switzerland", "2020-01-01", "2F:9A:53:E8:07:BC:64:D1:AF:38:71:0D:C5:92:4E:86"),
    ("MU", "Mauritius", "2021-01-01", "E0:6C:B4:27:F9:13:8D:52:AA:C6:39:75:04:DE:81:BB"),
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
# the preparer must select the currency of each reported account.
CURRENCIES: list[tuple[str, str]] = [
    ("NGN", "NGN - Nigerian Naira"),
    ("USD", "USD - US Dollar"),
    ("EUR", "EUR - Euro"),
    ("GBP", "GBP - Pound Sterling"),
    ("CHF", "CHF - Swiss Franc"),
    ("CAD", "CAD - Canadian Dollar"),
    ("AUD", "AUD - Australian Dollar"),
    ("JPY", "JPY - Japanese Yen"),
    ("CNY", "CNY - Chinese Yuan"),
    ("AED", "AED - UAE Dirham"),
    ("ZAR", "ZAR - South African Rand"),
    ("GHS", "GHS - Ghanaian Cedi"),
    ("KES", "KES - Kenyan Shilling"),
    ("INR", "INR - Indian Rupee"),
    ("MUR", "MUR - Mauritian Rupee"),
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

# Issuance validity window: an issued credential must be used within this
# many hours of issuance or it lapses unused.
CREDENTIAL_UNUSED_VALIDITY_HOURS: int = 24
