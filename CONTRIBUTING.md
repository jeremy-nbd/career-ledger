# Contributing

Thanks for helping make career evidence easier to maintain.

1. Use Python 3.9+ and run `python3 -m career_ledger`.
2. Make a focused change with fictional fixtures.
3. Run `python3 -m compileall -q career_ledger` and `python3 -m unittest discover -s tests -v`.
4. Describe the user-visible behavior, evidence boundary and validation in your pull request.

Keep the modes honest: local keyword matching is not semantic evaluation. Generated claims must remain drafts. Any change to confirmation, citations or exports should include a regression test showing why it is needed.

Do not commit `.env`, `data/`, exported personal backups, real resumes, employer documents or API credentials. Bug reports should use minimal fictional examples. For model compatibility issues, name the provider/model and HTTP status but omit keys and sensitive request bodies.

Ideas for contribution are listed in the README. Discuss large architectural or hosting changes in an Issue before implementing them.
