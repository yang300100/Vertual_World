"""哥哥，人物提出用途与原理，独立评审把达成的科技绑定到可结算能力。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PrincipleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class MagicPlan(PrincipleModel):
    pattern_id: str = Field(min_length=1, max_length=100)
    source_item_id: str = Field(min_length=1, max_length=100)
    receiver_item_id: str | None = Field(default=None, min_length=1, max_length=100)


class InventionDesign(PrincipleModel):
    name: str = Field(min_length=2, max_length=100)
    principle: str = Field(min_length=20, max_length=2000)
    requirements: list[str] = Field(min_length=1, max_length=12)
    limitations: str = Field(min_length=8, max_length=1500)
    application: str = Field(min_length=8, max_length=1500)
    adopt_if_approved: bool = True
    magic_plan: MagicPlan | None = None


class TechnologyConnection(PrincipleModel):
    technology_id: str = Field(min_length=1, max_length=100)
    kind: Literal["requires", "improves", "complements"]
    explanation: str = Field(min_length=12, max_length=1500)


class ConnectionReview(TechnologyConnection):
    approved: bool


class TechnologyIdea(PrincipleModel):
    name: str = Field(min_length=2, max_length=100)
    problem: str = Field(min_length=8, max_length=1500)
    principle: str = Field(min_length=40, max_length=4000)
    requirements: list[str] = Field(min_length=1, max_length=12)
    limitations: str = Field(min_length=8, max_length=1500)
    parents: list[str] = Field(default_factory=list, max_length=6)
    application: str = Field(min_length=8, max_length=1500)
    adopt_if_approved: bool
    level: Literal["foundation", "technique", "invention"] = "invention"
    unlocks: list[str] = Field(default_factory=list, max_length=8)
    inventions: list[InventionDesign] = Field(default_factory=list, max_length=3)
    connections: list[TechnologyConnection] = Field(default_factory=list, max_length=6)
    domain: Literal["physical", "magic", "hybrid"] = "physical"

    @model_validator(mode="after")
    def separate_technology_and_inventions(self):
        if len(set(self.parents)) != len(self.parents):
            raise ValueError("前置技术不能重复")
        if self.level != "invention" and (not self.unlocks or not self.inventions):
            raise ValueError("基础技术须说明可复用能力，并把具体应用放入衍生发明")
        if len({(r.technology_id, r.kind) for r in self.connections}) != len(self.connections):
            raise ValueError("同一种技术联系不能重复")
        if len({i.name for i in self.inventions}) != len(self.inventions):
            raise ValueError("衍生发明名称不能重复")
        if self.domain == "physical" and any(i.magic_plan for i in self.inventions):
            raise ValueError("使用魔纹与储能的发明须标为魔法或魔工结合")
        if self.level != "invention":
            required = {r.technology_id for r in self.connections if r.kind == "requires"}
            if required != set(self.parents):
                raise ValueError("每项必需前置都须解释因果联系，不能只填连线")
        return self


class IdeaBatch(PrincipleModel):
    reason: str = Field(min_length=8, max_length=2000)
    ideas: list[TechnologyIdea] = Field(default_factory=list, max_length=3)


class CapabilityEffect(PrincipleModel):
    kind: Literal[
        "labor_time",
        "labor_energy",
        "food_nutrition",
        "material_strength",
        "craft_precision",
        "magic_energy_cost",
        "magic_transfer_efficiency",
    ]
    activity: Literal["work", "food", "craft", "magic"]
    factor: float = Field(gt=0, le=1.4)
    explanation: str = Field(min_length=12, max_length=1500)
    required_skill: str | None
    carrier: Literal["knowledge", "tool", "food_process"]
    materials: list["ApplicationMaterial"] = Field(default_factory=list, max_length=4)
    location_kinds: list[str] = Field(default_factory=list, max_length=6)
    magic_plan: MagicPlan | None = None

    @model_validator(mode="after")
    def bounded_effect(self):
        if self.kind in {"magic_energy_cost", "magic_transfer_efficiency"}:
            if (
                self.activity != "magic"
                or self.carrier == "food_process"
                or self.magic_plan is None
            ):
                raise ValueError("魔法能力须绑定本人已知魔纹和实际储能器物")
            if self.kind == "magic_energy_cost" and not 0.6 <= self.factor <= 0.98:
                raise ValueError("魔法消耗降低幅度须为2%至40%，仍受能量守恒限制")
            if self.kind == "magic_transfer_efficiency" and not 1.02 <= self.factor <= 1.4:
                raise ValueError("储能转移效率提升幅度须为2%至40%")
            if self.kind == "magic_transfer_efficiency" and not self.magic_plan.receiver_item_id:
                raise ValueError("储能转移须指定真实接收器物")
            if self.kind == "magic_energy_cost" and self.magic_plan.receiver_item_id:
                raise ValueError("单纹施放不能暗中混入储能转移")
        elif self.magic_plan is not None:
            raise ValueError("魔法能量不能伪装为普通劳动或食物能力")
        elif self.kind == "food_nutrition":
            if self.activity != "food" or not 1.02 <= self.factor <= 1.4:
                raise ValueError("食物能力须作用于食物，提升幅度为2%至40%")
        elif self.kind in {"material_strength", "craft_precision"}:
            if self.activity != "craft" or self.carrier != "tool" or not 1.02 <= self.factor <= 1.4:
                raise ValueError("物品属性只能绑定实际制造的工具，提升幅度为2%至40%")
        elif self.activity != "work" or not 0.6 <= self.factor <= 0.98:
            raise ValueError("劳动能力须作用于具体劳动，节省幅度为2%至40%")
        if self.kind == "food_nutrition" and self.carrier != "food_process":
            raise ValueError("营养能力必须实际用于食物处理与吸收")
        if self.carrier == "tool" and not self.materials:
            raise ValueError("工具型科技必须声明原型所需的真实材料")
        return self


class ApplicationMaterial(PrincipleModel):
    kind: Literal["wood", "metal", "clay", "fiber", "paper", "water", "charcoal"]
    quantity: int = Field(ge=1, le=4, strict=True)


CapabilityEffect.model_rebuild()


class InventionDecision(PrincipleModel):
    index: int = Field(ge=0, le=2, strict=True)
    approved: bool
    reasoning: str = Field(min_length=20, max_length=2000)
    effect: CapabilityEffect | None = None

    @model_validator(mode="after")
    def complete(self):
        if self.approved != (self.effect is not None):
            raise ValueError("每项可应用发明须有独立能力契约，未通过的设计不得产生能力")
        return self


class PrincipleDecision(PrincipleModel):
    proposal_id: str = Field(min_length=1, max_length=100)
    approved: bool
    reasoning: str = Field(min_length=20, max_length=3000)
    duplicate_of: str | None = None
    effect: CapabilityEffect | None = None
    connections: list[ConnectionReview] = Field(default_factory=list, max_length=6)
    invention_reviews: list[InventionDecision] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def review_complete(self):
        if self.approved and self.duplicate_of:
            raise ValueError("重复方法不能算新科技")
        if not self.approved and self.effect is not None:
            raise ValueError("未通过原理评审不能登记可用能力")
        if not self.approved and any(r.approved for r in self.invention_reviews):
            raise ValueError("基础原理未通过时，不能解锁其衍生发明")
        if self.effect is not None and self.invention_reviews:
            raise ValueError("旧单项能力与新版逐项发明评审不能混用")
        return self


class ReviewBatch(PrincipleModel):
    decisions: list[PrincipleDecision] = Field(min_length=1, max_length=3)


class ResearchIdea(TechnologyIdea):
    """哥哥，新请求的必填结构不沿用旧版兼容默认值。"""

    level: Literal["foundation", "technique"]
    unlocks: list[str] = Field(min_length=1, max_length=8)
    inventions: list[InventionDesign] = Field(min_length=1, max_length=2)
    connections: list[TechnologyConnection] = Field(max_length=6)
    parents: list[str] = Field(max_length=6)
    domain: Literal["physical", "magic", "hybrid"]


class ResearchBatch(PrincipleModel):
    reason: str = Field(min_length=8, max_length=600)
    ideas: list[ResearchIdea] = Field(max_length=2)


class ResearchDecision(PrincipleDecision):
    effect: None
    connections: list[ConnectionReview] = Field(max_length=6)
    invention_reviews: list[InventionDecision] = Field(min_length=1, max_length=2)


class ResearchReviews(PrincipleModel):
    decisions: list[ResearchDecision] = Field(min_length=1, max_length=2)
