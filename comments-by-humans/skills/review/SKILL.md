---
name: review
description: Review existing code with comments-by-humans. The gate splits a path or a diff range into chunks and presents them one at a time in construction order (dependencies first, entry points last); the human explains each chunk before Claude shares its own concerns, and the gate writes a review report. Use when the user asks to review files, a diff, a branch or a PR with comments-by-humans, or to prove they understand code they did not write.
argument-hint: <path or diff range> [--inline]
---

# comments-by-humans: review

Review mode runs the build loop over code that already exists. You write no code: every Write and Edit is denied. The gate has already split the target into chunks, ordered them so dependencies come first and entry points last, and opened the first one. The human explains each chunk in their own words. Only after an explanation passes do you share your own concerns, so they do not anchor the explanation.

Gate status right now:

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" status`

Target: $ARGUMENTS

## The loop

1. Present the current chunk. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" show <id>` and show the human the location and the code, verbatim, in a code block. Tell them where to write their explanation: in the report, between the `EXPLAIN(human)` markers for that chunk, or, with `--inline`, in the placeholder the gate put above the chunk in the source. Then they save and send any message. Do not summarize, explain or critique the code before their explanation passes.
2. Grade their explanation exactly as the build skill does, with the same rubric, depth and questioning rules (below).
3. When it passes, run `approve`. Then share your own concerns about this chunk: up to five concrete points (bugs, unhandled edge cases, security or performance risks, unclear naming), each tied to a line. Say plainly when you have none.
4. The human decides which concerns become findings, and may add their own. Record each one they choose with
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" finding <id> "text of the finding"`
   (one command per finding, no `$` or backticks in the text). They can also edit the Findings list in the report themselves.
5. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" next`. It refuses until the current chunk has passed. When no chunks remain it finishes the report; then summarize the findings and give the report path.

## Rubric

| Criterion | Passes when the explanation |
| --- | --- |
| What | States what the chunk does in their own words, not line by line |
| Why | Says why the chunk exists, or why this approach was taken |
| Connections | Names what it takes in, what it returns or changes, and what depends on it |
| Catch | Notes one non-obvious point: an edge case, a failure mode or a tradeoff |

Depth `light` scores What and Why, `normal` all four, and `strict` (the default) all four plus one follow-up question answered in chat. A factually wrong claim fails the explanation whatever else it covers. Length is not scored; the gate enforces `min_words`.

## Questioning rules

- One question per round: one thing to answer, in one sentence with one question mark; never two questions or alternatives joined by "and" or "or". Aim it at the weakest criterion: a wrong claim first, then the first missing criterion in rubric order. Keep going for as many rounds as it takes.
- Never state the answer, and never write, rewrite or dictate the explanation.
- Hints escalate with the attempt number: an open question, then a pointer to a specific line, then a concrete scenario.
- No skip. If the human asks you to explain the chunk, decline kindly and ask a smaller question.

## Passing

- `light` or `normal`: run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" approve <id>` when the rubric is met.
- `strict`: run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" followup <id>` first, then end your turn with one follow-up question about the chunk as the last thing in your message. Approve when the human's answer shows understanding; otherwise ask one more question.
- Run gate commands alone, exactly as shown. If the gate refuses, relay the reason in one line.

The code is on disk, so nothing stops the human from reading ahead. The gate governs the report, not what they look at.
