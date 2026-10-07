"""哥哥，仅压缩科研契约的表示；真实程序仍由原Pydantic模型严格校验。"""

from collections import Counter
from copy import deepcopy
from string import ascii_letters, digits

from world_engine.materials import encoded

SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "dependentSchemas"}
SCHEMA_ARRAYS = {"anyOf", "oneOf", "allOf", "prefixItems"}
SCHEMA_CHILDREN = {
    "items",
    "additionalProperties",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
    "contains",
    "unevaluatedProperties",
    "unevaluatedItems",
    "contentSchema",
}


def _walk(node, visit):
    if not isinstance(node, dict):
        return node
    result = {}
    for key, value in node.items():
        # 展示标题和OpenAPI类型映射不参与JSON Schema验证；op约束和所有描述保留。
        if key in {"title", "discriminator"}:
            continue
        if key in SCHEMA_MAPS and isinstance(value, dict):
            result[key] = {name: visit(child) for name, child in value.items()}
        elif key in SCHEMA_ARRAYS and isinstance(value, list):
            result[key] = [visit(child) for child in value]
        elif key in SCHEMA_CHILDREN and isinstance(value, (dict, bool)):
            result[key] = visit(value)
        else:
            # required、enum、default、description都是数据，不递归处理其中的键。
            result[key] = value
    return result


def compact_prompt_schema(schema):
    """共享本项目生成的本地schema约束，并稳定缩短内部定义名；不改写动作字段。"""
    original = deepcopy(schema)
    root_definitions = original.get("$defs", {})

    def check_scope(node, *, root=False):
        if not isinstance(node, dict):
            return node
        if any(key in node for key in {"$id", "$anchor", "$dynamicRef", "$dynamicAnchor"}):
            raise ValueError("科研提示契约仅支持无独立作用域的本地定义")
        if "$defs" in node and not root:
            raise ValueError("科研提示契约不能移动嵌套定义作用域")
        if "$ref" in node and (
            not node["$ref"].startswith("#/$defs/") or node["$ref"][8:] not in root_definitions
        ):
            raise ValueError("科研提示契约引用未定义或外部资源")
        return _walk(node, check_scope)

    stripped = check_scope(original, root=True)
    counts = Counter()
    nodes = {}

    def count(node):
        if isinstance(node, dict):
            key = encoded(node)
            counts[key] += 1
            nodes[key] = node
            _walk(node, count)
        return node

    count(stripped)
    occupied = set(root_definitions)
    aliases = {}
    for key, repeats in sorted(counts.items()):
        # 只共享能节省文本的真实schema节点，字段映射和默认数据不会进入此表。
        if repeats <= 1 or (repeats - 1) * len(key.encode("utf-8")) <= repeats * 26 + 10:
            continue
        name = "R" + str(len(aliases))
        while name in occupied:
            name = "_" + name
        occupied.add(name)
        aliases[key] = name

    def factor(node, *, own_key=None):
        if not isinstance(node, dict):
            return node
        key = encoded(node)
        if key in aliases and key != own_key:
            return {"$ref": "#/$defs/" + aliases[key]}
        return _walk(node, factor)

    result = factor(stripped)
    definitions = result.setdefault("$defs", {})
    definitions.update({name: factor(nodes[key], own_key=key) for key, name in aliases.items()})
    needed = set()
    frequency = Counter()

    def referenced(node):
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"][8:]
                frequency[name] += 1
                if name not in needed:
                    needed.add(name)
                    referenced(definitions[name])
            _walk({key: value for key, value in node.items() if key != "$defs"}, referenced)
        return node

    referenced(result)
    alphabet = ascii_letters + digits
    ordered = sorted(needed, key=lambda name: (-frequency[name], name))
    names = {
        name: alphabet[index] if index < len(alphabet) else "D" + str(index)
        for index, name in enumerate(ordered)
    }
    result["$defs"] = {name: definitions[name] for name in ordered}

    def rename(node):
        if not isinstance(node, dict):
            return node
        out = _walk(node, rename)
        if "$ref" in out:
            out["$ref"] = "#/$defs/" + names[out["$ref"][8:]]
        if "$defs" in out:
            out["$defs"] = {names[name]: value for name, value in out["$defs"].items()}
        return out

    return rename(result)
