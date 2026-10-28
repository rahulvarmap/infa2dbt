from pathlib import Path

import pytest

from api.parser import parse_powercenter_xml, topological_order
from api.parser.models import Connector, Instance, Mapping

SAMPLE = Path(__file__).resolve().parents[2] / "samples" / "m_member_enrollment.xml"
EXPECTED_ORDER = [
    "MEMBER_ENROLLMENT", "SQ_MEMBER_ENROLLMENT", "EXP_MEMBER_DERIVE",
    "FIL_ACTIVE_MEMBERS", "DIM_MEMBER",
]


@pytest.fixture
def parsed():
    return parse_powercenter_xml(SAMPLE.read_bytes())


def document(mapping=""):
    return f'<POWERMART><REPOSITORY NAME="repo"><FOLDER NAME="folder">{mapping}</FOLDER></REPOSITORY></POWERMART>'.encode()


def test_sample_counts_and_order(parsed):
    assert len(parsed.sources) == 1
    assert len(parsed.targets) == 1
    assert len(parsed.mappings) == 1
    mapping = parsed.mappings[0]
    assert mapping.name == "m_member_enrollment"
    assert len(mapping.sources) == 1
    assert len(mapping.targets) == 1
    assert len(mapping.transformations) == 3
    assert len(mapping.instances) == 5
    assert len(mapping.connectors) == 35
    assert topological_order(mapping) == EXPECTED_ORDER


def test_attributes_and_port_details(parsed):
    source_qualifier, expression, filter_ = parsed.mappings[0].transformations
    assert source_qualifier.attributes["Source Filter"] == "MEMBER_ENROLLMENT.STATUS_CD IS NOT NULL"
    assert source_qualifier.attributes["Sql Query"] == ""
    assert filter_.attributes["Filter Condition"] == "STATUS_CD = 'A' AND ENROLL_END_DT >= TRUNC(SYSDATE)"
    assert all(port.datatype and port.name and port.porttype for port in expression.ports)
    assert any(port.expression for port in expression.ports)
    assert parsed.sources[0].fields[0].precision == "20"
    assert parsed.sources[0].fields[0].scale == "0"


def test_case_insensitive_tags_and_attributes():
    result = parse_powercenter_xml(document(
        '<mapping nAmE="mixed"><transformation name="t" TyPe="Future Vendor Type">'
        '<transformfield name="p" datatype="string" porttype="OUTPUT" expression="x" precision="10" scale="0"/>'
        '<tableattribute nAmE="Custom Setting" VaLuE="YES"/></transformation>'
        '<instance name="t" type="TRANSFORMATION"/></mapping>'
    ).replace(b"POWERMART", b"powermart").replace(b"REPOSITORY", b"repository").replace(b"FOLDER", b"folder"))
    transformation = result.mappings[0].transformations[0]
    assert transformation.type == "Future Vendor Type"
    assert transformation.attributes == {"Custom Setting": "YES"}
    assert transformation.ports[0].expression == "x"
    assert topological_order(result.mappings[0]) == ["t"]


def test_collects_every_mapping_and_folder():
    result = parse_powercenter_xml(
        b'<POWERMART><REPOSITORY NAME="r"><FOLDER NAME="a"><SOURCE NAME="s"/>'
        b'<TARGET NAME="t"/><MAPPING NAME="one"/><MAPPING NAME="two"/></FOLDER>'
        b'<FOLDER NAME="b"><MAPPING NAME="three"/></FOLDER></REPOSITORY>'
        b'<REPOSITORY NAME="r2"><FOLDER NAME="c"><MAPPING NAME="four"/></FOLDER></REPOSITORY></POWERMART>'
    )
    assert [mapping.name for mapping in result.mappings] == ["one", "two", "three", "four"]
    assert result.mappings[-1].repository == "r2"
    assert result.mappings[-1].folder == "c"
    assert len(result.sources) == len(result.targets) == 1


def test_referenced_reusable_transformation():
    result = parse_powercenter_xml(document(
        '<TRANSFORMATION NAME="shared" TYPE="Unfamiliar"><TABLEATTRIBUTE NAME="setting" VALUE="value"/></TRANSFORMATION>'
        '<MAPPING NAME="m"><INSTANCE NAME="shared_alias" TRANSFORMATION_NAME="shared" TYPE="TRANSFORMATION"/></MAPPING>'
    ))
    assert result.mappings[0].transformations[0].type == "Unfamiliar"
    assert result.mappings[0].transformations[0].attributes["setting"] == "value"


def test_doctype_is_not_loaded():
    data = SAMPLE.read_bytes().replace(b'powrmart.dtd', b'https://invalid.invalid/never-load.dtd')
    assert len(parse_powercenter_xml(data).mappings) == 1


@pytest.mark.parametrize("xml", [
    b"", b"<POWERMART>", b"<not-powercenter/>",
    b'<!DOCTYPE POWERMART [<!ENTITY x SYSTEM "file:///etc/passwd">]><POWERMART>&x;</POWERMART>',
    b'<!DOCTYPE POWERMART [<!ENTITY x "expanded">]><POWERMART NAME="&x;"/>',
])
def test_rejects_bad_xml_and_entities(xml):
    with pytest.raises(ValueError):
        parse_powercenter_xml(xml)


def graph(edges, names=("a", "b", "c")):
    return Mapping(
        name="graph", instances=[Instance(name=name) for name in names],
        connectors=[Connector(from_instance=a, from_field="x", to_instance=b, to_field="x") for a, b in edges],
    )


def test_parallel_ports_do_not_duplicate_dependency_and_disconnected_nodes_survive():
    assert topological_order(graph([("a", "b"), ("a", "b")])) == ["a", "c", "b"]
    assert topological_order(Mapping(name="empty")) == []


@pytest.mark.parametrize("edges", [[("a", "b"), ("b", "a")], [("a", "a")]])
def test_cycle_is_clear(edges):
    with pytest.raises(ValueError, match="Cycle detected.*graph"):
        topological_order(graph(edges))


def test_unknown_instance_is_clear():
    with pytest.raises(ValueError, match="unknown instance"):
        topological_order(graph([("a", "missing")]))


def test_duplicate_instance_is_clear():
    with pytest.raises(ValueError, match="duplicate instance"):
        topological_order(graph([], names=("a", "a")))
