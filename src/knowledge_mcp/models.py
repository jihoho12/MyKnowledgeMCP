"""데이터 모델 정의"""

from dataclasses import dataclass, field


@dataclass
class EntityInput:
    name: str
    category: str = "general"
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    properties: dict = field(default_factory=dict)


@dataclass
class RelationInput:
    source: str
    target: str
    relation: str
    description: str = ""


@dataclass
class ToolResponse:
    success: bool
    data: dict | None = None
    error: dict | None = None

    def to_dict(self) -> dict:
        result = {"success": self.success}
        if self.data is not None:
            result["data"] = self.data
        if self.error is not None:
            result["error"] = self.error
        return result
