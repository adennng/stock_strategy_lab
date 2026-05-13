---
name: image-review
description: "通用图片理解 skill。无论当前 Agent 使用的模型是否支持多模态，都可以调用独立多模态服务读取本地图片并返回文字描述。"
license: Proprietary project skill
---

# Image Review Skill

## 适用场景

当你需要理解本地图片，但当前 Agent 模型不支持多模态，或你希望用独立的多模态模型进行图片识别时，使用本 skill。

## 使用原则

适用场景：

```text
1. 调用方明确要求查看图片。
2. 图像可能包含数据文件没有直接表达的视觉形态，例如走势阶段切换、曲线分叉、图表异常。
3. 当前 Agent 模型不支持多模态，但任务确实需要图片描述。
```

本 skill 调用的是独立服务，不要求当前 Agent 自身具备看图能力。

## 命令

```powershell
python -m strategy_lab.cli image review IMAGE_PATH --question "请描述这张图中与策略复盘有关的关键信息"
```

指定输出路径：

```powershell
python -m strategy_lab.cli image review IMAGE_PATH --question "请比较策略曲线与基准曲线的可见差异" --output-path artifacts/image_reviews/my_review.json
```

## 参数

`IMAGE_PATH`
  本地图片路径。可以使用项目相对路径，也可以使用 Windows 绝对路径。

`--question`
  希望图片理解模型重点回答的问题。问题应具体，例如“请看策略和基准曲线在哪些时间段明显分化”，不要只写“看看图”。

`--output-path`
  可选。JSON 结果输出路径。不传时自动写入 `artifacts/image_reviews/image_review_{timestamp}.json`。

## 输出 JSON

命令会输出并保存 JSON：

```json
{
  "status": "success",
  "image_path": "...",
  "question": "...",
  "model": "kimi-k2.6",
  "base_url": "https://api.moonshot.cn/v1",
  "content": "图片理解结果",
  "reasoning_content": "...",
  "output_path": "...",
  "created_at": "..."
}
```

