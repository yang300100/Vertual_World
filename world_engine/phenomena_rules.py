"""哥哥，可组合的中层自然机制只读取物品状态与操作，不读取假设或科技名称。"""

import ast
import hashlib
import inspect
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from world_engine.craft_physics import CRAFT_CONTROLS, CRAFT_METRICS, assay_craft, process_craft
from world_engine.material_physics import EXTENSIVE, MaterialState


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def finite(value, name, *, minimum=0):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < minimum
    ):
        raise ValueError(f"{name}必须为符合范围的有限数值")
    return value


@dataclass(frozen=True)
class RuleSpec:
    key: str
    domain: str
    version: int
    operations: tuple[str, ...]
    metrics: tuple[str, ...]
    properties: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class RuleOutcome:
    status: str
    rule_key: str
    version: int
    outputs: tuple
    public_facts: tuple[dict, ...]
    audit: dict
    reason: str = ""


class RuleRegistry:
    """哥哥，新机制通过可信代码注册；运行时不接受模型提供的公式或回调。"""

    def __init__(self):
        self._rules: dict[tuple[str, int], tuple[RuleSpec, Callable]] = {}
        self._implementations = {}
        self._frozen = False

    def register(self, spec, evaluator):
        if self._frozen:
            raise ValueError("执行期机制注册表已冻结，扩展须在下一版本启动前登记")
        if (
            not isinstance(spec, RuleSpec)
            or isinstance(spec.version, bool)
            or not isinstance(spec.version, int)
            or spec.version < 1
            or not callable(evaluator)
        ):
            raise ValueError("机制须有固定版本与可信执行器")
        identity = (spec.key, spec.version)
        if identity in self._rules:
            raise ValueError("机制名称已注册，不能在运行中替换规则")
        try:
            implementation = fingerprint(inspect.getsource(evaluator))
        except (TypeError, OSError) as exc:
            raise ValueError("执行器须有可核验的可信源码，注册失败不留下半注册机制") from exc
        self._rules[identity] = (spec, evaluator)
        self._implementations[identity] = implementation

    def freeze(self):
        self._frozen = True

    def specification(self, key, version=None):
        versions = [v for k, v in self._rules if k == key]
        selected = version if version is not None else max(versions, default=0)
        return self._rules.get((key, selected))

    def evaluate(self, key, operation, *, version=None, **context):
        entry = self.specification(key, version)
        if entry is None or operation not in entry[0].operations:
            return RuleOutcome(
                "unsupported",
                key,
                entry[0].version if entry else 0,
                (),
                (),
                {},
                "引擎尚未覆盖该机制或操作；这不是实验失败，也不产生证据",
            )
        outcome = entry[1](entry[0], operation, **context)
        if (
            not isinstance(outcome, RuleOutcome)
            or outcome.rule_key != entry[0].key
            or outcome.version != entry[0].version
            or outcome.status not in {"computed", "unsupported"}
        ):
            raise ValueError("执行器结果必须对应固定机制和版本")
        if outcome.status == "unsupported" and (outcome.outputs or outcome.public_facts):
            raise ValueError("未覆盖的机制不能返回产物或现象")
        if any(
            fact.get("channel") not in {"action", "sight", "instrument", "device"}
            for fact in outcome.public_facts
        ):
            raise ValueError("现象必须声明可验证的观察通道")
        canonical(outcome.audit)
        canonical(outcome.public_facts)
        return outcome

    def capabilities(self):
        return [asdict(value[0]) for _, value in sorted(self._rules.items())]

    def public_capabilities(self):
        return [
            {k: v for k, v in spec.items() if k != "properties"} for spec in self.capabilities()
        ]


