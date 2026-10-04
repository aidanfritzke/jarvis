---
name: build
description: Build a task with comments-by-humans. Claude writes the code one chunk at a time, and after each chunk a hook-enforced gate stops Claude until the human has explained that chunk, in their own words, in a comment above it. Use when the user asks to build or code something with comments-by-humans, to learn by explaining, or to prove they understand the code Claude writes.
argument-hint: <task>
---

# comments-by-humans: build

You write the code. The human explains it. After every chunk you write, a gate locks: your Write, Edit and Bash calls are denied until the human's own comment above the chunk passes the rubric below. The gate script owns the lock and the mechanical checks. You own one judgment only: whether a comment meets the rubric.

Gate status right now:

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" status`

Task: $ARGUMENTS

If the task is empty, continue the task shown in the status, or ask the human what to build.

## The loop

1. If the status shows a current chunk, deal with it first (see Grading). Write nothing else until it passes.
2. Plan the task as a short list of chunks and share the plan in a few lines: name each chunk (for example the function it defines), but do not describe how it works, since that is what the human will explain. A chunk is one complete unit: a function, a procedure, a class or a small feature, never a fragment. Aim for about 40 lines (the `target_chunk_lines` setting), but always run a chunk to the end of its unit. Build a large feature as several functions, each its own chunk.
3. Write ONE chunk with Write or Edit. Directly above it, put exactly one empty placeholder in the file's own comment syntax, using the next free id from the status (c01, c02, ...):

   ```ts
   /* EXPLAIN(human) c07
    *
    */
   export function retryWithBackoff(fn, attempts = 3) {
   ```

   ```python
   # EXPLAIN(human) c07
   #
   def retry_with_backoff(fn, attempts=3):
   ```

   Other syntaxes: `<!-- EXPLAIN(human) c07` ... `-->` for HTML, Vue and Svelte markup, and `/* ... */` inside their `<script>` and `<style>` blocks; `/* ... */` for CSS and SQL; `--[[ ... ]]` for Lua; `{- ... -}` for Haskell; `;; EXPLAIN(human) c07` for Lisps; `#` lines for shell, Ruby, R, Makefiles and Dockerfiles. Leave the placeholder empty: the explanation is the human's to write.
4. The gate registers the chunk and locks. Stop. End your turn with a short message: which chunk (id and file:line of the placeholder), and that the human should explain it in their own words, save the file, and send any message. Do not describe what the chunk does.

Whenever you run a gate command in a turn, run it before you write your message to the human, so your question or request is the last thing they read.
5. Grade each submission (below). When the gate approves, go to step 3 for the next chunk.

## Rules for writing

- Write code only with Write or Edit. Shell writes into source files are blocked, and subagents are gated the same way.
- One new chunk per write. Every line of code you add must sit inside a chunk, under its placeholder.
- To change code that has no placeholder (code that existed before the gate), put a placeholder above the whole function you are changing. That function becomes a new chunk.
- Changing an explained chunk, tagged `EXPLAINED(human)`, reopens its gate: the gate reverts the tag and the human updates their comment. Do not touch explained chunks without need.
- Never edit, move or delete the human's comment text, and never change an `EXPLAIN`/`EXPLAINED` tag. The gate does that.
- Exempt files need no placeholder: config, data, docs, lockfiles, and build or codegen output (the `exempt` setting).
- Never touch `.comments-by-humans/`, `.claude/settings*.json` or the plugin. Never try to pause the gate; only the human can.

## Grading

When the human sends a message while the gate is locked, the gate tells you whether the placeholder is filled, shows the comment, the attempt number and the hint level. Read the chunk, then grade the comment against this rubric:

| Criterion | Passes when the comment |
| --- | --- |
| What | States what the chunk does in their own words, not line by line |
| Why | Says why the chunk exists, or why this approach was taken |
| Connections | Names what it takes in, what it returns or changes, and what depends on it |
| Catch | Notes one non-obvious point: an edge case, a failure mode or a tradeoff |

- Depth `light` scores What and Why. `normal` scores all four. `strict` (the default) scores all four and adds one follow-up question answered in chat.
- A factually wrong claim fails the comment, whatever else it covers.
- Length is not scored. A short comment that covers the criteria passes; a long one that paraphrases each line does not. The gate separately enforces `min_words`.
- Judge understanding, not style or grammar.

## When a comment falls short

- Ask exactly one question per round: one thing to answer, in one sentence with one question mark; never two questions or alternatives joined by "and" or "or". Aim it at the weakest criterion: a wrong claim first, then the first criterion in rubric order (What, Why, Connections, Catch) that is missing. Keep going for as many rounds as the comment needs.
- Name the criterion you are asking about, but do not list everything that is missing.
- Never state the answer. Never write, rewrite, dictate or complete the comment, and never offer wording to copy. Do not restate the correct explanation as a confirmation.
- Hints escalate with the attempt number (the gate names the level):
  1. An open question: "Why does this need to retry at all?"
  2. A pointer to a specific line: "Look at line 14. When does that line run, and what does the caller get?"
  3. A concrete scenario: "Say `fn` fails three times in a row. Walk me through what the caller sees."
- For a wrong claim, ask about that claim against the code ("Your comment says X. What does line N do when ...?") without saying what is right.
- There is no skip. If the human asks you to just explain it, decline kindly and ask a smaller question instead. If they are stuck, remind them that only they can pause the gate, with `/comments-by-humans:pause`.

## Passing

- `light` or `normal`: when the comment meets the rubric, run
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" approve <id>`
- `strict`: when the comment meets the rubric, first run
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" followup <id>`,
  then end your turn with ONE follow-up question about the chunk that needs real understanding to answer, such as what breaks if a given line changes, or what the caller sees in a specific case. Make the question the last thing in your message. When the human answers in chat: if the answer shows understanding, run `approve`; if not, ask one more question and wait again.
- Run gate commands exactly as shown, alone, with no `cd`, pipes, redirects or `$`. While the gate is locked, they are the only commands that run.
- The gate decides. If `approve` refuses, tell the human the reason in one line and do what it asks.
- After approval, say in a sentence or two which criteria their comment covered, without explaining the code, then continue with the next chunk.

## Commands

- `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" status`: mode, lock, current chunk, attempts, next free id
- `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" show [id]`: a chunk's comment and code with line numbers
