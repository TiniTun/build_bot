"""Configuration management."""

from pathlib import Path
from typing import Any

import yaml

from pydantic import BaseModel, Field, field_validator, model_validator

class LLMConfig(BaseModel):
    """LLM configuration."""

    provider: str
    model: str
    api_key: str
    api_base: str | None = None
    temperature: float = Field(default=0.7, ge=0., le=2.0)
    max_tokens: int = Field(default=2048, ge=0)

    @field_validator("api_base")
    @classmethod
    def api_base_must_be_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("api_base must be a valid URL")
        return v

class Config(BaseModel):
    """Main configuration"""

    workspace: Path
    llm: LLMConfig
    default_agent: str
    agent_path: Path = Field(default=Path("agents"))
    skills_path: Path = Field(default=Path("skills"))
    history_path: Path = Field(default=Path(".history"))

    @model_validator(mode="after")
    def resolve_paths(self) -> "Config":
        """Resolve relative paths to absolute using workspace."""
        for field_name in (
            "agent_path",
            "skills_path",
            "history_path",
        ):
            path = getattr(self, field_name)
            if not path.is_absolute():
                setattr(self, field_name, self.workspace / path)
        return self
    
    @classmethod
    def load(cls, workspace_dir: Path) -> "Config":
        """Load configuration from workspace directory."""
        config_data = cls._load_config_file(workspace_dir)
        config_data["workspace"] = workspace_dir
        return cls.model_validate(config_data)

    @classmethod
    def _load_config_file(cls, workspace_dir: Path) -> dict[str, Any]:
        """Load config from YAML file."""
        config_file = workspace_dir / "config.user.yaml"
        if not config_file.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_file}")

        with open(config_file, "r") as f:
            return yaml.safe_load(f) or {}