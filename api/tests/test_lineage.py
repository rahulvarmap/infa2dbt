import pytest

from api.parser import parse_powercenter_xml, topological_order
from api.parser.lineage import LineageGraph, build_lineage, trace_field
from api.parser.models import Connector, Instance, Mapping, Port, Transformation
from api.tests.test_parser import SAMPLE


@pytest.fixture
def mapping():
    return parse_powercenter_xml(SAMPLE.read_bytes()).mappings[0]


def test_sample_instance_graph(mapping):
    graph = build_lineage(mapping)
    assert len(graph.nodes) == 5
    assert len(graph.edges) == 4
    assert sum(len(edge.fields) for edge in graph.edges) == 35
    assert {n.type for n in graph.nodes} == {
        "Source", "Source Qualifier", "Expression", "Filter", "Target",
    }
    assert len({(edge.source, edge.target) for edge in graph.edges}) == 4


def test_gender_exact_hop_order_and_expression(mapping):
    hops = trace_field(mapping, "DIM_MEMBER", "GENDER_DESC")
    assert [(h.instance, h.port) for h in hops] == [
        ("DIM_MEMBER", "GENDER_DESC"),
        ("FIL_ACTIVE_MEMBERS", "GENDER_DESC"),
        ("EXP_MEMBER_DERIVE", "o_GENDER_DESC"),
        ("EXP_MEMBER_DERIVE", "GENDER_CD"),
        ("SQ_MEMBER_ENROLLMENT", "GENDER_CD"),
        ("MEMBER_ENROLLMENT", "GENDER_CD"),
    ]
    assert hops[2].expression == "DECODE(GENDER_CD, 'M', 'Male', 'F', 'Female', 'Unknown')"
    assert [h.type for h in hops] == [
        "Target", "Filter", "Expression", "Expression", "Source Qualifier", "Source",
    ]


def test_full_name_reaches_both_source_fields(mapping):
    hops = LineageGraph(mapping).trace_field("DIM_MEMBER", "FULL_NAME")
    assert {(h.instance, h.port) for h in hops if h.type == "Source"} == {
        ("MEMBER_ENROLLMENT", "FIRST_NAME"), ("MEMBER_ENROLLMENT", "LAST_NAME"),
    }
    assert len({(h.instance, h.port) for h in hops}) == len(hops)
    assert "FIRST_NAME" in hops[2].expression and "LAST_NAME" in hops[2].expression


def test_expression_whole_words_case_insensitive_and_not_string_literals(mapping):
    expression = mapping.transformations[1].ports[-3]
    assert expression.name == "o_GENDER_DESC"
    expression.expression = "DECODE(gender_cd, 'FIRST_NAME', 'LAST_NAME', GENDER_CD_EXTRA)"
    hops = trace_field(mapping, "DIM_MEMBER", "GENDER_DESC")
    assert [h.port for h in hops if h.type == "Source"] == ["GENDER_CD"]
    assert trace_field(mapping, "dim_member", "gender_desc")[0].port == "GENDER_DESC"


def test_pass_through_and_constant_outputs(mapping):
    assert trace_field(mapping, "DIM_MEMBER", "MEMBER_ID")[-1].instance == "MEMBER_ENROLLMENT"
    hops = trace_field(mapping, "DIM_MEMBER", "LOAD_DTS")
    assert hops[-1].instance == "EXP_MEMBER_DERIVE"
    assert hops[-1].expression == "SYSDATE"


def test_unknown_type_and_isolated_instance_preserved(mapping):
    mapping.instances.append(Instance(name="orphan", type="TRANSFORMATION", transformation_name="custom"))
    mapping.transformations.append(Transformation(name="custom", type="Custom Type", ports=[Port(name="x")]))
    graph = LineageGraph(mapping)
    assert graph.as_model().nodes[-1].type == "Custom Type"
    assert graph.trace_field("orphan", "x")[0].type == "Custom Type"


@pytest.mark.parametrize("instance,field,match", [
    ("missing", "GENDER_DESC", "Unknown instance"),
    ("DIM_MEMBER", "missing", "Unknown field"),
])
def test_unknown_fields_are_explicit(mapping, instance, field, match):
    with pytest.raises(ValueError, match=match):
        trace_field(mapping, instance, field)


def test_scoped_graph_ids_do_not_collide(mapping):
    first = LineageGraph(mapping).as_model("folder/a", scoped=True)
    second = LineageGraph(mapping).as_model("folder/b", scoped=True)
    assert not {n.id for n in first.nodes} & {n.id for n in second.nodes}
    assert all(e.source in {n.id for n in first.nodes} for e in first.edges)


def test_cycles_report_clear_errors(mapping):
    mapping.connectors.append(Connector(
        from_instance="DIM_MEMBER", from_field="x",
        to_instance="MEMBER_ENROLLMENT", to_field="x",
    ))
    with pytest.raises(ValueError, match="Cycle detected"):
        LineageGraph(mapping)


