from lxml import etree

from .models import Connector, Field, Instance, Mapping, ParseResult, Port, Source, Target, Transformation


def _tag(node) -> str:
    return etree.QName(node).localname.upper() if isinstance(node.tag, str) else ""


def _attrs(node) -> dict[str, str]:
    return {etree.QName(key).localname.upper(): value for key, value in node.attrib.items()}


def _children(node, name):
    return [child for child in node if _tag(child) == name]


def _attributes(node) -> dict[str, str]:
    return {
        _attrs(child).get("NAME", ""): _attrs(child).get("VALUE", "")
        for child in _children(node, "TABLEATTRIBUTE")
    }


def _field(node, port=False):
    attrs = _attrs(node)
    values = {key: attrs.get(key.upper(), "") for key in ("name", "datatype", "precision", "scale")}
    if port:
        return Port(**values, porttype=attrs.get("PORTTYPE", ""), expression=attrs.get("EXPRESSION", ""))
    return Field(**values)


def _transformation(node):
    attrs = _attrs(node)
    return Transformation(
        name=attrs.get("NAME", ""),
        type=attrs.get("TYPE", ""),
        ports=[_field(port, True) for port in _children(node, "TRANSFORMFIELD")],
        attributes=_attributes(node),
    )


def parse_powercenter_xml(xml_bytes: bytes) -> ParseResult:
    """Read PowerCenter exports without loading DTDs or expanding entities."""
    parser = etree.XMLParser(
        load_dtd=False, no_network=True, resolve_entities=False,
        dtd_validation=False, recover=False, huge_tree=False,
    )
    try:
        root = etree.fromstring(xml_bytes, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as error:
        raise ValueError("Invalid XML: the document is empty, malformed, or exceeds safe XML limits.") from error
    if _tag(root) != "POWERMART":
        raise ValueError("Expected a POWERMART document containing REPOSITORY and FOLDER elements.")
    dtd = root.getroottree().docinfo.internalDTD
    if dtd is not None and list(dtd.iterentities()):
        raise ValueError("XML entity declarations are not supported; entity resolution is disabled.")
    # Entity references must never disappear silently from an IR.
    if any(isinstance(node, etree._Entity) for node in root.iter()):
        raise ValueError("XML entity references are not supported; entity resolution is disabled.")
    result = ParseResult()
    for repository in _children(root, "REPOSITORY"):
        repository_name = _attrs(repository).get("NAME", "")
        for folder in _children(repository, "FOLDER"):
            context = {"repository": repository_name, "folder": _attrs(folder).get("NAME", "")}
            sources = [
                Source(name=_attrs(node).get("NAME", ""),
                       fields=[_field(field) for field in _children(node, "SOURCEFIELD")],
                       attributes=_attributes(node), **context)
                for node in _children(folder, "SOURCE")
            ]
            targets = [
                Target(name=_attrs(node).get("NAME", ""),
                       fields=[_field(field) for field in _children(node, "TARGETFIELD")],
                       attributes=_attributes(node), **context)
                for node in _children(folder, "TARGET")
            ]
            result.sources.extend(sources)
            result.targets.extend(targets)
            reusable = {_attrs(node).get("NAME", ""): node for node in _children(folder, "TRANSFORMATION")}
            for node in _children(folder, "MAPPING"):
                instances = []
                for instance in _children(node, "INSTANCE"):
                    attrs = _attrs(instance)
                    instances.append(Instance(
                        name=attrs.get("NAME", ""), type=attrs.get("TYPE", ""),
                        transformation_name=attrs.get("TRANSFORMATION_NAME", ""),
                        transformation_type=attrs.get("TRANSFORMATION_TYPE", ""),
                    ))
                source_names = {
                    item.transformation_name or item.name for item in instances
                    if item.type.casefold() in {"source", "source definition"}
                    or item.transformation_type.casefold() == "source definition"
                }
                target_names = {
                    item.transformation_name or item.name for item in instances
                    if item.type.casefold() in {"target", "target definition"}
                    or item.transformation_type.casefold() == "target definition"
                }
                transformations = [_transformation(child) for child in _children(node, "TRANSFORMATION")]
                known = {item.name for item in transformations}
                for instance in instances:
                    if instance.transformation_name in reusable and instance.transformation_name not in known:
                        transformations.append(_transformation(reusable[instance.transformation_name]))
                        known.add(instance.transformation_name)
                connectors = []
                for connector in _children(node, "CONNECTOR"):
                    attrs = _attrs(connector)
                    connectors.append(Connector(
                        from_instance=attrs.get("FROMINSTANCE", ""),
                        from_field=attrs.get("FROMFIELD", ""),
                        to_instance=attrs.get("TOINSTANCE", ""),
                        to_field=attrs.get("TOFIELD", ""),
                    ))
                result.mappings.append(Mapping(
                    name=_attrs(node).get("NAME", ""), **context,
                    sources=[item for item in sources if item.name in source_names],
                    targets=[item for item in targets if item.name in target_names],
                    transformations=transformations, instances=instances, connectors=connectors,
                    attributes=_attributes(node),
                ))
    return result
