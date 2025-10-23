# Contributing to Spisdil Moderation Bot

Thanks for your interest in contributing! This project powers production-grade moderation flows, so we ask contributors to follow the guidelines below to keep the codebase reliable.

## Table of Contents

1. [Code of Conduct](#code-of-conduct)
2. [Getting Started](#getting-started)
3. [Development Workflow](#development-workflow)
4. [Coding Standards](#coding-standards)
5. [Testing Expectations](#testing-expectations)
6. [Documentation](#documentation)
7. [Security Considerations](#security-considerations)
8. [Releases](#releases)

## Code of Conduct

We expect everyone to follow inclusive, respectful communication. By participating you agree to uphold the behaviour outlined in the [Contributor Covenant 2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/). Report incidents privately via the process in [`SECURITY.md`](SECURITY.md).

## Getting Started

1. **Fork & clone** the repository.
2. **Create a virtual environment** (Python ≥ 3.11) and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```
3. **Copy `.env.example`** to `.env` and fill in the required secrets (Telegram token, OpenAI keys) if you plan to run the bot locally.

## Development Workflow

1. Create a feature branch from `master`: `git checkout -b feature/your-change`.
2. Keep commits scoped and well described; squash fixups before opening a PR.
3. Align with the [Conventional Commits](https://www.conventionalcommits.org/) style where possible (`feat:`, `fix:`, `docs:`…).
4. Open a Pull Request against `master` and link related issues.
5. Ensure CI passes and respond to review feedback promptly.

## Coding Standards

- Follow the existing formatting and typing conventions.
- Keep functions small and composable; document complex flows with inline comments as needed (avoid over-commenting obvious code).
- Prefer dependency injection for services to ease testing.
- Use ASCII text for code comments unless domain-specific Unicode is required.

## Testing Expectations

Before submitting a PR, run:

```bash
ruff check .
mypy spisdil_moder_bot
pytest
python -m compileall spisdil_moder_bot
```

Add tests for new functionality and regression coverage for bug fixes. Integration tests that hit live OpenAI APIs should be guarded by `RUN_LIVE_TESTS=1`.

## Documentation

- Update [`README.md`](README.md) when user-visible behaviour or configuration changes.
- Add entries to [`CHANGELOG.md`](CHANGELOG.md) summarising notable updates.
- For substantial features, include usage notes in `docs/` or comment references in the relevant modules.

## Security Considerations

- Never commit secrets. Use `.env` locally and GitHub secrets for CI.
- Changes touching authentication, rule enforcement, or data retention must include a risk assessment in the PR description.
- Report vulnerabilities privately following [`SECURITY.md`](SECURITY.md); do not open public issues for security matters.

## Releases

We publish tagged releases out of `master`. Each release should:

1. Update `CHANGELOG.md`.
2. Bump the version in `pyproject.toml`.
3. Create a signed tag (`git tag -s vX.Y.Z`).
4. Attach release notes referencing key PRs.

Thank you for helping us keep Spisdil Moderation Bot robust and trustworthy!
