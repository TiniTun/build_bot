"""Configuration management."""

import logging
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from pydantic import BaseModel, Field, field_validator, model_validator
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

class LLMConfig(BaseModel):
    """LLM configuration.

    ``provider`` is a free-form label; actual routing is handled by LiteLLM
    based on ``model``. Use the LiteLLM model format, e.g. ``gpt-4`` for an
    OpenAI-compatible model or ``anthropic/claude-opus-4.8`` for Anthropic.
    ``extra`` carries provider-specific parameters passed verbatim to LiteLLM.
    """

    provider: str
    model: str
    api_key: str
    api_base: str | None = None
    temperature: float = Field(default=0.7, ge=0., le=2.0)
    max_tokens: int = Field(default=2048, ge=0)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("model")
    @classmethod
    def model_must_not_be_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError(
                "model must be a LiteLLM model string, "
                "e.g. 'gpt-4' or 'anthropic/claude-opus-4.8'"
            )
        return v

    @field_validator("api_base")
    @classmethod
    def api_base_must_be_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("api_base must be a valid URL")
        return v


class TelegramVoiceConfig(BaseModel):
    """Telegram voice-message transcription configuration.

    ``api_key`` may be null; the channel falls back to ``llm.api_key`` only when
    ``provider`` is ``openai``. Limits guard cost and latency before download.
    """

    enabled: bool = False
    provider: Literal["openai"] = "openai"
    model: str = "gpt-4o-mini-transcribe"
    api_key: str | None = None
    max_file_size_mb: int = Field(default=20, gt=0)
    max_duration_seconds: int = Field(default=300, gt=0)
    request_timeout_seconds: int = Field(default=60, gt=0)


class TelegramConfig(BaseModel):
    """Telegram platform configuration."""

    enabled: bool = True
    bot_token: str
    allowed_user_ids: list[str] = Field(default_factory=list)
    voice: TelegramVoiceConfig = Field(default_factory=TelegramVoiceConfig)


class BraveWebSearchConfig(BaseModel):
    """Configuration for web search provider."""

    provider: Literal["brave"] = "brave"
    api_key: str


class Crawl4AIWebReadConfig(BaseModel):
    """Configuration for web read provider."""

    provider: Literal["crawl4ai"] = "crawl4ai"


class SourceSessionConfig(BaseModel):
    """Session affinity configuration for a source."""

    session_id: str


class ChannelConfig(BaseModel):
    """Channel configuration."""

    enabled: bool = False
    telegram: TelegramConfig | None = None


class ApiConfig(BaseModel):
    """HTTP API configuration."""

    host: str = "127.0.0.1"
    port: int = Field(default=8005, gt=0, lt=65536)


class ToolsConfig(BaseModel):
    """Capability policy for the tool registry.

    Absent from config => no policy is applied and current behavior is preserved.
    """

    enabled_capabilities: list[str] = Field(default_factory=list)
    # risk level (read/draft/confirm_required/write) -> allow/deny/require_confirmation
    risk_policy: dict[str, str] = Field(default_factory=dict)


class ExternalProviderConfig(BaseModel):
    """Configuration for a single external provider domain (email/calendar)."""

    provider: str | None = None
    enabled: bool = False


class ExternalToolsConfig(BaseModel):
    """External provider toggles. Disabled by default."""

    email: ExternalProviderConfig = Field(default_factory=ExternalProviderConfig)
    calendar: ExternalProviderConfig = Field(default_factory=ExternalProviderConfig)


