# NRS AEOI-CRS Platform (Demonstration MVP)

A demonstration slice of an Automatic Exchange of Information platform for the
Common Reporting Standard, prepared by Seismic Consulting for the Nigeria
Revenue Service. The system pairs a self-registration portal for Reporting
Financial Institutions with an internal Supervision Centre, and simulates the
collection, validation, packaging, and Common Transmission System exchange
cycle end to end.

All external integrations (CTS transport, encryption, email delivery) are
simulated. The CRS XML, MessageRefID, DocRefID, and CorrDocRefID structures
underneath are real and follow the OECD CRS XML User Guide conventions.

## Running the demo

```
pip install -r requirements.txt
python manage.py migrate
python manage.py demo_seed
python manage.py runserver
```

`demo_seed` resets the database and prints every credential the presenter
needs: the Super Admin password, three pre-issued officer credentials, and
five portal accounts. Rerun it at any time to restore a clean demo state.

Surfaces:

- RFI Portal: http://localhost:8000/portal/login/
- Supervision Centre: http://localhost:8000/backoffice/login/
- Demo email outbox: http://localhost:8000/demo/outbox/

Tests:

```
python -m pytest
```

## The 10-minute demo walkthrough

Have the `demo_seed` console output visible; it holds all credentials.

**Minute 0 to 2. Issued credentials, the access model.**
Sign in to the Supervision Centre as `superadmin`. Show the Admin Console:
officers hold no standing passwords; access is by single-use, time-boxed
credential. Issue a credential live: pick an Internal Admin user, set 1.0
hours, and show the one-time passcode panel (stress that it is shown once and
stored hashed). Open Live Sessions to show remaining time and the revoke
control. Sign out.

**Minute 2 to 3. Session expiry, live.**
Sign in with the pre-issued Exchange Officer credential (0.05 hours, a 3
minute window). Point at the countdown in the header. Continue the demo; it
will expire mid-presentation and produce the "access window has ended" page,
after which the credential refuses reuse. That refusal is the message.

**Minute 3 to 5. Enrolment, four-eyes registration.**
In a second browser (or private window), open the portal enrolment form and
submit a new institution with any PDF as the CEO letter; show the reference
number and status-check page. Sign in to the backoffice with the pre-issued
Registration Supervisor credential, open Registration, and decide the seeded
Harmattan Merchant Bank application (already reviewed and recommended by a
Registration Officer; the four-eyes note explains why the reviewer cannot
decide). Approve it, then show the temporary password email in the demo
outbox.

**Minute 5 to 7. Filing and validation.**
Sign in to the portal as the First Bank of Wazobia Maker, open Filings, and
show the three paths: XML upload (with the schema panel and line-level
rejection), manual entry, and the one-click nil return (CRS703). Open filing
FIL-2025-00002 (Submitted). Switch to the backoffice with the pre-issued
Returns Officer credential, open Returns, and run validation on that filing:
three findings appear, split file level and record level (missing TIN warning,
implausible TIN, negative balance). Return it to the RFI with a reason. Back
in the portal, show the returned filing: only flagged records can be amended,
and an amendment creates an OECD2 record carrying a CorrDocRefID to the
original. The seeded Lagoon Capital filing FIL-2025-00003 already shows this
lineage.

**Minute 7 to 9. Exchange, the showpiece.**
As an Exchange Officer (issue a fresh credential from the admin console if the
3 minute one has expired), open Exchange. Show the partner jurisdiction table
with key fingerprints, then click Build exchange packages: the remaining
approved record is sorted by residence jurisdiction into a package with a
compliant MessageRefID (NG2025AE000001). Open it and click through the
pipeline: sign, AES key, encrypt, wrap, metadata, upload. Use the act-as-
partner control to accept it and archive the cycle. Then open the seeded
France package: record errors are outstanding; open the Correction queue,
supply the missing TIN, and build the CRS702 correction message. Show the
generated XML: CorrDocRefID pointing at the original DocRefID, DocTypeIndic
OECD2. Open Inbound Files: process the Canada file (its stage one decrypt
fails, issuing a 50003 file-error Status Message to the partner), then walk
the United Kingdom file through: approve it for domestic use and run taxpayer
matching. Two of the three records match the domestic register by TIN and are
risk-profiled (enhanced, specific, general review tracks by balance); one is
left unmatched. Use the resolve-identities control to register the unmatched
holder, reattempt matching to close the loop, then disseminate. The Tax
Authority View shows the matched records sorted into their review tracks.

**Minute 9 to 10. Reporting and audit.**
Open Reports for the filing funnel, exchange statistics, correction rates, and
timeliness against 30 September. Close on the Auditor view: the append-only
audit log with the flagged credential-sharing event highlighted in red.

