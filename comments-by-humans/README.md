# comments-by-humans

Claude writes the code. You explain it.

comments-by-humans is a Claude Code plugin that stops Claude after every chunk of code it writes until you have explained that chunk, in your own words, in a comment above it. Claude grades your comment against a fixed rubric and, when it falls short, asks one question at a time until it passes, without ever giving you the answer. The stop is enforced by hooks, not by a prompt: while a comment is pending, Claude's Write, Edit and Bash calls are denied.

The same loop pointed at existing code is a review tool: the gate walks a path or a diff one chunk at a time, in construction order, and writes a review report.

Requires Claude Code and Python 3, nothing else. Tested with Claude Code 2.1.289 and Python 3.11.

## Install

```
/plugin marketplace add aidanfritzke/jarvis
/plugin install comments-by-humans@jarvis
```

To install from a branch, pin it: `/plugin marketplace add aidanfritzke/jarvis#jarvis-comments-by-humans`. From a shell, the same commands are `claude plugin marketplace add ...` and `claude plugin install ...`.

Then add `.comments-by-humans/` to your project's `.gitignore`. The plugin suggests this but never edits the file itself.

## Use

| Command | What it does |
| --- | --- |
| `/comments-by-humans:build <task>` | Turn the gate on and build the task one chunk at a time. Also resumes after a pause. |
| `/comments-by-humans:review <path or diff range> [--inline]` | Review existing code chunk by chunk, for example `src/`, `main..HEAD` or `HEAD~3`. With no argument, resumes the current review. |
| `/comments-by-humans:pause` | Lift the gate until you run build or review again. Only you can run it; each use is logged. |
| `/comments-by-humans:status` | Show the mode, the lock, the current chunk and its attempts, and the attempts each passed chunk took. |

### Build mode

1. Claude plans the task as chunks: complete units such as a function, a procedure or a class, about 40 lines each.
2. Claude writes one chunk with an empty placeholder above it, in the file's own comment syntax:

   ```ts
   /* EXPLAIN(human) c07
    *
    */
   export function retryWithBackoff(fn, attempts = 3) {
   ```

3. The gate locks. Claude stops and asks you to explain the chunk.
4. You type your explanation into the placeholder in your own editor, save, and send any message.
5. Claude grades it. If it falls short, Claude asks one question and you revise. At the default `strict` depth, a passing comment also draws one follow-up question that you answer in chat.
6. When it passes, Claude runs `gate approve`. The gate checks what Claude cannot fake, rewrites the tag to `EXPLAINED(human) c07`, and opens. Your comment stays in the code as documentation, and the tag lets an audit find every explained chunk.

### The rubric

| Criterion | Passes when the comment |
| --- | --- |
| What | States what the chunk does in your own words, not line by line |
| Why | Says why the chunk exists, or why this approach was taken |
| Connections | Names what it takes in, what it returns or changes, and what depends on it |
| Catch | Notes one non-obvious point: an edge case, a failure mode or a tradeoff |

A factually wrong claim fails the comment whatever else it covers. Length is not scored. When a comment falls short, Claude asks one question at a time, aimed at the weakest criterion, and escalates its hints: an open question, then a pointer to a specific line, then a concrete scenario. It never states the answer, never rewrites your comment, and never offers a skip.

### Review mode

`/comments-by-humans:review src/` splits the target into chunks and orders them so dependencies come first and entry points last. For each chunk, you write your explanation in the review report (`.comments-by-humans/review/<id>.md`) between the chunk's `EXPLAIN(human)` markers, or with `--inline`, in a placeholder the gate inserts above the chunk in the source. Claude writes no code in review mode. Only after your explanation passes does Claude share its own concerns, so they do not anchor yours; you choose which become findings. `gate next` refuses to open the next chunk until the current one passes. The finished report lists every chunk with your explanation, the attempts it took and its findings.

The code is on disk, so nothing stops you reading ahead. The gate governs the report, not what you look at.

## How the gate is enforced

Six hook entries call one script, `scripts/gate.py`. It reads `.comments-by-humans/state.json` and exits at once when the mode is off.