class Config(BaseModel):
    """Main configuration"""

    workspace: Path
    llm: LLMConfig
    default_agent: str
    agents_path: Path = Field(default=Path("agents"))
    skills_path: Path = Field(default=Path("skills"))
    crons_path: Path = Field(default=Path("crons"))
    memories_path: Path = Field(default=Path("memories"))
    logging_path: Path = Field(default=Path(".logs"))
    history_path: Path = Field(default=Path(".history"))
    event_path: Path = Field(default=Path(".event"))
    websearch: BraveWebSearchConfig | None = None
    webread:Crawl4AIWebReadConfig | None = None
    channels: ChannelConfig = Field(default_factory=ChannelConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    tools: ToolsConfig | None = None
    external_tools: ExternalToolsConfig = Field(default_factory=ExternalToolsConfig)
    sources: dict[str, SourceSessionConfig] = Field(default_factory=dict)
    routing: dict = Field(default_factory=lambda: {"bindings": []})
    default_delivery_source: str | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_valid(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"timezone must be a valid IANA timezone: {v}") from e
        return v

    @model_validator(mode="after")
    def resolve_paths(self) -> "Config":
        """Resolve relative paths to absolute using workspace."""
        if not self.workspace.is_absolute():
            self.workspace = self.workspace.resolve()     

        for field_name in (
            "agents_path",
            "skills_path",
            "crons_path",
            "memories_path",
            "logging_path",
            "history_path",
            "history_path",
            "event_path",
        ):
            path = getattr(self, field_name)
            if not path.is_absolute():
                setattr(self, field_name, self.workspace / path)
        return self
    
    @classmethod
    def load(cls, workspace_dir: Path) -> "Config":
        """Load configuration from workspace directory."""
        config_data: dict[str, Any] = cls._load_merged_configs(workspace_dir)
        config_data["workspace"] = workspace_dir
        return cls.model_validate(config_data)

    @classmethod
    def _load_merged_configs(cls, workspace_dir: Path) -> dict[str, Any]:
        """Load and merge user and runtime config files."""
        config_data: dict[str, Any] = {}

        user_config: Path = workspace_dir / "config.user.yaml"
        runtime_config: Path = workspace_dir / "config.runtime.yaml"
        if user_config.exists():
            with open(file=user_config) as f:
                config_data = cls._deep_merge(config_data, yaml.safe_load(f) or {})

        if runtime_config.exists():    
            with open(runtime_config, "r") as f:
                config_data = cls._deep_merge(config_data, yaml.safe_load(f) or {})
        
        return config_data

    @staticmethod
    def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Deep merge override dict into base dict."""
        result = base.copy()

        for key, value in override.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = Config._deep_merge(result[key], value)
            else:
                result[key] = value

        return result

    def _set_nested(self, obj: dict, key: str, value: Any) -> None:
        """Set a nested value in a dict using dot notation."""
        keys = key.split(".")
        for k in keys[:-1]:
            if k not in obj or not isinstance(obj[k], dict):
                obj[k] = {}
            obj = obj[k]
        obj[keys[-1]] = value

    def _set_config_value(self, config_path: Path, key: str, value: Any) -> None:
        """Update a config value in a YAML file."""
        # Load existing or start fresh
        if config_path.exists():
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}

        if isinstance(value, BaseModel):
            value = value.model_dump()

        # Update the key (supports nested via dot notation)
        self._set_nested(data, key, value)

        # Write back
        with open(config_path, "w") as f:
            yaml.dump(data, f)

    def set_user(self, key: str, value: Any) -> None:
        """Update a config value in config.user.yaml."""
        self._set_config_value(self.workspace / "config.user.yaml", key, value)

    def set_runtime(self, key: str, value: Any) -> None:
        """Update a runtime value in config.runtime.yaml."""
        self._set_config_value(self.workspace / "config.runtime.yaml", key, value)

        if not self.reload():
            logging.warning("Config runtime update was written, but in-memory reload failed")

    def reload(self) -> bool:
        """Re-read config.user.yaml and merge with runtime."""
        try:
            config_data = self._load_merged_configs(self.workspace)
            config_data["workspace"] = self.workspace

            # Create new instance and copy values
            new_config = Config.model_validate(config_data)

            # Update all fields from new config
            for field_name in Config.model_fields:
                setattr(self, field_name, getattr(new_config, field_name))

            return True
        except Exception as e:
            logging.debug("Config reload failed: %s", e)
            return False


class ConfigHandler(FileSystemEventHandler):
    """Handles config file modification events."""

    def __init__(self, config: Config) -> None:
        self._cofig = config

    def on_modified(self, event) -> None:
        """Reload config when config.user.yaml changes."""
        if event.is_directory:
            return 
        
        path = Path(event.src_path).name
        if path in {"config.user.yaml", "config.runtime.yaml"}:
            self._cofig.reload()


class ConfigReloader:
    """Manages watchdog observer for config hot reload."""

    def __init__(self, config: Config):
        self._config = config
        self._observer = Observer()

    def start(self) -> None:
        """Start watching config file for changes."""
        handler = ConfigHandler(self._config)
        self._observer.schedule(handler, str(self._config.workspace), recursive=False)
        self._observer.start()

    def stop(self) -> None:
        """Stop watching."""
        self._observer.stop()
        self._observer.join()
        del self._observer