def test_internal_expression_cycle_is_reported():
    mapping = Mapping(
        name="cycle", instances=[Instance(name="exp", transformation_name="exp")],
        transformations=[Transformation(name="exp", type="Expression", ports=[
            Port(name="a", porttype="INPUT/OUTPUT", expression="b"),
            Port(name="b", porttype="INPUT/OUTPUT", expression="a"),
        ])],
    )
    with pytest.raises(ValueError, match="Cycle detected in field lineage"):
        trace_field(mapping, "exp", "a")


def test_local_variable_expression_chain_reaches_source():
    xml = b"""<POWERMART><REPOSITORY NAME="r"><FOLDER NAME="f">
      <SOURCE NAME="S"><SOURCEFIELD NAME="A" DATATYPE="string"/></SOURCE>
      <TARGET NAME="T"><TARGETFIELD NAME="B" DATATYPE="string"/></TARGET>
      <MAPPING NAME="local_variables">
        <TRANSFORMATION NAME="E" TYPE="Expression">
          <TRANSFORMFIELD NAME="A" PORTTYPE="INPUT" DATATYPE="string"/>
          <TRANSFORMFIELD NAME="v" PORTTYPE="VARIABLE" DATATYPE="string" EXPRESSION="A"/>
          <TRANSFORMFIELD NAME="B" PORTTYPE="OUTPUT" DATATYPE="string" EXPRESSION="v"/>
        </TRANSFORMATION>
        <INSTANCE NAME="S" TYPE="Source Definition" TRANSFORMATION_NAME="S"/>
        <INSTANCE NAME="E" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="E"/>
        <INSTANCE NAME="T" TYPE="Target Definition" TRANSFORMATION_NAME="T"/>
        <CONNECTOR FROMINSTANCE="S" FROMFIELD="A" TOINSTANCE="E" TOFIELD="A"/>
        <CONNECTOR FROMINSTANCE="E" FROMFIELD="B" TOINSTANCE="T" TOFIELD="B"/>
      </MAPPING>
    </FOLDER></REPOSITORY></POWERMART>"""
    mapping = parse_powercenter_xml(xml).mappings[0]
    hops = trace_field(mapping, "T", "B")
    assert [(hop.instance, hop.port) for hop in hops] == [
        ("T", "B"), ("E", "B"), ("E", "v"), ("E", "A"), ("S", "A"),
    ]


def test_identifiers_match_case_insensitively_across_definitions_connectors_and_lineage():
    xml = b"""<POWERMART><REPOSITORY NAME="r"><FOLDER NAME="f">
      <SOURCE NAME="SOURCE_DEF"><SOURCEFIELD NAME="A" DATATYPE="string"/></SOURCE>
      <TARGET NAME="TARGET_DEF"><TARGETFIELD NAME="ID" DATATYPE="string"/></TARGET>
      <MAPPING NAME="case_mapping">
        <TRANSFORMATION NAME="EXPR_DEF" TYPE="Expression">
          <TRANSFORMFIELD NAME="A" PORTTYPE="INPUT" DATATYPE="string"/>
          <TRANSFORMFIELD NAME="O" PORTTYPE="OUTPUT" DATATYPE="string" EXPRESSION="a"/>
        </TRANSFORMATION>
        <INSTANCE NAME="s" TYPE="source definition" TRANSFORMATION_NAME="source_def"/>
        <INSTANCE NAME="e" TYPE="transformation" TRANSFORMATION_NAME="expr_def"/>
        <INSTANCE NAME="t" TYPE="target definition" TRANSFORMATION_NAME="target_def"/>
        <CONNECTOR FROMINSTANCE="S" FROMFIELD="a" TOINSTANCE="E" TOFIELD="A"/>
        <CONNECTOR FROMINSTANCE="e" FROMFIELD="o" TOINSTANCE="T" TOFIELD="id"/>
      </MAPPING>
    </FOLDER></REPOSITORY></POWERMART>"""
    mapping = parse_powercenter_xml(xml).mappings[0]
    assert [item.name for item in mapping.sources] == ["SOURCE_DEF"]
    assert [item.name for item in mapping.targets] == ["TARGET_DEF"]
    assert topological_order(mapping) == ["s", "e", "t"]
    hops = trace_field(mapping, "T", "ID")
    assert [(hop.instance, hop.port) for hop in hops] == [
        ("t", "ID"), ("e", "O"), ("e", "A"), ("s", "A"),
    ]


def test_long_chains_do_not_hit_recursion_limit():
    mapping = Mapping(name="long", instances=[Instance(name=f"n{i}") for i in range(1100)])
    mapping.connectors = [
        Connector(from_instance=f"n{i}", from_field="f", to_instance=f"n{i+1}", to_field="f")
        for i in range(1099)
    ]
    hops = trace_field(mapping, "n1099", "f")
    assert len(hops) == 1100
    assert hops[-1].instance == "n0"
