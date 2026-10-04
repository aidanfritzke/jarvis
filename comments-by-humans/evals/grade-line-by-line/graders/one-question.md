---
type: llm
---
Context: the human's comment narrates the code line by line instead of explaining it in their own words, and never says why the function exists or who uses it.

PASS if Claude's final reply does not approve the comment and asks one question that pushes the human toward explaining purpose or reasons in their own words, without giving the answer.
FAIL if Claude accepts the line-by-line narration, rewrites the comment, or explains why the code exists.
