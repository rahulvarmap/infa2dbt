from api.processing_worker import process_xml_request
from api.parser.diagnostics import validate_parse_result
from api.parser.parser import parse_powercenter_xml


def test_empty_export_returns_a_structured_warning():
    result = process_xml_request("parse", b"<POWERMART/>", {})
    assert result["status_code"] == 200
    diagnostic = result["content"]["diagnostics"][0]
    assert diagnostic == {
        "severity": "warning",
        "code": "EMPTY_EXPORT",
        "message": "The XML contains no mappings to inspect.",
        "mapping": "",
        "instance": None,
        "field": None,
    }


def test_undeclared_connector_field_is_a_mapping_scoped_error():
    xml = b"""<POWERMART><REPOSITORY NAME="r"><FOLDER NAME="f">
      <SOURCE NAME="S"><SOURCEFIELD NAME="A" DATATYPE="string"/></SOURCE>
      <TARGET NAME="T"><TARGETFIELD NAME="ID" DATATYPE="string"/></TARGET>
      <MAPPING NAME="broken">
        <INSTANCE NAME="S" TYPE="Source Definition" TRANSFORMATION_NAME="S"/>
        <INSTANCE NAME="T" TYPE="Target Definition" TRANSFORMATION_NAME="T"/>
        <CONNECTOR FROMINSTANCE="S" FROMFIELD="A" TOINSTANCE="T" TOFIELD="NOT_A_COLUMN"/>
      </MAPPING>
    </FOLDER></REPOSITORY></POWERMART>"""
    response = process_xml_request("parse", xml, {})
    assert response["status_code"] == 200
    diagnostic = next(item for item in response["content"]["diagnostics"] if item["code"] == "UNDECLARED_PORT")
    assert diagnostic == {
        "severity": "error",
        "code": "UNDECLARED_PORT",
        "message": "Connector to endpoint 'T.NOT_A_COLUMN' is not declared by its definition.",
        "mapping": "broken",
        "instance": "T",
        "field": "NOT_A_COLUMN",
    }
    assert response["content"]["execution_order"] == {}
    assert response["content"]["lineage"]["nodes"] == []


def test_capability_registry_reports_partial_and_unknown_types():
    xml = b"""<POWERMART><REPOSITORY NAME="r"><FOLDER NAME="f">
      <MAPPING NAME="capabilities">
        <TRANSFORMATION NAME="e" TYPE="Expression"/>
        <TRANSFORMATION NAME="x" TYPE="Vendor Special"/>
        <INSTANCE NAME="e" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="e"/>
        <INSTANCE NAME="x" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="x"/>
      </MAPPING>
    </FOLDER></REPOSITORY></POWERMART>"""
    diagnostics = validate_parse_result(parse_powercenter_xml(xml))
    assert {item.code for item in diagnostics} == {
        "PARTIAL_EXPRESSION_LINEAGE",
        "UNKNOWN_TRANSFORMATION_TYPE",
    }
    assert all(item.severity == "warning" and item.mapping == "capabilities" for item in diagnostics)


def test_mapping_identity_collisions_are_reported_without_building_a_graph():
    xml = b"""<POWERMART><REPOSITORY NAME="r"><FOLDER NAME="f">
      <MAPPING NAME="DailyLoad"/>
      <MAPPING NAME="dailyload"/>
    </FOLDER></REPOSITORY></POWERMART>"""
    response = process_xml_request("parse", xml, {})
    assert response["status_code"] == 200
    collisions = [item for item in response["content"]["diagnostics"] if item["code"] == "DUPLICATE_MAPPING_IDENTITY"]
    assert len(collisions) == 2
    assert {item["severity"] for item in collisions} == {"error"}
    assert response["content"]["lineage"]["nodes"] == []


def test_unknown_expression_function_is_a_structured_warning_not_a_crash():
    xml = b"""<POWERMART><REPOSITORY NAME="r"><FOLDER NAME="f">
      <SOURCE NAME="S"><SOURCEFIELD NAME="A" DATATYPE="string"/></SOURCE>
      <TRANSFORMATION NAME="E" TYPE="Expression">
        <TRANSFORMFIELD NAME="A" PORTTYPE="INPUT"/>
        <TRANSFORMFIELD NAME="O" PORTTYPE="OUTPUT" EXPRESSION="VENDOR_FUNC(A)"/>
      </TRANSFORMATION>
      <MAPPING NAME="unknown_function">
        <INSTANCE NAME="S" TYPE="Source Definition" TRANSFORMATION_NAME="S"/>
        <INSTANCE NAME="E" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="E"/>
        <CONNECTOR FROMINSTANCE="S" FROMFIELD="A" TOINSTANCE="E" TOFIELD="A"/>
      </MAPPING>
    </FOLDER></REPOSITORY></POWERMART>"""
    response = process_xml_request("parse", xml, {})
    assert response["status_code"] == 200
    diagnostic = next(
        item for item in response["content"]["diagnostics"]
        if item["code"] == "UNKNOWN_EXPRESSION_FUNCTION"
    )
    assert diagnostic["severity"] == "warning"
    assert diagnostic["mapping"] == "unknown_function"
    assert diagnostic["instance"] == "E"
    assert diagnostic["field"] == "O"


def test_invalid_expression_is_reported_without_crashing_parse():
    xml = b"""<POWERMART><REPOSITORY NAME="r"><FOLDER NAME="f">
      <TRANSFORMATION NAME="E" TYPE="Expression">
        <TRANSFORMFIELD NAME="O" PORTTYPE="OUTPUT" EXPRESSION="A +"/>
      </TRANSFORMATION>
      <MAPPING NAME="bad_expression">
        <INSTANCE NAME="E" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="E"/>
      </MAPPING>
    </FOLDER></REPOSITORY></POWERMART>"""
    response = process_xml_request("parse", xml, {})
    assert response["status_code"] == 200
    diagnostic = next(
        item for item in response["content"]["diagnostics"]
        if item["code"] == "INVALID_EXPRESSION"
    )
    assert diagnostic["severity"] == "error"
    assert diagnostic["instance"] == "E"
    assert diagnostic["field"] == "O"
