from __future__ import annotations

import base64
import json
import mimetypes
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config


class ImageReviewRequest(BaseModel):
    image_path: Path
    question: str = "请描述图片中的关键信息，并说明与量化策略复盘相关的可见事实。"
    output_path: Path | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class ImageReviewResult(BaseModel):
    status: str
    image_path: Path
    question: str
    model: str
    base_url: str
    content: str
    reasoning_content: str | None = None
    output_path: Path | None = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class ImageReviewService:
    """调用独立多模态模型识别本地图片并返回文字描述。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()

    def run(self, request: ImageReviewRequest) -> ImageReviewResult:
        image_path = self._resolve_path(request.image_path)
        if not image_path.exists():
            raise FileNotFoundError(f"图片文件不存在：{image_path}")
        if not image_path.is_file():
            raise IsADirectoryError(f"不是图片文件：{image_path}")

        api_key = request.api_key or os.getenv("MOONSHOT_API_KEY") or os.getenv("CRITIC_AGENT_API_KEY")
        base_url = request.base_url or os.getenv("MOONSHOT_BASE_URL") or "https://api.moonshot.cn/v1"
        model = request.model or os.getenv("MOONSHOT_MODEL") or "kimi-k2.6"
        if not api_key:
            raise RuntimeError("缺少 MOONSHOT_API_KEY，无法调用 Kimi 多模态模型。")

        from openai import OpenAI

        image_url = self._image_data_url(image_path)
        client = OpenAI(api_key=api_key, base_url=base_url)
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是股票量化项目中的图片理解助手。请只描述图片中可见事实，"
                        "不要编造数据；如果图片与调用方提供的数据文件冲突，应提醒以数据文件为准。"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {
                            "type": "text",
                            "text": f"{request.question}\n\n图片路径：{image_path}",
                        },
                    ],
                },
            ],
        )
        message = completion.choices[0].message
        result = ImageReviewResult(
            status="success",
            image_path=image_path,
            question=request.question,
            model=model,
            base_url=base_url,
            content=message.content or "",
            reasoning_content=getattr(message, "reasoning_content", None),
        )
        output_path = self._resolve_output_path(request.output_path)
        result.output_path = output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result

    def _resolve_path(self, path: Path) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _resolve_output_path(self, output_path: Path | None) -> Path:
        if output_path is not None:
            return self._resolve_path(output_path)
        return (
            self.config.root_dir
            / "artifacts"
            / "image_reviews"
            / f"image_review_{datetime.now():%Y%m%d_%H%M%S}.json"
        )

    def _image_data_url(self, image_path: Path) -> str:
        mime_type, _ = mimetypes.guess_type(str(image_path))
        if not mime_type:
            suffix = image_path.suffix.lower().lstrip(".") or "png"
            mime_type = f"image/{'jpeg' if suffix in {'jpg', 'jpeg'} else suffix}"
        payload = base64.b64encode(image_path.read_bytes()).decode("utf-8")
        return f"data:{mime_type};base64,{payload}"
