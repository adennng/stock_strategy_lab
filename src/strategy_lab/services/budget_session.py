from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from strategy_lab.config import AppConfig, load_app_config


class BudgetChatSession(BaseModel):
    thread_id: str
    created_at: str
    updated_at: str
    title: str | None = None
    current_run_state_path: str | None = None
    message_count: int = 0


class BudgetSessionManager:
    """BudgetAgent 终端会话注册表。

    LangGraph 的完整消息状态保存在 SQLite checkpointer 中；本服务只保存便于
    CLI 列表和恢复的轻量元数据，例如 thread_id、标题和当前加载的 budget_run_state。
    """

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.workspace_dir = self.config.root_dir / "artifacts" / "budget_agent_workspace"
        self.checkpoint_dir = self.workspace_dir / "checkpoints"
        self.sessions_dir = self.workspace_dir / "sessions"
        self.checkpoint_path = self.checkpoint_dir / "budget_agent.sqlite"
        self.registry_path = self.sessions_dir / "sessions.json"

    def ensure_dirs(self) -> None:
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def new_thread_id(self) -> str:
        return f"budget-chat-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    def create_session(
        self,
        *,
        thread_id: str | None = None,
        title: str | None = None,
        current_run_state_path: str | Path | None = None,
    ) -> BudgetChatSession:
        self.ensure_dirs()
        now = self._now()
        session = BudgetChatSession(
            thread_id=thread_id or self.new_thread_id(),
            created_at=now,
            updated_at=now,
            title=title,
            current_run_state_path=str(current_run_state_path) if current_run_state_path else None,
            message_count=0,
        )
        sessions = self._load_registry()
        sessions[session.thread_id] = session.model_dump()
        self._save_registry(sessions)
        return session

    def get_session(self, thread_id: str) -> BudgetChatSession | None:
        payload = self._load_registry().get(thread_id)
        return BudgetChatSession.model_validate(payload) if payload else None

    def get_or_create_session(
        self,
        *,
        thread_id: str | None = None,
        title: str | None = None,
        current_run_state_path: str | Path | None = None,
    ) -> BudgetChatSession:
        if thread_id:
            existing = self.get_session(thread_id)
            if existing:
                return existing
        return self.create_session(
            thread_id=thread_id,
            title=title,
            current_run_state_path=current_run_state_path,
        )

    def latest_session(self) -> BudgetChatSession | None:
        sessions = self.list_sessions(limit=1)
        return sessions[0] if sessions else None

    def list_sessions(self, *, limit: int | None = None) -> list[BudgetChatSession]:
        items = [BudgetChatSession.model_validate(value) for value in self._load_registry().values()]
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return items[:limit] if limit else items

    def update_session(
        self,
        thread_id: str,
        *,
        title: str | None = None,
        current_run_state_path: str | Path | None | object = ...,
        message_count: int | None = None,
    ) -> BudgetChatSession:
        sessions = self._load_registry()
        existing = sessions.get(thread_id)
        if existing:
            session = BudgetChatSession.model_validate(existing)
        else:
            session = self.create_session(thread_id=thread_id)
        session.updated_at = self._now()
        if title:
            session.title = title
        if current_run_state_path is not ...:
            session.current_run_state_path = str(current_run_state_path) if current_run_state_path else None
        if message_count is not None:
            session.message_count = int(message_count)
        sessions[thread_id] = session.model_dump()
        self._save_registry(sessions)
        return session

    def _load_registry(self) -> dict[str, dict[str, Any]]:
        self.ensure_dirs()
        if not self.registry_path.exists():
            return {}
        try:
            value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def _save_registry(self, sessions: dict[str, dict[str, Any]]) -> None:
        self.ensure_dirs()
        self.registry_path.write_text(
            json.dumps(sessions, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")