## Architecture

- Django 5 modular monolith, SQLite, server-rendered templates, plain CSS,
  minimal vanilla JS. No SPA framework, no charting libraries.
- Apps: `core` (officers, issued credentials, audit log, auth backend,
  middleware), `portal` (external RFI surface), `backoffice` (internal
  Supervision Centre), `exchange` (CTS simulation, packaging, status
  messages, inbound taxpayer matching).
- The Supervision Centre is organised to the three BPMN processes of the
  approved process model: Process 1 — Front-End Filing (enrolment approval and
  returns validation), Process 2 — Outbound Exchange (packaging and
  corrections), and Process 3 — Inbound Receipt (inbound files, taxpayer
  matching, and the Tax Authority View).
- Hard surface separation: each surface has its own session cookie
  (`nrs_portal_sessionid` scoped to `/portal`, `nrs_backoffice_sessionid`
  scoped to `/backoffice`), its own login page, base template, and
  navigation. Cross-surface requests return 404, not 403.
- Regulatory constants (deadlines, partner list, FI categories, inbound
  risk-rating thresholds) live in `core/config.py`.
- The credential lifecycle is enforced by a custom auth backend
  (`core/auth_backends.py`) plus middleware (`core/middleware.py`). The
  server clock is the source of truth; the header countdown is presentation.

## Demo assumptions

Recorded where the specification or regulatory practice left room:

1. **Simplified CRS schema for uploads.** Uploaded filings use a reduced
   document (see `portal/schema/crs_filing_simplified.xsd`) rather than the
   full CRS XML Schema v2.0. Structural validation happens in code because
   the demo avoids native XSD dependencies. Generated exchange XML follows
   the CRS_OECD v2 element structure with the fields the demo collects.
2. **Status Message codes are indicative.** File errors use 50003 (failed
   decryption), 50008 (invalid MessageRefID), and 50010 (duplicate
   MessageRefID); record errors use 80003 (invalid country code) and 80008
   (TIN not supplied). They follow the CRS Status Message convention of
   50000-series file and 80000-series record errors without claiming the
   exact production catalogue.
3. **Correction lineage inside one filing.** A domestic resubmission keeps
   the same filing; the amended record is a new AccountReport with a fresh
   DocRefID, DocTypeIndic OECD2, and CorrDocRefID pointing at the original,
   which remains visible as superseded. The CRS702 exchange correction
   applies the same convention across packages.
4. **RFI activation.** An approved institution becomes Active when its
   Primary User completes the forced first password change.
5. **Nil returns.** Filed as message type CRS703 and accepted by a Returns
   Supervisor like any filing.
6. **PU change requests** are listed in the role matrix for the Registration
   Supervisor but are out of scope for this demo build.
7. **Inbound taxpayer matching.** Inbound records are matched to a seeded
   domestic taxpayer register on TIN, then on identity (name) as a fallback,
   and risk-profiled by account balance into enhanced, specific, and general
   review tracks. Unmatched records are held and matching is reattempted once
   identity is resolved. The register stands in for the wider NRS taxpayer
   database; production matching rules belong in the BRD (see note N-03).
8. **Seeded portal passwords.** Seeded portal users skip the forced password
   change so the walkthrough flows; a newly approved PU goes through the
   forced change as designed.
9. **MessageRefID.** Format is sending country + year + receiving country +
   six-digit sequence per corridor (for example NG2025GB000123).
10. **Scope.** The Supervision Centre implements the three BPMN processes only.
    Administrative penalty enforcement, out of scope of the process model, is
    not part of this build.

## Roles and access

Backoffice access is by four CRS internal user roles, assigned by the Super
Admin. A user may hold several roles at once; their access is the union.
There are no pre-created users: the Super Admin signs in, creates users under
Users, assigns each one or more roles, and issues single-use credentials from
the Admin Console.

| Role | Access |
|---|---|
| Super Admin | Standing account. Create users, assign roles, issue and revoke credentials, live sessions, audit log. Does not process casework. |
| Internal Admin User | Full operational access across registration, returns, exchange, inbound receipt, reports, and the audit log, including the final authorisations (enrolment and returns approval, transmission). |
| Assistant Admin | Processing and review across all workspaces (record assessments, validate returns, run exchange operations, process inbound files), but not the final authorisations or the audit log. |
| Export Only User | Read and export of reports, exchange packages and their generated XML, and the Tax Authority View. No processing. |
| View Only User | Read-only across the workspaces. No actions, no exports. |

Four-eyes is preserved by identity: whoever reviewed an enrolment or validated
a filing cannot also make the final decision on it, regardless of role. In
practice an Assistant Admin prepares and an Internal Admin authorises, or two
Internal Admins act as the two pairs of eyes.
