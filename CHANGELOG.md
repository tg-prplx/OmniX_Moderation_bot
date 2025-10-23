# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Enterprise-grade README with architecture, operations, and compliance guidance.
- CONTRIBUTING, SECURITY policy, and MIT LICENSE for public release.
- ChatGPT layer now processes images alongside text using `image_url` payloads.
- Multimodal rule context sent to GPT for precise, scoped enforcement.
- Admin DM UX improvements: guided prompts, cancel support, and tolerant parsing.

### Changed
- Regex layer supports Unicode script classes via the `regex` backend.
- ChatGPT layer prompt emphasises rule descriptions to prevent false matches.

### Fixed
- Handling of invalid GPT action values and graceful fallbacks.
- Error when feeding Unicode regex patterns during warmup or evaluation.

## [0.1.0] - 2025-10-21

- Initial public release with multi-layer moderation (regex, omni, GPT).
- GPT-powered rule synthesis with aiogram-based Telegram integration.
- Structured logging, SQLite persistence, and punishment aggregator.