def _material(spec, operation, *, state, settings, auxiliaries=(), hours, supplied_energy, profile):
    outputs = process_craft(
        state, operation, settings, auxiliaries, hours, supplied_energy, profile
    )
    facts = [
        {
            "channel": "action",
            "code": "processing_completed",
            "text": "实际材料处理已结算，具体性能须通过观察或量器检验。",
        }
    ]
    if len(outputs) > 1:
        facts.append(
            {
                "channel": "sight",
                "code": "separated_outputs",
                "text": "处理后得到分离的主产物和副产物。",
            }
        )
    if outputs[0].geometry_ratio != state.geometry_ratio:
        facts.append(
            {
                "channel": "sight",
                "code": "shape_changed",
                "text": "材料成形后的几何外形发生了变化。",
            }
        )
    # 哥哥，内部孔隙、结合强度、精确含水量和温度不直接当成肉眼读数。
    audit = {
        "before": [asdict(s) for s in (state, *auxiliaries)],
        "after": [asdict(s) for s in outputs],
        "operation": operation,
        "settings": settings,
        "hours": hours,
        "supplied_energy": supplied_energy,
        "profile": profile,
        "conserved_properties": list(EXTENSIVE),
    }
    return RuleOutcome("computed", spec.key, spec.version, outputs, tuple(facts), audit)


def _assay(spec, operation, *, state, metric, instrument, profile):
    if metric not in spec.metrics:
        return RuleOutcome(
            "unsupported", spec.key, spec.version, (), (), {}, "该量测机制尚不支持此指标"
        )
    if metric in CRAFT_METRICS:
        measured = assay_craft(state, metric, instrument, profile)
    else:
        from world_engine.collective_research import CollectiveResearchService

        precision = finite(instrument["precision"], "量器精度", minimum=1e-12)
        detection = finite(instrument["detection_limit"], "检测限")
        raw = CollectiveResearchService._measurement(state, metric)
        measured = {
            "value": None if raw < detection else round(raw / precision) * precision,
            "unit": instrument.get("unit", "relative"),
            "precision": precision,
            "detection_limit": detection,
        }
    fact = {
        "channel": "instrument",
        "code": "measurement",
        "metric": metric,
        **measured,
        "text": "实际量器完成检验；读数受精度和检测限约束。",
    }
    return RuleOutcome(
        "computed",
        spec.key,
        spec.version,
        (measured,),
        (fact,),
        {
            "sample": asdict(state),
            "metric": metric,
            "instrument": dict(instrument),
            "profile": profile,
        },
    )


def charge_band(stored, capacity):
    finite(stored, "储能")
    finite(capacity, "容量", minimum=1e-12)
    if stored > capacity:
        raise ValueError("储能不能超过实际容量")
    return "充盈" if stored / capacity >= 0.7 else "尚有余量" if stored > 0 else "耗尽"


def _magic(spec, operation, *, source, pattern, minutes, factor, receiver=None):
    finite(minutes, "供能活动分钟", minimum=1e-12)
    finite(factor, "能力倍率", minimum=1e-12)
    if pattern["operation"] != operation:
        raise ValueError("完整魔纹的固定功能不匹配")
    for device in (source, receiver):
        if device is not None:
            charge_band(device["stored"], device["capacity"])
            finite(device["output_per_minute"], "器物输出", minimum=1e-12)
    for key in ("base_cost", "useful_output", "base_efficiency", "max_efficiency"):
        finite(pattern[key], "魔纹参数", minimum=1e-12)
    if not 0 < pattern["base_efficiency"] <= pattern["max_efficiency"] < 1:
        raise ValueError("魔力转移必须有损且受效率上限约束")
    if pattern["base_cost"] * pattern["max_efficiency"] < pattern["useful_output"]:
        raise ValueError("魔纹输入和输出参数不满足能量边界")
    if operation == "cast":
        if receiver is not None or not 0.6 <= factor <= 0.98:
            raise ValueError("单纹供能契约不匹配")
        spent = max(
            pattern["base_cost"] * factor, pattern["useful_output"] / pattern["max_efficiency"]
        )
        if spent > source["stored"] or spent > source["output_per_minute"] * minutes:
            raise ValueError("实际器物的储能或输出不足")
        output, before, after = pattern["useful_output"], pattern["base_cost"], spent
        receiver_after = None
    else:
        if receiver is None or not 1.02 <= factor <= 1.4:
            raise ValueError("储能转移契约不匹配")
        if source["item_id"] == receiver["item_id"]:
            raise ValueError("禁止同一器物自充循环")
        efficiency = min(pattern["max_efficiency"], pattern["base_efficiency"] * factor)
        spent = min(
            source["stored"],
            source["output_per_minute"] * minutes,
            (receiver["capacity"] - receiver["stored"]) / efficiency,
        )
        if spent <= 0:
            raise ValueError("没有可转移能量或接收容量")
        output, before, after = spent * efficiency, pattern["base_efficiency"], efficiency
        receiver_after = receiver["stored"] + output
    result = {
        "spent": spent,
        "output": output,
        "loss": spent - output,
        "source_after": max(0, source["stored"] - spent),
        "receiver_after": receiver_after,
        "baseline": before,
        "actual": after,
    }
    facts = [
        {
            "channel": "device",
            "code": "source_charge",
            "item_id": source["item_id"],
            "before": charge_band(source["stored"], source["capacity"]),
            "after": charge_band(result["source_after"], source["capacity"]),
            "text": "已知单纹供能完成，器物反馈当前储能档位。",
        }
    ]
    if receiver:
        facts.append(
            {
                "channel": "device",
                "code": "receiver_charge",
                "item_id": receiver["item_id"],
                "before": charge_band(receiver["stored"], receiver["capacity"]),
                "after": charge_band(receiver_after, receiver["capacity"]),
                "text": "相容接收器物完成能量接收，反馈当前储能档位。",
            }
        )
    return RuleOutcome(
        "computed",
        spec.key,
        spec.version,
        (result,),
        tuple(facts),
        {
            "source": dict(source),
            "receiver": dict(receiver) if receiver else None,
            "pattern": dict(pattern),
            "minutes": minutes,
            "factor": factor,
            "result": result,
        },
    )


