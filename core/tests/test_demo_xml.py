"""The demo XML generator must always produce filable documents."""
from __future__ import annotations

import pytest
import xmlschema

from core import config
from core.management.commands.demo_xml import build_amended_file, build_new_file
from portal.xml_ingest import parse_crs_upload

YEAR = config.CURRENT_REPORTING_YEAR
SCHEMA = "exchange/schemas/crs-v2.0/CrsXML_v2.0.xsd"


@pytest.mark.django_db
def test_generated_files_are_schema_valid_and_parse_clean():
    new_xml, doc_refs = build_new_file("GB", "TESTTOKEN")
    amended_xml = build_amended_file("GB", "TESTTOKEN", doc_refs)
    schema = xmlschema.XMLSchema(SCHEMA)
    for label, xml in (("new", new_xml), ("amended", amended_xml)):
        schema.validate(xml)
        result = parse_crs_upload(xml.encode(), YEAR)
        assert result.ok, (label, result.errors)
        assert not result.warnings, (label, result.warnings)
        assert not result.contains_test_data  # live indicators, demo-clean

    # The amendment references exactly the new file's records.
    amended = parse_crs_upload(amended_xml.encode(), YEAR)
    corr_refs = {record.corr_doc_ref_id for record in amended.records}
    assert corr_refs == {doc_refs[0], doc_refs[2]}
    assert amended.message_type_indic == "CRS702"
