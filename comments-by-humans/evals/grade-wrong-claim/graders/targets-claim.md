---
type: llm
---
Context: the human's comment covers every rubric criterion but makes one factually wrong claim: it says the function returns None when every attempt fails. The code actually raises RuntimeError("out of attempts").

PASS if Claude's final reply does not approve, and its question points the human at what happens after the last failed attempt (for example asks them to look at the line after the loop) without itself stating that the function raises RuntimeError.
FAIL if Claude approves, if it states the correct behavior outright (says it raises RuntimeError or an exception), or if it rewrites the comment.