REGISTRY = RuleRegistry()
REGISTRY.register(
    RuleSpec(
        "material_processing",
        "physical",
        1,
        tuple(sorted(CRAFT_CONTROLS)),
        (),
        tuple(MaterialState.__dataclass_fields__),
        "材料的热、结构、组分与分离响应；真实操作仍需资源和工时。",
    ),
    _material,
)
REGISTRY.register(
    RuleSpec(
        "material_measurement",
        "physical",
        1,
        ("assay",),
        tuple(
            sorted(
                CRAFT_METRICS
                | {"extractable_salt", "particles", "moisture", "germination", "soil_ph"}
            )
        ),
        tuple(MaterialState.__dataclass_fields__),
        "真实量器读取有限指标，精度与检测限决定可见读数。",
    ),
    _assay,
)
REGISTRY.register(
    RuleSpec(
        "magic_energy",
        "magic",
        1,
        ("cast", "transfer"),
        ("charge_band",),
        ("stored", "capacity", "output_per_minute", "coupling"),
        "已知完整单纹与相容器物的有限供能、有损转移及档位反馈。",
    ),
    _magic,
)
REGISTRY.freeze()

# 哥哥，每个执行器单独固定源码；新增无关机制不会替换旧机制的指纹。
PHYSICAL_CORE_HASH = fingerprint(
    {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in (
            "craft_physics.py",
            "material_physics.py",
        )
    }
)


def _legacy_measurement_hash():
    source = Path(__file__).with_name("collective_research.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    owner = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "CollectiveResearchService"
    )
    method = next(
        n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == "_measurement"
    )
    return fingerprint(ast.dump(method, include_attributes=False))


SUPPORT_HASHES = {
    "material_measurement": fingerprint(
        {"legacy_measurement": _legacy_measurement_hash(), "finite": inspect.getsource(finite)}
    ),
    "magic_energy": fingerprint(
        {"charge_band": inspect.getsource(charge_band), "finite": inspect.getsource(finite)}
    ),
}


def rule_definition(outcome):
    entry = REGISTRY.specification(outcome.rule_key, outcome.version)
    if entry is None:
        raise ValueError("结算机制的指定版本尚未注册")
    spec = asdict(entry[0])
    return {
        "spec": spec,
        "implementation_hash": REGISTRY._implementations[(outcome.rule_key, outcome.version)],
        "physical_core_hash": PHYSICAL_CORE_HASH if spec["domain"] == "physical" else None,
        "support_functions_hash": SUPPORT_HASHES.get(outcome.rule_key),
        "profile": outcome.audit.get("profile"),
        "pattern": {
            k: outcome.audit["pattern"][k]
            for k in (
                "id",
                "operation",
                "coupling_key",
                "base_cost",
                "useful_output",
                "base_efficiency",
                "max_efficiency",
            )
        }
        if outcome.rule_key == "magic_energy"
        else None,
    }
