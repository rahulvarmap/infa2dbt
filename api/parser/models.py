from pydantic import BaseModel, Field as ModelField


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


class ParseResponse(ParseResult):
    execution_order: dict[str, list[str]] = ModelField(default_factory=dict)
