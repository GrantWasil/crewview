# Contributing

Start with an issue describing the problem and a synthetic reproduction. Please read `AGENTS.md` before changing code or asking an agent to work on Crewview.

Keep changes focused and preserve the public-message, permission, attribution, and local-only boundaries. New providers or platforms need an explicit design and test plan; changing role names alone does not add provider support.

Run the relevant unit tests and `python3 scripts/release-check.py`. Tests should use temporary state and fake CLIs, never live account credentials. Include what changed, why, test evidence, and known limitations in your pull request.

Do not include private prompts, histories, installation backups, user configuration, or screenshots of actual work. Use `demo.py` for shareable examples. Changes to the distribution allowlist require reviewing every newly allowed file.

By contributing, you agree that your contributions are provided under the repository's MIT license.
