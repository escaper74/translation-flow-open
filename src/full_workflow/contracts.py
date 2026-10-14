from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from translation_flow.contracts import JobRequest


WORKFLOW_MODE = "translation_picture_final"


@dataclass(frozen=True)
class FullWorkflowGates:
    picture_and_mux_30m_human_accepted: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FullWorkflowGates":
        gate = cls(
            picture_and_mux_30m_human_accepted=(
                value.get("picture_and_mux_30m_human_accepted") is True
            )
        )
        if not gate.picture_and_mux_30m_human_accepted:
            raise ValueError("整片处理必须显式确认 30 分钟画面与最终汇合样片已通过人工验收")
        return gate


@dataclass(frozen=True)
class FullWorkflowDelivery:
    additional_mp4: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "FullWorkflowDelivery":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("delivery 必须是 JSON object")
        additional_mp4 = value.get("additional_mp4", False)
        if not isinstance(additional_mp4, bool):
            raise ValueError("delivery.additional_mp4 必须是 boolean")
        return cls(additional_mp4=additional_mp4)


@dataclass(frozen=True)
class FullWorkflowRequest:
    translation: JobRequest
    gates: FullWorkflowGates
    delivery: FullWorkflowDelivery

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FullWorkflowRequest":
        if value.get("workflow_mode") != WORKFLOW_MODE:
            raise ValueError(f"不支持的顶层 workflow 模式：{value.get('workflow_mode')!r}")
        return cls(
            translation=JobRequest.from_mapping(value),
            gates=FullWorkflowGates.from_mapping(value.get("gates", {})),
            delivery=FullWorkflowDelivery.from_mapping(value.get("delivery")),
        )
