# Claude Code instructions

Project-specific guidance for [Claude Code](https://claude.com/claude-code)
working in this repo. Most guidance lives in [AGENTS.md](AGENTS.md) — this
file adds Claude Code-specific conventions on top.

## Read AGENTS.md first

The shared agent guidance — architectural principles, file map, style
rules, anti-patterns — lives in [AGENTS.md](AGENTS.md). Everything below
assumes you've read it.

## Claude Code-specific conventions

### Prefer the Edit tool over rewriting whole files

The Python files are small enough to read in one pass, but each one
serves a clear single purpose. When making changes:

- Use `Edit` with precise `old_string`/`new_string` to make surgical
  changes. Preserves file mtime semantics and avoids accidental
  formatting drift.
- Use `Write` only for net-new files or when 50%+ of a file is being
  rewritten.

### Prefer Bash over standalone shell scripts

This repo has no `scripts/` directory by design — the smoke test, the
proxy, and the server are the only executables. Don't add a script when
a documented Bash command in the README would do.

### Stay in the example's spirit

This is example code for sharing publicly. Every line is read by people
who'll judge whether the pattern is worth copying. Optimize for
readability and stability over cleverness.

- No dependency upgrades unless they fix a real problem
- No refactors for refactor's sake
- No "while we're here" cleanups in unrelated files
- No premature abstractions (interfaces, base classes, factories)

### Sensitive data review before commit

Before any commit that touches user-facing content (README, .html,
docs):

1. Grep for any internal codenames, customer names, or project
   shorthand used in your local environment
2. Grep for hardcoded workspace IDs (16-char hex strings) outside of
   `<placeholder>` brackets
3. Grep for personal email addresses
4. Check that `.databricks/`, `.env`, and `.mcp.json` are in
   `.gitignore`

The `.gitignore` is configured for this, but `databricks sync` and
similar commands can write state files that need to stay local.

### Voice and style

Customer-facing prose (README, HTML guides) uses Brickster voice. When
editing those files, invoke the `brickster-voice` skill to verify the
result. See AGENTS.md for the relevant rules.

### When you need to verify changes work

Run the smoke test against the live workspace:

```bash
export DATABRICKS_CONFIG_PROFILE=<your-profile>
export DATABRICKS_WAREHOUSE_ID=<your-warehouse-id>
uv run smoke_test.py
```

The test takes 30-90 seconds (cold-starts the warehouse). PASS means
pinning works end-to-end. Don't trust unit tests over this — the whole
value of the repo depends on the live behavior matching what the docs
claim.

### Common Claude Code skills relevant here

- `databricks-docs` — when looking up Databricks API behavior, prefer
  the official docs index over web search.
- `brickster-voice` — when editing README, HTML, or other user-facing
  prose, run this to align tone with the Databricks style guide.
- `commit` — when ready to commit, use this skill for the conventional
  commit-message format the project uses.
