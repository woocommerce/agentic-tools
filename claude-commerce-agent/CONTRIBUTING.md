# Contributing

This repository is a reference implementation: it demonstrates how the agents from
Anthropic's commerce-agents reference can run against a live WooCommerce store. It follows
the reference at the commit `requirements.txt` pins, and the reference's packages are
installed from there rather than copied or altered.

## Bugs and questions

Open an issue. Include the store setup you ran against (the Docker store, or a real site)
and the exact command or prompt that misbehaved.

## Security

Please do not file security problems as public issues. Report them privately to the
maintainers.

## Pull requests

Changes are welcome when they keep two things true: the reference's packages stay
unmodified, installed from the pin, and the safety properties the test suites assert (no
checkout completion, no merchant write without approval) keep passing. Run `ruff check .`,
`ruff format --check .`, and `pytest` before opening the PR.
