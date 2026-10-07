"""人物可以自拟的通用实验程序；不能在程序中写入结论。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResearchCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["measure", "experiment", "configure", "send", "wait"]
    settings: list[dict[str, float]] = Field(default_factory=list, max_length=40)
    select: Literal["lowest_noise", "highest_service"] | int | None = None
    minimum_service: float = Field(default=0, ge=0, le=1.2)
    recipients: Literal["contacts"] | list[str] = Field(default_factory=list, max_length=100)
    text: str = Field(default="", max_length=800)
    share_program: bool = False
    hours: float = Field(default=1, ge=0.25, le=2400)

    @model_validator(mode="before")
    @classmethod
    def normalize_syntax(cls, value):
        """哥哥，只兼容等价写法；不补动作、不改人物给出的参数。"""
        if not isinstance(value, dict):
            return value
        value = dict(value)
        if "op" not in value and "action" in value:
            value["op"] = value.pop("action")
        if value.get("op") == "configure" and isinstance(value.get("settings"), dict):
            value["settings"] = [value["settings"]]
        return value

    @model_validator(mode="after")
    def validate_operation(self):
        if self.op == "experiment" and not self.settings:
            raise ValueError("实验须由人物指定实际参数")
        if self.op == "configure" and bool(self.settings) == (self.select is not None):
            raise ValueError("调节须指定一组参数，或指定已有实验的选择标准")
        if self.op == "configure" and len(self.settings) > 1:
            raise ValueError("运行装置一次只能安装一组参数")
        return self


class ResearchProgram(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=600)
    commands: list[ResearchCommand] = Field(default_factory=list, max_length=12)
