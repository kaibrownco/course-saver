# Instructions for AI agents working on this repo

## Do not trigger CI builds on your own
Pushing to `main` (or opening a PR) only runs the tests. The slow Windows/Android builds
(~40 billed minutes of a limited free quota) run when the commit message or PR title contains `[build]`,
when a `v*` tag is pushed, or when the workflow is run manually.

- **Never add `[build]` to a commit message or PR title, push a `v*` tag, or run `gh workflow run build.yml`
  unless the user explicitly tells you to in that conversation.** A past instruction to build does not carry over
  to later commits.
- Don't write `[build]` in commit messages even in passing (e.g. "removed the [build] flag"): any occurrence triggers it.
- If a change is only worth verifying on a real build, say so and let the user decide.

## Safety
- Never commit or print cookies, session files, passwords or scraped course content. `cookies/`, `library/`
  and `build/` are gitignored; keep it that way.
- Never delete a user's downloaded files automatically. Deleting media is only ever an explicit user action
  (the "Remove course" button).
- Don't accept SDK/licence prompts or install large toolchains (e.g. `flet build --yes`) without asking first.

## Working in the repo
- App UI: `src/main.py` (Flet). Shared logic: `src/core/` (`service.py` is the entry point). CLI: `src/cli/`.
- Run tests with `pytest` (they use local stub servers; no network or real accounts needed).
- Don't type real credentials anywhere; test login against the local stub in `tests/`.
- See `README.md` for setup and how to build.
