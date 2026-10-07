"""哥哥，科研契约只允许有限的真实动作，不接受文字解锁或任意代码。"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import TypeAliasType

from world_engine.collective_types import CollectiveCommand, CollectiveProgram, Hypothesis
from world_engine.research_types import ResearchProgram

Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Identifier = Annotated[str, Field(min_length=1, max_length=100)]
Role = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,39}$")]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
MethodReference = TypeAliasType(
    "MethodReference",
    Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            pattern=r"^(?:[a-zA-Z][a-zA-Z0-9_]{0,39}|\$[a-zA-Z][a-zA-Z0-9_]{0,39}:[0-9]+)$",
            description=(
                "方法内引用必须是inputs声明的角色，或先前步骤的$结果键:产物序号。"
                "序号为0至15；$结果键缺少序号无效。禁止使用现场实物id。"
            ),
        ),
    ],
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ParameterSpec(StrictModel):
    minimum: Number
    maximum: Number
    unit: str = Field(min_length=1, max_length=40)
    default: Number | None = None

    @model_validator(mode="after")
    def bounds(self):
        if self.minimum > self.maximum:
            raise ValueError("参数范围须有序")
        if self.default is not None and not self.minimum <= self.default <= self.maximum:
            raise ValueError("默认参数超出适用范围")
        return self


class CraftStep(StrictModel):
    op: Literal[
        "wash",
        "heat",
        "dry",
        "screen",
        "filter",
        "adsorb",
        "mix",
        "bind",
        "settle",
        "store",
        "form",
        "compact",
        "cool",
    ]
    input: Identifier
    hours: Annotated[Number, Field(gt=0, le=5760)] = 1
    quantity: Annotated[PositiveInt, Field(le=1000000)] = 1
    settings: dict[Role, Number | str] = Field(default_factory=dict, max_length=16)
    auxiliary_inputs: list[Identifier] = Field(default_factory=list, max_length=8)
    apparatus: Identifier | None = None
    energy: Identifier | None = Field(
        default=None,
        description=(
            "能量需求大于0时必须提供真实有限能源。form/compact/dry同样可能需要能源，"
            "不能只给heat填写。直接craft绑定现场energy_stocks.item_id；"
            "method步骤绑定inputs中声明的energy角色。需求为0才可留空。"
        ),
    )
    result_key: Role | None = None


class AssayStep(StrictModel):
    op: Literal["assay"]
    input: Identifier
    metric: str = Field(min_length=1, max_length=60)
    instrument: Identifier
    result_key: Role | None = None


class MethodCraftStep(CraftStep):
    input: MethodReference
    auxiliary_inputs: list[MethodReference] = Field(default_factory=list, max_length=8)
    apparatus: MethodReference | None = None
    energy: MethodReference | None = None


class MethodAssayStep(AssayStep):
    input: MethodReference
    instrument: MethodReference


class CraftCommand(CraftStep):
    op: Literal["craft"]
    operation: Literal[
        "wash",
        "heat",
        "dry",
        "screen",
        "filter",
        "adsorb",
        "mix",
        "bind",
        "settle",
        "store",
        "form",
        "compact",
        "cool",
    ]


class ResonanceCommand(StrictModel):
    op: Literal["resonance"]
    program: ResearchProgram
    result_key: Role | None = None


class MethodCallStep(StrictModel):
    op: Literal["use_method"]
    technology_id: Identifier
    version: PositiveInt
    bindings: dict[Role, MethodReference] = Field(min_length=1, max_length=16)
    parameters: dict[Role, Number | str] = Field(default_factory=dict, max_length=16)
    result_key: Role | None = None


MethodStep = Annotated[
    MethodCraftStep | MethodAssayStep | MethodCallStep, Field(discriminator="op")
]


class MethodSpec(StrictModel):
    inputs: dict[Role, Literal["material", "instrument", "apparatus", "energy"]] = Field(
        min_length=1, max_length=16
    )
    parameters: dict[Role, ParameterSpec] = Field(default_factory=dict, max_length=16)
    steps: list[MethodStep] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def references(self):
        available: set[str] = set()

        def resource(ref):
            if ref in self.inputs:
                return
            if ref.startswith("$") and ":" in ref:
                key, index = ref[1:].rsplit(":", 1)
                if key in available and index.isdigit() and int(index) < 16:
                    return
            raise ValueError(
                f"方法引用{ref!r}无效：须为inputs已声明角色或先前$结果键:产物序号（0至15）"
            )

        for step in self.steps:
            if isinstance(step, MethodCallStep):
                refs, settings = step.bindings.values(), step.parameters
            else:
                refs = [step.input]
                settings = step.settings if isinstance(step, CraftStep) else {}
                if isinstance(step, CraftStep):
                    refs += step.auxiliary_inputs
                    refs += [x for x in (step.apparatus, step.energy) if x is not None]
                else:
                    refs += [step.instrument]
                    if self.inputs.get(step.instrument) != "instrument":
                        raise ValueError("检验必须绑定真实量器 instrument 角色")
            for ref in refs:
                resource(ref)
            for value in settings.values():
                if isinstance(value, str) and (
                    not value.startswith("param:") or value[6:] not in self.parameters
                ):
                    raise ValueError("方法参数引用尚未定义")
            if step.result_key:
                if step.result_key in available:
                    raise ValueError("方法结果标识不能重复")
                available.add(step.result_key)
        return self


class ClaimSpec(StrictModel):
    metric: str = Field(min_length=1, max_length=60)
    direction: Literal["increase", "decrease"]
    minimum_delta: Annotated[Number, Field(gt=0)]
    conditions: dict[str, Number | str] = Field(default_factory=dict, max_length=24)
    comparator_role: Role


class DeclareCommand(StrictModel):
    op: Literal["declare"]
    name: str = Field(min_length=1, max_length=120)
    problem: str = Field(min_length=1, max_length=2400)
    claim: ClaimSpec
    method: MethodSpec
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=40)
    result_key: Role | None = None


class ReviseClaimCommand(DeclareCommand):
    op: Literal["revise_claim"]
    candidate_id: Identifier
    revision: PositiveInt


class ValidateCommand(StrictModel):
    op: Literal["validate"]
    candidate_id: Identifier
    revision: PositiveInt
    bindings: dict[Role, Identifier] = Field(min_length=1, max_length=16)
    control_bindings: dict[Role, Identifier] = Field(min_length=1, max_length=16)
    parameters: dict[Role, Number] = Field(default_factory=dict, max_length=16)
    result_key: Role | None = None


class UseMethodCommand(StrictModel):
    op: Literal["use_method"]
    technology_id: Identifier
    version: PositiveInt
    bindings: dict[Role, Identifier] = Field(min_length=1, max_length=16)
    parameters: dict[Role, Number] = Field(default_factory=dict, max_length=16)
    result_key: Role | None = None


class ShareMethodCommand(StrictModel):
    op: Literal["share_method"]
    technology_id: Identifier
    version: PositiveInt
    recipients: list[Identifier] = Field(min_length=1, max_length=32)
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=40)
    text: str = Field(default="", max_length=4000)


class InvalidateMethodCommand(StrictModel):
    op: Literal["invalidate_method"]
    technology_id: Identifier
    version: PositiveInt
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=2400)


# 哥哥，旧实物动作沿用原有约束，新科研声明各有独立的严格结构。
ScienceCommand = Annotated[
    CollectiveCommand
    | DeclareCommand
    | ReviseClaimCommand
    | ValidateCommand
    | UseMethodCommand
    | ShareMethodCommand
    | InvalidateMethodCommand
    | CraftCommand
    | AssayStep
    | ResonanceCommand,
    Field(discriminator="op"),
]


class ScienceProgram(StrictModel):
    reason: str = Field(min_length=1, max_length=2400)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=8)
    commands: list[ScienceCommand] = Field(default_factory=list, max_length=24)

    @model_validator(mode="before")
    @classmethod
    def finite_native_settings(cls, value):
        if isinstance(value, dict) and isinstance(value.get("commands", []), list):
            for command in value.get("commands", []):
                if not isinstance(command, dict) or not isinstance(
                    command.get("settings", {}), dict
                ):
                    continue
                for setting in command.get("settings", {}).values():
                    if (
                        isinstance(setting, bool)
                        or not isinstance(setting, (int, float))
                        or not math.isfinite(setting)
                    ):
                        raise ValueError("材料处理参数须为有限数值")
        return value

    @model_validator(mode="after")
    def experimental_predictions(self):
        original = [x.model_dump() for x in self.commands if isinstance(x, CollectiveCommand)]
        # 逐条验证不受旧程序12条上限影响；整个科研程序仍有24条硬边界。
        for command in original:
            CollectiveProgram.model_validate(
                {
                    "reason": self.reason,
                    "hypotheses": [x.model_dump() for x in self.hypotheses],
                    "commands": [command],
                }
            )
        keys = [getattr(x, "result_key", None) for x in self.commands]
        keys = [x for x in keys if x is not None]
        if len(set(keys)) != len(keys):
            raise ValueError("程序结果标识不可重复")
        return self
