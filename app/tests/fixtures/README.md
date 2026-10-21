# Synthetic research fixtures

`content/` is invented test input, reconstructed from the existing test assertions.
It is not a copy of a researcher workspace. Familiar direction and project keys
are compatibility identifiers used by the tests, not user records.

Tests copy this folder to a temporary workspace. Do not replace it with real
research, PDFs, conversations, databases, environment files, or credentials.
To add a regression, add the smallest synthetic record required by that test.

Some assertions compare exact text from the historical test suite. For example,
`projects.yml`'s note is from `test_ui.py::test_project_edit_one_line_diff`;
`now.yml`'s question is from `test_chat.py::QUESTION`, and its week/puzzle text
is from `test_weekly.py` round-trip assertions. These compatibility strings were
reconstructed from test code, not loaded from the author's research files. They
are not a statement about a user's present research activity.
