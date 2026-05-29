"""Skill loader for discovering, loading, and validating skills."""

import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from utils.def_loader import DefNotFoundError, discover_definitions, parse_definition

if TYPE_CHECKING:
    from utils.config import Config

logger = logging.getLogger(__name__)


def _validate_safe_path(value: str) -> str:
    """Reject absolute paths and parent traversal in skill-relative paths."""
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe path (must stay inside the skill directory): {value}")
    return value


class SkillReference(BaseModel):
    """A reference document bundled with a skill (loaded on demand)."""

    model_config = ConfigDict(extra="forbid")

    path: str
    description: str
    when_to_load: str

    _check_path = field_validator("path")(_validate_safe_path)


class SkillScript(BaseModel):
    """An executable script bundled with a skill (run on demand)."""

    model_config = ConfigDict(extra="forbid")

    path: str
    description: str
    when_to_run: str

    _check_path = field_validator("path")(_validate_safe_path)


class SkillDef(BaseModel):
    """Loaded skill definition (Skills v2 contract)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    when_to_use: list[str] = Field(min_length=1)
    required_tools: list[str] = Field(default_factory=list)
    permissions: dict = Field(default_factory=dict)
    references: list[SkillReference] = Field(default_factory=list)
    scripts: list[SkillScript] = Field(default_factory=list)
    content: str

    @field_validator("content")
    @classmethod
    def content_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("skill body must not be empty")
        return v


class SkillValidation(NamedTuple):
    """Validation outcome for a single skill directory."""

    id: str
    errors: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


class SkillLoader:
    """Load and manage skill definitions from filesystem."""

    @staticmethod
    def from_config(config: "Config") -> "SkillLoader":
        """Create SkillLoader from config."""
        return SkillLoader(config)

    def __init__(self, config: "Config"):
        self.config = config

    def discover_skills(self) -> list[SkillDef]:
        """Scan skills directory and return list of valid SkillDef."""
        return discover_definitions(
            self.config.skills_path, "SKILL.md", self._parse_skill_def
        )

    def _parse_skill_def(
        self, def_id: str, frontmatter: dict[str, Any], body: str
    ) -> SkillDef | None:
        """Parse skill definition (callback for discover_definitions); skips invalid."""
        skill, errors = self._validate_skill(def_id, frontmatter, body)
        if errors:
            logger.warning(f"Invalid skill '{def_id}': {'; '.join(errors)}")
            return None
        return skill

    def _validate_skill(
        self, def_id: str, frontmatter: dict[str, Any], body: str
    ) -> tuple[SkillDef | None, list[str]]:
        """Validate a single skill. Returns (SkillDef|None, errors)."""
        try:
            skill = SkillDef.model_validate(
                {**frontmatter, "id": def_id, "content": body.strip()}
            )
        except ValidationError as e:
            return None, [self._format_error(err) for err in e.errors()]

        errors = self._validate_bundled_files(def_id, skill)
        if errors:
            return None, errors
        return skill, []

    def _validate_bundled_files(self, def_id: str, skill: SkillDef) -> list[str]:
        """Check that referenced reference/script files exist on disk."""
        skill_dir = self.config.skills_path / def_id
        errors: list[str] = []
        for ref in skill.references:
            if not (skill_dir / ref.path).is_file():
                errors.append(f"references: file not found: {ref.path}")
        for script in skill.scripts:
            if not (skill_dir / script.path).is_file():
                errors.append(f"scripts: file not found: {script.path}")
        return errors

    @staticmethod
    def _format_error(err: dict[str, Any]) -> str:
        """Format a pydantic error into a readable line."""
        loc = ".".join(str(part) for part in err["loc"]) or "(root)"
        return f"{loc}: {err['msg']}"

    def validate_skills(self) -> list[SkillValidation]:
        """Validate every skill directory, returning per-skill results."""
        results: list[SkillValidation] = []
        path = self.config.skills_path
        if not path.exists():
            return results

        for def_dir in sorted(path.iterdir()):
            if not def_dir.is_dir():
                continue

            def_file = def_dir / "SKILL.md"
            if not def_file.exists():
                results.append(SkillValidation(def_dir.name, ["SKILL.md not found"]))
                continue

            errors = self._validate_skill_file(def_dir.name, def_file)
            results.append(SkillValidation(def_dir.name, errors))

        return results

    def _validate_skill_file(self, def_id: str, def_file) -> list[str]:
        """Read and validate one SKILL.md, returning its error list."""
        try:
            content = def_file.read_text()
            frontmatter, body = parse_definition(
                content, def_id, lambda _id, fm, bd: (fm, bd)
            )
        except Exception as e:  # noqa: BLE001 - surfaced as a validation error
            return [f"failed to read SKILL.md: {e}"]

        _, errors = self._validate_skill(def_id, frontmatter, body)
        return errors

    def load_skill(self, skill_id: str) -> SkillDef:
        """Load full skill definition by ID."""
        skills = self.discover_skills()
        for skill in skills:
            if skill.id == skill_id:
                return skill

        raise DefNotFoundError("skill", skill_id)
