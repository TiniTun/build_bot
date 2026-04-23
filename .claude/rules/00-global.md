General principles:
- Prefer maintainability over cleverness.
- Prefer explicit code over magic.
- Prefer simple architecture over premature abstraction.
- Always preserve existing architecture unless explicitly asked to refactor it.
- Before writing code, first infer where the code belongs.
- Never place unrelated logic into the same file.
- Never generate placeholder pseudo-code unless explicitly requested.
- Never silently ignore errors.
- Never break typing.
- Never introduce hidden global state.
- Never duplicate business logic across modules.
- If requirements are ambiguous, choose the safest and most maintainable option.

When editing code:
- Read surrounding files and imports first.
- Reuse existing patterns from the repository.
- Minimize unnecessary code changes.
- Keep diffs small and focused.