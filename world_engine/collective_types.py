"""哥哥，人物可提交分阶段研究与交流，契约不包含灾难处理路线。"""

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Prediction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str = Field(min_length=1, max_length=60)
    low: float
    high: float

    @model_validator(mode="after")
    def validate_interval(self):
        if not math.isfinite(self.low) or not math.isfinite(self.high) or self.low > self.high:
            raise ValueError("预测须是有序的有限范围")
        return self


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    explanation: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=40)
    predictions: list[Prediction] = Field(min_length=1, max_length=12)


class CollectiveCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal[
        "inspect",
        "experiment",
        "propose",
        "accept",
        "revise",
        "send",
        "produce",
        "plant",
        "observe",
        "wait",
        "collect",
        "reject",
        "start",
        "rest",
        "harvest",
        "ration",
        "land",
        "gather",
        "diet",
        "dispose",
        "maintain",
        "move",
        "give",
        "grant_region",
        "water",
    ]
    batch_id: str | None = None
    quantity: int = Field(default=1, ge=1, le=1000000, strict=True)
    method: str | None = None
    instrument_id: str | None = None
    apparatus_id: str | None = None
    hypothesis_index: int | None = Field(default=None, ge=0)
    operation: str | None = None
    settings: dict[str, float] = Field(default_factory=dict)
    auxiliary_ids: list[str] = Field(default_factory=list, max_length=8)
    auxiliary_quantities: dict[str, Annotated[int, Field(strict=True, ge=1)]] = Field(
        default_factory=dict
    )
    control_batch_id: str | None = None
    hours: float = Field(default=1, ge=0.25, le=5760)
    recipients: list[str] = Field(default_factory=list, max_length=100)
    text: str = Field(default="", max_length=4000)
    proposal_id: str | None = None
    version: int | None = Field(default=None, ge=1)
    tasks: list[dict] = Field(default_factory=list, max_length=12)
    task_keys: list[str] = Field(default_factory=list, max_length=12)
    resource_task_keys: list[str] = Field(default_factory=list, max_length=12)
    evidence_ids: list[str] = Field(default_factory=list, max_length=40)
    participants: list[str] = Field(default_factory=list, max_length=12)
    region_id: str | None = None
    result_key: str | None = Field(default=None, pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,39}$")
    crop_id: str | None = None
    food_policy: Literal["allow", "avoid", "reserve"] | None = None
    site_id: str | None = None
    location_id: str | None = None
    permissions: list[
        Literal["ration", "plant", "harvest", "land", "water", "gather", "dispose", "maintain"]
    ] = Field(default_factory=list, max_length=8)
    repeat_count: int = Field(default=1, ge=1, le=240, strict=True)
    repeat_interval_hours: float = Field(default=24, ge=1, le=2160)

    @model_validator(mode="after")
    def validate_processing_settings(self):
        if self.settings and (
            self.op not in {"produce", "experiment", "observe"} or not self.operation
        ):
            raise ValueError("本动作不执行材料处理，settings 参数不会生效，不能提交")
        return self


class CollectiveProgram(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2400)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=8)
    commands: list[CollectiveCommand] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_experimental_prediction(self):
        for command in self.commands:
            if command.repeat_count > 1 and command.op not in {"gather", "dispose", "maintain"}:
                raise ValueError("只有明确的采集、转运和维护劳动可以重复；研究判断不能机械复制")
            if command.hypothesis_index is not None and command.hypothesis_index >= len(
                self.hypotheses
            ):
                raise ValueError("引用的假说和预测不存在")
            if any(isinstance(n, bool) or n < 1 for n in command.auxiliary_quantities.values()):
                raise ValueError("辅助材料数量须为正整数")
            if command.op == "experiment":
                if command.hypothesis_index is None or command.hypothesis_index >= len(
                    self.hypotheses
                ):
                    raise ValueError("实验须引用事先登记的假说和预测")
                if not command.batch_id or not command.method or not command.instrument_id:
                    raise ValueError("实验须指定实际批次、测量方法与器具")
                prediction = self.hypotheses[command.hypothesis_index]
                if not any(p.metric == command.method for p in prediction.predictions):
                    raise ValueError("实验指标必须有事先预测")
        keys = [x.result_key for x in self.commands if x.result_key]
        if len(set(keys)) != len(keys):
            raise ValueError("程序结果标识不可重复")
        return self
