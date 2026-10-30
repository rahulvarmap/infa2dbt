from pydantic import BaseModel, Field as ModelField
from typing import Literal


class Field(BaseModel):
    name: str
    datatype: str = ""
    precision: str = ""
    scale: str = ""


class Source(BaseModel):
    name: str
    fields: list[Field] = ModelField(default_factory=list)
    attributes: dict[str, str] = ModelField(default_factory=dict)
    repository: str = ""
    folder: str = ""


class Target(Source):
    pass


class Port(Field):
    porttype: str = ""
    expression: str = ""
    group: str = ""
    default_value: str = ""


class Transformation(BaseModel):
    name: str
    type: str = ""
    ports: list[Port] = ModelField(default_factory=list)
    attributes: dict[str, str] = ModelField(default_factory=dict)


class Instance(BaseModel):
    name: str
    type: str = ""
    transformation_name: str = ""
    transformation_type: str = ""


class Connector(BaseModel):
    from_instance: str
    from_field: str
    to_instance: str
    to_field: str


class Mapping(BaseModel):
    name: str
    repository: str = ""
    folder: str = ""
    sources: list[Source] = ModelField(default_factory=list)
    targets: list[Target] = ModelField(default_factory=list)
    transformations: list[Transformation] = ModelField(default_factory=list)
    instances: list[Instance] = ModelField(default_factory=list)
    connectors: list[Connector] = ModelField(default_factory=list)
    attributes: dict[str, str] = ModelField(default_factory=dict)


class ParseResult(BaseModel):
    sources: list[Source] = ModelField(default_factory=list)
    targets: list[Target] = ModelField(default_factory=list)
    mappings: list[Mapping] = ModelField(default_factory=list)


class Diagnostic(BaseModel):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    mapping: str
    instance: str | None = None
    field: str | None = None


class LineageNode(BaseModel):
    id: str
    name: str
    type: str
    mapping: str


class FieldPair(BaseModel):
    from_field: str
    to_field: str


class LineageEdge(BaseModel):
    id: str
    source: str
    target: str
    fields: list[FieldPair] = ModelField(default_factory=list)
    mapping: str


class LineageData(BaseModel):
    nodes: list[LineageNode] = ModelField(default_factory=list)
    edges: list[LineageEdge] = ModelField(default_factory=list)


class LineageHop(BaseModel):
    instance: str
    port: str
    type: str
    expression: str = ""


class TraceResponse(BaseModel):
    mapping: str
    hops: list[LineageHop]


class ParseResponse(ParseResult):
    execution_order: dict[str, list[str]] = ModelField(default_factory=dict)
    lineage: LineageData = ModelField(default_factory=LineageData)
    diagnostics: list[Diagnostic] = ModelField(default_factory=list)
