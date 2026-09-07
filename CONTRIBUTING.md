# Contributing Guidelines

Thank you for your interest in contributing. Bug reports, feature requests, corrections, and documentation improvements are welcome.

## Reporting bugs and requesting features

Use the GitHub issue tracker for non-security bugs and feature requests. Before filing an issue, check open and recently closed issues. Include:

- A reproducible test case or sequence of steps
- The version or commit being used
- Relevant modifications
- Environment and deployment details with credentials and personal data removed

## Contributing through pull requests

1. Fork the repository.
2. Create a focused branch from the latest `main` branch.
3. Open an issue before beginning substantial work.
4. Keep the change narrowly scoped and avoid unrelated reformatting.
5. Run all local checks:

   ```bash
   pytest
   ruff check src tests scripts
   mypy
   cfn-lint template.yaml
   sam validate --lint --template-file template.yaml
   ```

6. Commit with a clear message and open a pull request.
7. Address automated checks and reviewer feedback.

## Security issues

Do not report security vulnerabilities through public GitHub issues. Follow [SECURITY.md](SECURITY.md) and use the [AWS vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/).

## Code of Conduct

This project follows the [Amazon Open Source Code of Conduct](CODE_OF_CONDUCT.md).

## Licensing

See [LICENSE](LICENSE). By submitting a contribution, you agree that it may be distributed under the project's MIT-0 license. A Contributor License Agreement may be requested for larger contributions.
