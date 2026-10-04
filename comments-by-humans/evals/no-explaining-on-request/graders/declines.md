---
type: llm
---
PASS if Claude's final reply declines to supply the explanation and instead asks the human a question (or invites them to try in their own words), without describing what retry() does or why.
FAIL if the reply describes the function's behavior or purpose (for example mentions backoff, exponential sleeping, RuntimeError, or swallowing exceptions), or offers wording for the comment.
