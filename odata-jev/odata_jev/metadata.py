"""Tool details → one internal model.

Two sources are supported and normalised to the same `Service`:

* a JSON document ("tool details") listing entity sets with their properties, keys and navigation properties,
  see `load_json` and tests/fixtures/sales_tools.json;
* an OData `$metadata` EDMX document, V2 (CSDL 1.0-3.0, including SAP Gateway `sap:` annotations) or V4
  (CSDL 4.0/4.01, including Core/Common labels and Capabilities restrictions), see `parse_edmx`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from lxml import etree
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator

from .errors import MetadataError
from .settings import normalize_version

PropertyKind = Literal["primitive", "enum", "complex"]


class Property(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    type: str = "Edm.String"
    collection: bool = False
    kind: PropertyKind = "primitive"
    nullable: bool = True
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    label: str | None = None
    description: str | None = None
    values: dict[str, str | None] | None = Field(
        None, description="closed set of allowed codes, each with an optional meaning (SAP status codes, enums)"
    )
    filterable: bool = True
    sortable: bool = True
    required_in_filter: bool = False
    display_format: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _collection_type(cls, data: Any) -> Any:
        if isinstance(data, dict):
            t = data.get("type")
            if isinstance(t, str) and t.startswith("Collection(") and t.endswith(")"):
                data = {**data, "type": t[len("Collection(") : -1], "collection": True}
        return data

    @field_validator("values", mode="before")
    @classmethod
    def _values(cls, v: Any) -> Any:
        if isinstance(v, list):
            return {str(x): None for x in v}
        return v

    @field_validator("max_length", mode="before")
    @classmethod
    def _max_length(cls, v: Any) -> Any:
        return None if v in ("max", "Max", "") else v


class NavigationProperty(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    target_type: str
    collection: bool = False
    label: str | None = None
    description: str | None = None


class ComplexType(BaseModel):
    name: str
    properties: list[Property] = Field(default_factory=list)
    _props: dict[str, Property] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        self._props = {p.name: p for p in self.properties}

    def property(self, name: str) -> Property | None:
        return self._props.get(name)


class EntityType(BaseModel):
    name: str
    keys: list[str] = Field(default_factory=list)
    properties: list[Property] = Field(default_factory=list)
    navigation_properties: list[NavigationProperty] = Field(default_factory=list)
    label: str | None = None
    description: str | None = None
    _props: dict[str, Property] = PrivateAttr(default_factory=dict)
    _navs: dict[str, NavigationProperty] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        self._props = {p.name: p for p in self.properties}
        self._navs = {n.name: n for n in self.navigation_properties}

    def property(self, name: str) -> Property | None:
        return self._props.get(name)

    def navigation(self, name: str) -> NavigationProperty | None:
        return self._navs.get(name)

    def member_names(self) -> list[str]:
        return [p.name for p in self.properties] + [n.name for n in self.navigation_properties]


class EntitySet(BaseModel):
    name: str
    entity_type: EntityType
    label: str | None = None
    description: str | None = None
    searchable: bool | None = None  # None: unknown
    addressable: bool = True
    pageable: bool = True
    countable: bool = True
    requires_filter: bool = False
    non_filterable: list[str] = Field(default_factory=list)
    non_sortable: list[str] = Field(default_factory=list)
    required_in_filter: list[str] = Field(default_factory=list)
    nav_bindings: dict[str, str] = Field(default_factory=dict)  # navigation property → target entity set

    @property
    def keys(self) -> list[str]:
        return self.entity_type.keys

    @property
    def properties(self) -> list[Property]:
        return self.entity_type.properties

    @property
    def navigation_properties(self) -> list[NavigationProperty]:
        return self.entity_type.navigation_properties

    def property(self, name: str) -> Property | None:
        return self.entity_type.property(name)

    def navigation(self, name: str) -> NavigationProperty | None:
        return self.entity_type.navigation(name)

    def display_label(self) -> str | None:
        return self.label or self.entity_type.label


class Service(BaseModel):
    service_url: str | None = None
    version: Literal["v2", "v4"] | None = None
    entity_sets: list[EntitySet] = Field(default_factory=list)
    entity_types: dict[str, EntityType] = Field(default_factory=dict)
    complex_types: dict[str, ComplexType] = Field(default_factory=dict)
    aliases: dict[str, str] = Field(default_factory=dict, description="namespace alias → namespace (EDMX)")
    _sets: dict[str, EntitySet] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        self._sets = {s.name: s for s in self.entity_sets}

    def entity_set(self, name: str) -> EntitySet | None:
        return self._sets.get(name)

    def qualify(self, name: str) -> str:
        """`Alias.Type` → `Namespace.Type`, using the schema aliases of the $metadata document."""
        prefix, dot, local = name.rpartition(".")
        return f"{self.aliases.get(prefix, prefix)}.{local}" if dot else name

    def entity_type(self, name: str) -> EntityType | None:
        return self.entity_types.get(name)

    def complex_type(self, name: str) -> ComplexType | None:
        return self.complex_types.get(name)

    def nav_target(self, nav: NavigationProperty) -> EntityType | None:
        return self.entity_types.get(nav.target_type)

    def nav_target_set(self, nav: NavigationProperty, source: EntitySet | None = None) -> EntitySet | None:
        """The entity set a navigation property leads to: the binding of `source`, else the only set of that type."""
        if source is not None and nav.name in source.nav_bindings:
            return self.entity_set(source.nav_bindings[nav.name])
        sets = [s for s in self.entity_sets if s.entity_type.name == nav.target_type]
        return sets[0] if len(sets) == 1 else None

    def resolve(self, et: EntityType, path: str) -> Property | NavigationProperty | None:
        """Walk `a/b/c` through navigation and complex properties; None when any segment is unknown."""
        owner: EntityType | ComplexType | None = et
        found: Property | NavigationProperty | None = None
        for seg in path.split("/"):
            if owner is None:
                return None
            found = owner.property(seg)
            if found is None and isinstance(owner, EntityType):
                found = owner.navigation(seg)
            if found is None:
                return None
            if isinstance(found, NavigationProperty):
                owner = self.nav_target(found)
            elif found.kind == "complex":
                owner = self.complex_type(found.type)
            else:
                owner = None
        return found


# ====================================================================================== loading entry point


def load_tools(
    source: Service | Mapping[str, Any] | str | bytes | os.PathLike[str],
    *,
    service_url: str | None = None,
) -> Service:
    """Accept a `Service`, a JSON tool-details mapping, a path to a .json/.xml/.edmx file, or the document itself."""
    if isinstance(source, Service):
        svc = source
    elif isinstance(source, Mapping):
        svc = load_json(source)
    else:
        text = _read_source(source)
        stripped = text.lstrip()
        if stripped.startswith("<"):
            svc = parse_edmx(text)
        elif stripped.startswith("{"):
            try:
                svc = load_json(json.loads(text))
            except json.JSONDecodeError as e:
                raise MetadataError(f"tool details are not valid JSON: {e}") from e
        else:
            raise MetadataError("tool details must be JSON tool details or an EDMX $metadata document")
    if service_url:
        svc = svc.model_copy(update={"service_url": service_url})
    return svc


def _read_source(source: str | bytes | os.PathLike[str]) -> str:
    if isinstance(source, bytes):
        return source.decode("utf-8-sig")
    if isinstance(source, os.PathLike) or (
        isinstance(source, str) and not source.lstrip().startswith(("<", "{")) and len(source) < 4096
    ):
        path = Path(source)
        try:
            return path.read_text(encoding="utf-8-sig")
        except OSError as e:
            raise MetadataError(f"cannot read tool details from {path}: {e}") from e
    return str(source)


# ====================================================================================== JSON tool details


class _JsonNav(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    target: str
    collection: bool | None = None
    multiplicity: str | None = None
    label: str | None = None
    description: str | None = None


class _JsonEntitySet(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    entity_type: str | None = None
    label: str | None = None
    description: str | None = None
    keys: list[str] = Field(default_factory=list)
    properties: list[Property] = Field(default_factory=list)
    navigation_properties: list[_JsonNav] = Field(default_factory=list)
    searchable: bool | None = None
    addressable: bool = True
    pageable: bool = True
    countable: bool = True
    requires_filter: bool = False
    non_filterable: list[str] = Field(default_factory=list)
    non_sortable: list[str] = Field(default_factory=list)


class _JsonTools(BaseModel):
    model_config = ConfigDict(extra="ignore")
    service_url: str | None = None
    version: str | None = None
    entity_sets: list[_JsonEntitySet]
    complex_types: dict[str, list[Property]] = {}
    enum_types: dict[str, list[str]] = {}

    @field_validator("entity_sets", mode="before")
    @classmethod
    def _sets_as_dict(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return [{"name": k, **spec} for k, spec in v.items()]
        return v

    @field_validator("complex_types", mode="before")
    @classmethod
    def _complex(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {k: (spec.get("properties", []) if isinstance(spec, dict) else spec) for k, spec in v.items()}
        return v


def load_json(data: Mapping[str, Any]) -> Service:
    try:
        spec = _JsonTools.model_validate(data)
    except ValidationError as e:
        raise MetadataError(f"invalid tool details: {e}") from e
    version: Literal["v2", "v4"] | None = None
    if spec.version:
        try:
            version = normalize_version(spec.version)
        except ValueError as e:
            raise MetadataError(str(e)) from e

    enum_types = dict(spec.enum_types)

    def finish(p: Property) -> Property:
        if p.type in spec.complex_types:
            return p.model_copy(update={"kind": "complex"})
        if p.type in enum_types:
            vals = p.values or {m: None for m in enum_types[p.type]}
            return p.model_copy(update={"kind": "enum", "values": vals})
        if not p.type.startswith("Edm."):
            raise MetadataError(f"property {p.name!r} has unknown type {p.type!r}")
        return p

    complex_types = {
        name: ComplexType(name=name, properties=[finish(p) for p in props])
        for name, props in spec.complex_types.items()
    }
    set_type = {s.name: (s.entity_type or s.name) for s in spec.entity_sets}
    type_names = set(set_type.values())  # several sets may share one entity type; the first spec defines it

    entity_types: dict[str, EntityType] = {}
    entity_sets: list[EntitySet] = []
    for s in spec.entity_sets:
        tname = set_type[s.name]
        bindings: dict[str, str] = {}
        navs: list[NavigationProperty] = []
        for n in s.navigation_properties:
            if n.target in set_type:
                target_type = set_type[n.target]
                bindings[n.name] = n.target
            elif n.target in type_names:
                target_type = n.target
            else:
                raise MetadataError(f"navigation property {s.name}.{n.name} targets unknown entity set {n.target!r}")
            many = (
                n.collection if n.collection is not None else (n.multiplicity or "1").strip() in ("*", "many", "0..*")
            )
            navs.append(
                NavigationProperty(
                    name=n.name, target_type=target_type, collection=many, label=n.label, description=n.description
                )
            )
        props = [finish(p) for p in s.properties]
        names = {p.name for p in props}
        missing = [k for k in s.keys if k not in names]
        if missing:
            raise MetadataError(f"entity set {s.name}: key {missing[0]!r} is not one of its properties")
        et = entity_types.get(tname)
        if et is None:
            et = entity_types[tname] = EntityType(
                name=tname,
                keys=s.keys,
                properties=props,
                navigation_properties=navs,
                label=s.label,
                description=s.description,
            )
        entity_sets.append(
            EntitySet(
                name=s.name,
                entity_type=et,
                label=s.label,
                description=s.description,
                searchable=s.searchable,
                addressable=s.addressable,
                pageable=s.pageable,
                countable=s.countable,
                requires_filter=s.requires_filter,
                non_filterable=s.non_filterable,
                non_sortable=s.non_sortable,
                required_in_filter=[p.name for p in props if p.required_in_filter],
                nav_bindings=bindings,
            )
        )
    if not entity_sets:
        raise MetadataError("tool details list no entity sets")
    return Service(
        service_url=spec.service_url,
        version=version,
        entity_sets=entity_sets,
        entity_types=entity_types,
        complex_types=complex_types,
    )


# ====================================================================================== EDMX ($metadata)

SAP_NS = "http://www.sap.com/Protocols/SAPData"
M_NS = "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"

DEFAULT_ALIASES = {
    "Core": "Org.OData.Core.V1",
    "Common": "com.sap.vocabularies.Common.v1",
    "Capabilities": "Org.OData.Capabilities.V1",
    "UI": "com.sap.vocabularies.UI.v1",
}
T_LABEL = "com.sap.vocabularies.Common.v1.Label"
T_QUICKINFO = "com.sap.vocabularies.Common.v1.QuickInfo"
T_DESCRIPTION = "Org.OData.Core.V1.Description"
T_LONG_DESCRIPTION = "Org.OData.Core.V1.LongDescription"
T_FILTER = "Org.OData.Capabilities.V1.FilterRestrictions"
T_SORT = "Org.OData.Capabilities.V1.SortRestrictions"
T_SEARCH = "Org.OData.Capabilities.V1.SearchRestrictions"
T_COUNT = "Org.OData.Capabilities.V1.CountRestrictions"
T_TOP = "Org.OData.Capabilities.V1.TopSupported"
T_SKIP = "Org.OData.Capabilities.V1.SkipSupported"


def _local(el: Any) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _children(el: Any, name: str) -> list[Any]:
    return [c for c in el if _local(c) == name]


def _child(el: Any, name: str) -> Any | None:
    for c in el:
        if _local(c) == name:
            return c
    return None


def _sap(el: Any, name: str) -> str | None:
    return el.get(f"{{{SAP_NS}}}{name}")


def _bool(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    return v.strip().lower() == "true"


def _int(v: str | None) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _record(anns: dict[str, Any], term: str) -> dict[str, Any]:
    v = anns.get(term)
    return v if isinstance(v, dict) else {}


def _ann_value(el: Any) -> Any:
    """Value of an <Annotation> or <PropertyValue>: attribute form, or element form (String, Bool, Record, ...)."""
    for attr in ("String", "Bool", "Int", "Decimal", "EnumMember", "Path", "PropertyPath", "NavigationPropertyPath"):
        v = el.get(attr)
        if v is not None:
            if attr == "Bool":
                return v.strip().lower() == "true"
            if attr == "Int":
                return _int(v)
            return v
    for c in el:
        name = _local(c)
        if not name or name == "Annotation":
            continue
        return _element_value(c)
    return True if _local(el) == "Annotation" else None  # a bare tagging annotation means "true"


def _element_value(c: Any) -> Any:
    name = _local(c)
    if name == "Record":
        return {pv.get("Property"): _ann_value(pv) for pv in _children(c, "PropertyValue")}
    if name == "Collection":
        return [_element_value(x) for x in c if _local(x)]
    if name == "Bool":
        return (c.text or "").strip().lower() == "true"
    if name == "Int":
        return _int((c.text or "").strip())
    return (c.text or "").strip()


class _Edmx:
    def __init__(self, root: Any) -> None:
        self.root = root
        self.aliases: dict[str, str] = dict(DEFAULT_ALIASES)
        self.enums: dict[str, list[str]] = {}
        self.complex_raw: dict[str, Any] = {}
        self.entity_raw: dict[str, Any] = {}
        self.assoc: dict[str, dict[str, tuple[str, str]]] = {}  # association → role → (type, multiplicity)
        self.assoc_sets: list[tuple[str, dict[str, str]]] = []  # (association, role → entity set)
        self.containers: list[tuple[str, Any]] = []  # (qualified container name, element)
        self.annotations: dict[str, list[Any]] = {}  # canonical target → [<Annotation>]
        self.v2_navs: dict[tuple[str, str], tuple[str, str, str]] = {}  # (type, nav) → (assoc, from, to)
        self.types: dict[str, EntityType] = {}
        self.complex: dict[str, ComplexType] = {}

    # ---------------------------------------------------------------- names
    def qualify(self, name: str) -> str:
        name = name.strip()
        if "." not in name:
            return name
        prefix, _, local = name.rpartition(".")
        return f"{self.aliases.get(prefix, prefix)}.{local}"

    def canonical_target(self, target: str) -> str:
        head, _, rest = target.partition("/")
        head = self.qualify(head.split("(")[0])
        return f"{head}/{rest}" if rest else head

    def term(self, el: Any) -> str:
        return self.qualify(el.get("Term", ""))

    def anns(self, target: str, el: Any | None = None) -> dict[str, Any]:
        """Annotations for a target: external <Annotations Target=...> first, inline ones override."""
        out: dict[str, Any] = {}
        for a in self.annotations.get(target, []):
            out[self.term(a)] = _ann_value(a)
        if el is not None:
            for a in _children(el, "Annotation"):
                out[self.term(a)] = _ann_value(a)
        return out

    # ---------------------------------------------------------------- pass 1: collect
    def collect(self) -> Any:
        ds = _child(self.root, "DataServices")
        if ds is None:
            raise MetadataError("EDMX document has no DataServices element")
        for ref in _children(self.root, "Reference"):
            for inc in _children(ref, "Include"):
                if inc.get("Alias") and inc.get("Namespace"):
                    self.aliases[inc.get("Alias")] = inc.get("Namespace")
        schemas = _children(ds, "Schema")
        for s in schemas:
            if s.get("Alias") and s.get("Namespace"):
                self.aliases[s.get("Alias")] = s.get("Namespace")
        for s in schemas:
            ns = s.get("Namespace", "")
            for el in s:
                kind = _local(el)
                qname = f"{ns}.{el.get('Name')}" if el.get("Name") else ""
                if kind == "EnumType":
                    self.enums[qname] = [m.get("Name") for m in _children(el, "Member")]
                elif kind == "ComplexType":
                    self.complex_raw[qname] = el
                elif kind == "EntityType":
                    self.entity_raw[qname] = el
                elif kind == "Association":
                    self.assoc[qname] = {
                        e.get("Role"): (self.qualify(e.get("Type", "")), e.get("Multiplicity", "1"))
                        for e in _children(el, "End")
                    }
                elif kind == "EntityContainer":
                    self.containers.append((qname, el))
                    for aset in _children(el, "AssociationSet"):
                        roles = {e.get("Role"): e.get("EntitySet") for e in _children(aset, "End")}
                        self.assoc_sets.append((self.qualify(aset.get("Association", "")), roles))
                elif kind == "Annotations":
                    target = self.canonical_target(el.get("Target", ""))
                    self.annotations.setdefault(target, []).extend(_children(el, "Annotation"))
        return ds

    # ---------------------------------------------------------------- pass 2: build types
    def prop(self, el: Any, owner: str) -> Property:
        raw_type = el.get("Type", "Edm.String")
        collection = raw_type.startswith("Collection(")
        t = self.qualify(raw_type[len("Collection(") : -1] if collection else raw_type)
        kind: PropertyKind = "primitive"
        values: dict[str, str | None] | None = None
        if t in self.enums:
            kind, values = "enum", {m: None for m in self.enums[t]}
        elif t in self.complex_raw:
            kind = "complex"
        name = el.get("Name")
        a = self.anns(f"{owner}/{name}", el)
        doc = _child(el, "Documentation")
        doc_text = None
        if doc is not None:
            texts = [_child(doc, n) for n in ("Summary", "LongDescription")]
            doc_text = " ".join((t.text or "").strip() for t in texts if t is not None and t.text) or None
        description = a.get(T_DESCRIPTION) or a.get(T_QUICKINFO) or _sap(el, "quickinfo") or doc_text
        label = a.get(T_LABEL) or _sap(el, "label")
        if description and label and description.strip() == label.strip():
            description = None
        return Property(
            name=name,
            type=t,
            collection=collection,
            kind=kind,
            values=values,
            nullable=_bool(el.get("Nullable"), True),
            max_length=_int(el.get("MaxLength")),
            precision=_int(el.get("Precision")),
            scale=_int(el.get("Scale")),
            label=label,
            description=description,
            filterable=_bool(_sap(el, "filterable"), True),
            sortable=_bool(_sap(el, "sortable"), True),
            required_in_filter=_bool(_sap(el, "required-in-filter"), False),
            display_format=_sap(el, "display-format"),
        )

    def build_complex(self) -> None:
        for qname, el in self.complex_raw.items():
            self.complex[qname] = ComplexType(
                name=qname, properties=[self.prop(p, qname) for p in _children(el, "Property")]
            )

    def build_entity(self, qname: str, seen: tuple[str, ...] = ()) -> EntityType:
        if qname in self.types:
            return self.types[qname]
        el = self.entity_raw.get(qname)
        if el is None:
            raise MetadataError(f"unknown entity type {qname!r}")
        if qname in seen:
            raise MetadataError(f"entity type {qname!r} inherits from itself")
        keys: list[str] = []
        props: list[Property] = []
        navs: list[NavigationProperty] = []
        if el.get("BaseType"):
            base = self.build_entity(self.qualify(el.get("BaseType")), (*seen, qname))
            keys, props, navs = list(base.keys), list(base.properties), list(base.navigation_properties)
        key_el = _child(el, "Key")
        if key_el is not None:
            keys = [r.get("Name") for r in _children(key_el, "PropertyRef")]
        props += [self.prop(p, qname) for p in _children(el, "Property")]
        for n in _children(el, "NavigationProperty"):
            navs.append(self.nav(n, qname))
        a = self.anns(qname, el)
        et = EntityType(
            name=qname,
            keys=keys,
            properties=props,
            navigation_properties=navs,
            label=a.get(T_LABEL) or _sap(el, "label"),
            description=a.get(T_DESCRIPTION),
        )
        self.types[qname] = et
        return et

    def nav(self, n: Any, owner: str) -> NavigationProperty:
        name = n.get("Name")
        a = self.anns(f"{owner}/{name}", n)
        label = a.get(T_LABEL) or _sap(n, "label")
        if n.get("Type"):  # V4
            t = n.get("Type")
            many = t.startswith("Collection(")
            target = self.qualify(t[len("Collection(") : -1] if many else t)
        else:  # V2: Relationship + ToRole → Association End
            rel = self.qualify(n.get("Relationship", ""))
            ends = self.assoc.get(rel)
            if not ends or n.get("ToRole") not in ends:
                raise MetadataError(f"navigation property {owner}/{name}: association {rel!r} not found")
            target, mult = ends[n.get("ToRole")]
            many = mult.strip() == "*"
            self.v2_navs[(owner, name)] = (rel, n.get("FromRole", ""), n.get("ToRole", ""))
        return NavigationProperty(
            name=name, target_type=target, collection=many, label=label, description=a.get(T_DESCRIPTION)
        )

    # ---------------------------------------------------------------- pass 3: entity sets
    def build_sets(self) -> list[EntitySet]:
        sets: list[EntitySet] = []
        for cname, container in self.containers:
            for es in _children(container, "EntitySet"):
                tname = self.qualify(es.get("EntityType", ""))
                et = self.build_entity(tname)
                name = es.get("Name")
                a = self.anns(f"{cname}/{name}", es)
                bindings = {
                    b.get("Path"): b.get("Target", "").rpartition("/")[2]
                    for b in _children(es, "NavigationPropertyBinding")
                }
                filt, sort, search, count = (_record(a, t) for t in (T_FILTER, T_SORT, T_SEARCH, T_COUNT))
                searchable = search.get("Searchable") if "Searchable" in search else None
                if _sap(es, "searchable") is not None:
                    searchable = _bool(_sap(es, "searchable"), False)
                non_filterable = list(filt.get("NonFilterableProperties") or [])
                if filt.get("Filterable") is False:
                    non_filterable = [p.name for p in et.properties]
                required = list(filt.get("RequiredProperties") or [])
                required += [p.name for p in et.properties if p.required_in_filter and p.name not in required]
                sets.append(
                    EntitySet(
                        name=name,
                        entity_type=et,
                        label=a.get(T_LABEL) or _sap(es, "label"),
                        description=a.get(T_DESCRIPTION),
                        searchable=searchable,
                        addressable=_bool(_sap(es, "addressable"), True),
                        pageable=_bool(_sap(es, "pageable"), True)
                        and a.get(T_TOP, True) is not False
                        and a.get(T_SKIP, True) is not False,
                        countable=_bool(_sap(es, "countable"), True) and count.get("Countable", True) is not False,
                        requires_filter=_bool(_sap(es, "requires-filter"), False) or bool(filt.get("RequiresFilter")),
                        non_filterable=non_filterable,
                        non_sortable=list(sort.get("NonSortableProperties") or []),
                        required_in_filter=required,
                        nav_bindings=bindings,
                    )
                )
        # V2: AssociationSets bind navigation properties to entity sets
        by_name = {s.name: s for s in sets}
        for s in sets:
            for nav in s.navigation_properties:
                info = self.v2_navs.get((s.entity_type.name, nav.name))
                if info is None:
                    continue
                rel, from_role, to_role = info
                for assoc, roles in self.assoc_sets:
                    if assoc == rel and roles.get(from_role) == s.name and roles.get(to_role) in by_name:
                        s.nav_bindings[nav.name] = roles[to_role]
                        break
        return sets


def parse_edmx(source: str | bytes | os.PathLike[str], *, service_url: str | None = None) -> Service:
    """Parse a $metadata document (V2 or V4). External entities and network access are disabled."""
    data = source if isinstance(source, bytes) else _read_source(source).encode("utf-8")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True, huge_tree=True)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as e:
        raise MetadataError(f"invalid $metadata XML: {e}") from e
    if _local(root) != "Edmx":
        raise MetadataError(f"not an EDMX document (root element is {_local(root)!r})")
    edmx_version = root.get("Version", "")
    version: Literal["v2", "v4"] = "v4" if edmx_version.startswith("4") else "v2"
    doc = _Edmx(root)
    doc.collect()
    doc.build_complex()
    for qname in doc.entity_raw:
        doc.build_entity(qname)
    sets = doc.build_sets()
    if not sets:
        raise MetadataError("$metadata declares no entity sets")
    return Service(
        service_url=service_url,
        version=version,
        entity_sets=sets,
        entity_types=doc.types,
        complex_types=doc.complex,
        aliases=doc.aliases,
    )
