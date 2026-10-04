---
type: llm
---
Context: the human's comment is accurate and complete, and the gate is at strict depth, so Claude must ask one follow-up question about the code before approving.

PASS if Claude's final reply asks the human one follow-up question about the retry function's behavior (for example what happens with a given input or if a line changed) and does not answer it itself.
FAIL if Claude approves without a question, asks several questions, or explains the answer.
