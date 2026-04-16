## Setup commands

### Core

```
uv sync
uv run main.py
```

Exposed an API server on `http://localhost:6185` by default.

### Dashboard(WebUI)

```
cd dashboard
pnpm install # First time only. Use npm install -g pnpm if pnpm is not installed.
pnpm dev
```

Runs on `http://localhost:3000` by default.

## Dev environment tips

1. When modifying the WebUI, be sure to maintain componentization and clean code. Avoid duplicate code.
2. Do not add any report files such as xxx_SUMMARY.md.
3. After finishing, use `ruff format .` and `ruff check .` to format and check the code.
4. When committing, ensure to use conventional commits messages, such as `feat: add new agent for data analysis` or `fix: resolve bug in provider manager`.
5. Use English for all new comments.
6. For path handling, use `pathlib.Path` instead of string paths, and use `astrbot.core.utils.path_utils` to get the AstrBot data and temp directory.

## Plugin development requirements

1. **Repository scope**: Plugin code changes must NOT be committed to the AstrBot root repository. Only commit changes within the plugin's own directory/repository.
2. **Documentation updates**: When implementing feature improvements or bug fixes, you MUST update the following files in the plugin directory:
   - `CHANGELOG.md` - Document the changes
   - `README.md` - Update usage instructions if affected
   - `metadata.yaml` - Update the version number
   - `_conf_schema.json` or configuration files - Update if configuration options changed

## PR instructions

1. Title format: use conventional commit messages
2. Use English to write PR title and descriptions.
