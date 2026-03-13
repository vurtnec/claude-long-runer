# Contributing to Claude Long-Runner

Thank you for your interest in contributing! This guide will help you get started.

## How to Contribute

1. **Fork** the repository on GitHub.
2. **Clone** your fork locally: `git clone https://github.com/<your-username>/claude-long-runner.git`
3. **Create a branch** for your change: `git checkout -b feature/my-change`
4. **Make your changes**, add tests if applicable.
5. **Commit** using [Conventional Commits](https://www.conventionalcommits.org/) (see below).
6. **Push** to your fork: `git push origin feature/my-change`
7. **Open a Pull Request** against the `main` branch.

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Code Style

- Follow [PEP 8](https://peps.python.org/pep-0008/).
- Type hints are encouraged for function signatures and return values.
- Keep functions focused and well-documented with docstrings where appropriate.

## Commit Conventions

Use [Conventional Commits](https://www.conventionalcommits.org/):

- `feat: add new trigger type` -- a new feature
- `fix: handle empty state file` -- a bug fix
- `docs: update scheduler examples` -- documentation only
- `refactor: simplify trigger engine` -- code restructuring
- `test: add unit tests for state_manager` -- tests
- `chore: update dependencies` -- maintenance

## Reporting Issues

When opening an issue, please include:

- A clear, descriptive title.
- Steps to reproduce the problem (if applicable).
- Expected vs. actual behavior.
- Python version and OS.
- Relevant logs or error messages.

## Pull Request Guidelines

- Keep PRs focused on a single change.
- Update documentation if your change affects usage or configuration.
- Ensure existing functionality is not broken.

## Questions?

If you have questions, feel free to open a GitHub issue with the `question` label.
