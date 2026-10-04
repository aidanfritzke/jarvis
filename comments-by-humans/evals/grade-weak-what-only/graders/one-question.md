---
type: llm
---
Context: the human's comment on a small retry function only says what it does ("Retries the function a few times until it works") and nothing about why it exists, its inputs and outputs, or edge cases.

PASS if Claude's final reply tells the human the comment is not there yet and asks exactly one question meant to improve it, without itself stating the missing content (for example it must not say that the sleeps are exponential backoff, that it raises RuntimeError, or what it catches).
FAIL if Claude approves the comment, asks several questions at once, writes or suggests replacement wording for the comment, or explains the code.