| Hook | Job |
| --- | --- |
| `PreToolUse` on `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `mcp__.*` | Unlocked: deny a write that adds code outside a chunk, adds more than one placeholder, prefills a placeholder, or changes your comment. Locked: deny every write, including MCP write tools. |
| `PreToolUse` on `Bash` | Locked: deny every command except a bare gate CLI call. Unlocked: deny shell writes into source files, so code goes through Write and Edit. |
| `PostToolUse` on `Write`, `Edit`, `MultiEdit`, `NotebookEdit` | Register each new chunk with a hash of its code and lock the gate. Re-gate an explained chunk whenever its code changes. |
| `UserPromptSubmit` | On each message while locked: check the placeholder, count the attempt, and tell Claude how to grade it. |
| `UserPromptExpansion` | Record build, review and pause when you type them. |
| `PreToolUse` on `Skill` | Deny Claude invoking pause. |

**Approval.** Claude's judgment covers only the rubric. `gate approve` passes a chunk only if the comment is no longer the empty placeholder and meets `min_words`, the human submitted that exact version with a message, the chunk's code hash is unchanged since the lock was set, and, at `strict` depth, a follow-up question was recorded and the human has replied since.

**Re-gating.** Any change Claude makes to an explained chunk reopens its gate: the tag reverts to `EXPLAIN(human)`, your comment stays for you to update, and the lock is set.

**Fail closed.** A hook that times out, or exits with any code other than 2, does not block. The script therefore exits 2 on any internal error, including a corrupt state file.

**Protected paths.** While the gate exists, Claude's writes to `.comments-by-humans/`, `.claude/settings*.json`, `~/.claude/settings*.json`, `~/.claude/plugins/` and the plugin itself are denied, as are shell commands that touch them or disable the plugin. Hooks also fire inside subagents, so delegating a write does not bypass the lock.

**Human-only pause.** A command you type fires `UserPromptExpansion`, which records the pause. The pause skill sets `disable-model-invocation`, and a `PreToolUse` hook on `Skill` denies it as well, so Claude cannot pause itself.

## Settings

Edit `.comments-by-humans/config.json` (created with defaults on first use). Claude cannot edit it.

| Setting | Default | Meaning |
| --- | --- | --- |
| `depth` | `strict` | `light` scores What and Why. `normal` scores all four criteria. `strict` adds one follow-up question per chunk, answered in chat. |
| `target_chunk_lines` | `40` | Target chunk size. A chunk always runs to the end of its function, procedure or feature. |
| `min_words` | `5` | Shortest comment the gate accepts. |
| `exempt` | lockfiles, build and codegen output, data, config and docs | Glob patterns Claude may write without a placeholder. |
| `review.inline` | `false` | Write review explanations into the source instead of the report. |

## Project state

| File | Holds |
| --- | --- |
| `.comments-by-humans/state.json` | Mode, chunks, lock, current chunk, attempt counts |
| `.comments-by-humans/log.jsonl` | One `passed` line per chunk (file, line range, attempts), plus pause, re-gate, removal and ungated-write events |
| `.comments-by-humans/config.json` | The settings above |
| `.comments-by-humans/review/` | One report per review |

To remove the gate from a project entirely, delete `.comments-by-humans/` yourself.

## Limits

- **Grading can be lenient.** The hooks guarantee that a comment exists and that you submitted it; only Claude judges whether it is good. The rubric is fixed, strict depth adds a live follow-up, and the eval suite below checks that weak and wrong comments are rejected.
- **Chunk extents are heuristic.** A chunk runs from its placeholder through the first statement below it, everything nested inside it, and any line glued to it without a blank line. This works across brace and indentation languages without a parser, but unusual layouts can make a chunk larger or smaller than intended.
- **Shell writes are caught heuristically.** While unlocked, the Bash hook blocks redirects, `tee`, in-place `sed`/`perl`, copies and patches into source files. A determined script could still write a file in a way the heuristic misses. While locked, every command except the gate CLI is denied.
- **Unknown file types are not gated.** Files whose comment syntax the plugin does not know are written freely and logged as ungated. Notebook edits are logged the same way.
- **Your own edits are yours.** Hooks govern Claude, not you. If you change a chunk's code while it is locked, approval refuses until you run `/comments-by-humans:build` (or `review`) to accept the new code.

## Development

```
comments-by-humans/
├── .claude-plugin/plugin.json
├── skills/                    build, review, pause, status
├── hooks/hooks.json
├── scripts/
│   ├── gate.py                hook entry points, state machine, gate CLI
│   └── comments.py            comment syntax per language, markers, chunk extents, review chunker
├── tests/
│   ├── test_gate.py           every hook driven with real-shaped payloads (no model calls)
│   └── install_test.sh        install into a clean Claude Code home and prove the lock holds
└── evals/
    ├── <case>/                claude plugin eval cases: scripted weak, wrong and good comments,
    │                          locked writes, pause and "just explain it" pressure
    ├── _lib/                  scaffold that builds each case's workspace through the hooks
    └── learner/               headless driver with scripted learner profiles
```

Deterministic tests:

```
python3 -m unittest discover -s comments-by-humans/tests -v
```

Model-graded evals (they call the model and need the OS sandbox for Bash; on Linux install `bubblewrap` and `socat`):

```
claude plugin eval ./comments-by-humans --scaffold --allow-tools "Bash(python3 *)" Write Edit
```

End-to-end runs with a scripted learner (each costs real model calls):

```
python3 comments-by-humans/evals/learner/drive.py --profile weak-first --expect-chunks 5 \
  --task "Create textstats.py with five functions, each its own chunk: ..."
python3 comments-by-humans/evals/learner/drive.py --profile diligent --review src/ --workdir path/to/repo
```

Profiles live in `evals/learner/profiles.json`: `diligent`, `weak-first`, `wrong-first` and `pleads`.

Install test on a clean home directory:

```
comments-by-humans/tests/install_test.sh                      # this checkout
comments-by-humans/tests/install_test.sh aidanfritzke/jarvis   # the published marketplace
```

The design spec is in [`docs/comments-by-humans-spec.md`](../docs/comments-by-humans-spec.md).
