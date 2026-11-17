"""Regression: answers recorded from a REAL local model (qwen2.5-coder:7b via Ollama 0.40.16), 2026-11-17.

The first live run against a real model failed two ways that no protocol double had shown:
1. it ignored the <<<FILE>>> format and used markdown fences with a bare ``# path`` comment (nothing parsed, and
   three repairs got the same format back);
2. it wrote both files correctly, then repeated one sentence of its explanation until the output limit.
The strings below are copied from those runs (the looping tail is shortened); the tests pin the fixes.

A third finding came from the first real chat: the system prompt said "cite them like [#12]" and the model cited the
example number (#12) instead of the real pattern (#1).
"""

import re

import pytest

from app.chat.prompts import CHAT_SYSTEM_PROMPT
from app.generation.llm import early_stop_reason
from app.generation.parser import MAX_EXPLANATION_CHARS, parse_model_output

pytestmark = pytest.mark.regression

REAL_MARKDOWN_ANSWER = "```python\n# app/main.py\ndef add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n```\n\n```python\n# tests/test_main.py\nimport pytest\nfrom app.main import add, multiply\n\ndef test_add():\n    assert add(1, 2) == 3\n    assert add(-1, 1) == 0\n    assert add(-1, -1) == -2\n\ndef test_multiply():\n    assert multiply(1, 2) == 2\n    assert multiply(-1, 1) == -1\n    assert multiply(-1, -1) == 1\n```\n\n<<<EXPLANATION>>>\n- I added the `multiply` function to `app/main.py` which takes two arguments `a` and `b` and returns their product.\n- I also added a test for the `multiply` function in `tests/test_main.py`. The test checks that the function correctly multiplies positive numbers, a positive and a negative number, and two negative numbers.\n- The `add` function and its test were already present in the reference pattern, so I did not modify them.\n- No new dependencies were required for this implementation.\n- The function and test names follow the Python naming conventions, and the test functions use the `pytest` framework's assertion syntax.\n- The test functions include a variety of test cases to ensure the functions behave correctly in different scenarios."

REAL_LOOPING_ANSWER = '<<<FILE path="app/main.py" action="modify">>>\ndef add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n<<<END FILE>>>\n\n<<<FILE path="tests/test_main.py" action="create">>>\nimport pytest\nfrom app.main import add, multiply\n\ndef test_add():\n    assert add(1, 2) == 3\n    assert add(-1, 1) == 0\n    assert add(-1, -1) == -2\n\ndef test_multiply():\n    assert multiply(1, 2) == 2\n    assert multiply(-1, 1) == -1\n    assert multiply(-1, -1) == 1\n    assert multiply(0, 0) == 0\n    assert multiply(0, 1) == 0\n    assert multiply(1, 0) == 0\n<<<END FILE>>>\n\n<<<EXPLANATION>>>\nI added a `multiply` function to `app/main.py` that takes two arguments and returns their product. I also created a new test file `tests/test_main.py` with pytest tests for both the `add` and `multiply` functions. The tests cover various cases, including positive numbers, negative numbers, and zero.\nI did not add any new dependencies as the required functionality could be implemented using built-in Python functions.\nThe `multiply` function is straightforward and follows the same structure as the `add` function. The tests are simple assertions that check the correctness of the functions.\nI ensured that the code follows the project\'s language, framework, version, naming, and layout conventions.\nI included input validation and error handling in the tests, but since the functions are simple arithmetic operations, no additional error handling was necessary.\n[... about 1,100 more tokens of the same kind ...]\nI did not include any code that is not necessary for the project to be considered a complete and utter success.\nI did not include any code that is not necessary for the project to be considered a complete and utter success.\nI did not include any code that is not necessary for the project to be considered a complete and utter success.\nI did not include any code that is not necessary for the project to be considered a complete and utter success.\nI did not include any code that is not necessary for the project to be considered a complete and utter success.\nI did not include any code that is not necessary for the project to be considered a complete and utter success.'


def test_real_markdown_answer_with_bare_path_comments_now_parses():
    out = parse_model_output(REAL_MARKDOWN_ANSWER)
    assert [f.path for f in out.files] == ["app/main.py", "tests/test_main.py"]
    assert out.files[0].content == "def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n"
    assert "# app/main.py" not in out.files[0].content  # the label is not part of the file
    assert any("markdown" in w for w in out.warnings)  # still flagged: it did not follow the requested format
    assert out.explanation.startswith("- I added the `multiply` function")  # unterminated block is still captured


def test_real_looping_answer_keeps_the_files_and_collapses_the_loop():
    out = parse_model_output(REAL_LOOPING_ANSWER)
    assert [f.path for f in out.files] == ["app/main.py", "tests/test_main.py"]
    assert out.explanation.count("complete and utter success") == 1  # the repeated sentence is kept once
    assert len(out.explanation) <= MAX_EXPLANATION_CHARS + 2


def test_the_guard_stops_the_real_looping_answer_but_not_a_good_one():
    assert early_stop_reason(REAL_LOOPING_ANSWER) == "runaway"
    good = REAL_LOOPING_ANSWER[: REAL_LOOPING_ANSWER.index("<<<EXPLANATION>>>")]
    assert early_stop_reason(good) is None  # both files written, explanation not started yet: keep reading
    assert early_stop_reason(good + "<<<EXPLANATION>>>\nShort.\n<<<END EXPLANATION>>>") == "finished"


def test_the_chat_prompt_has_no_literal_example_citation_for_a_model_to_copy():
    assert not re.search(r"\[#\d+\]", CHAT_SYSTEM_PROMPT)  # a real 7B model copied "[#12]" verbatim
    assert "[#N]" in CHAT_SYSTEM_PROMPT and "Cite only patterns you were given" in CHAT_SYSTEM_PROMPT
