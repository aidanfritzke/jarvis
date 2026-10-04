# comments-by-humans: Claude Code plugin spec

Oct 3, 2026 · @fritzke

## Summary

comments-by-humans (working name) is a Claude Code plugin that stops Claude after every chunk of code it writes until you have explained that chunk, in your own words, in a multiline comment above it. Claude grades the comment against a fixed rubric and, when it falls short, asks as many questions as it takes to point you in the right direction, one at a time, without giving the answer. A hook enforces the stop, not a prompt: Claude's write tools are denied while a comment is pending.

The same loop pointed at code that already exists becomes a code review tool.

It differs from [vibe-wise](https://github.com/nykooi1/vibe-wise) and the built-in [Learning output style](https://code.claude.com/docs/en/output-styles) in who explains. There, Claude explains or you write code. Here, Claude writes code and you explain it.

## Goals and non-goals

Goals:

- **Proof of understanding.** A chunk passes only when your comment shows you understand it. Claude never supplies the explanation.
- **Nothing advances unexplained.** Every chunk Claude writes carries a comment you wrote. There is no skip.
- **The block is mechanical.** Hooks enforce it, so it holds even if Claude drifts from its instructions.
- **Claude asks, never tells.** It does not write, dictate or rewrite your comment.
- **The comments stay.** Approved comments remain in the code as documentation.
- **One engine, two modes.** Build mode gates new code. Review mode walks code that already exists.

Non-goals:

- **Not practice at writing code.** The Learning style already covers that.
- **No custom editor UI in v1.** You type the comment straight into the source file, in whatever editor you use.

## Build mode

Build mode is one loop per chunk: Claude writes, the gate locks, you explain, Claude grades. `/comments-by-humans:build <task>` starts it.

&#91;embedded content: build-mode loop · 5 steps, 1 decision\]

A failed grade returns to you with questions, one at a time. Only a pass opens the gate.

- **Chunks.** Claude plans the task as a sequence of chunks. Each is a complete unit: a function, a procedure or a whole feature, never a fragment. About 40 lines is the target, not a cutoff.
- **Submitting.** No command is needed. Save the file and send any message; a hook detects the filled placeholder and prompts Claude to grade.
- **While locked.** Claude may read and search the code to form its question. It may not write files or run commands.
- **Later changes.** Any change Claude makes to a chunk you already explained reopens its gate. You update the comment before Claude continues.

## Comment contract

Claude writes an empty placeholder above each chunk, in the file's own block-comment syntax. You fill it in.

```ts
/* EXPLAIN(human) c07
 *
 */
export function retryWithBackoff(fn, attempts = 3) {
  // ...
}
```

The marker follows the `TODO(human)` convention of the Learning style. On approval the gate script rewrites it to `EXPLAINED(human) c07` and leaves your comment. The tag stays in the code, so an audit can find every explained chunk.

A comment passes when it meets the rubric:

| Criterion | Passes when the comment |
| --- | --- |
| What | States what the chunk does in your own words, not line by line |
| Why | Says why the chunk exists, or why this approach was taken |
| Connections | Names what it takes in, what it returns or changes, and what depends on it |
| Catch | Notes one non-obvious point: an edge case, a failure mode or a tradeoff |

A factually wrong claim fails the comment whatever else it covers. Length is not scored.

At the default `strict` depth, a comment that meets the rubric draws one follow-up question about the chunk, such as what breaks if a given line changes. You answer it in chat, in your own words, before the gate opens.

When a comment falls short, Claude follows four rules:

- One question at a time, aimed at the weakest criterion, for as many rounds as the comment needs.
- Never state the answer and never rewrite the comment.
- Hints escalate: an open question, then a pointer to a specific line, then a concrete scenario.
- Claude never explains. It keeps nudging until the comment passes, and the log records how many attempts that took.

## Enforcement

Six hooks and one state file make the gate hard. Each hook calls the same script, which reads `.comments-by-humans/state.json` and exits at once when the mode is off. Event behavior below is taken from the [hooks reference](https://code.claude.com/docs/en/hooks).

| Hook | Job |
| --- | --- |
| `PreToolUse` on `Write`, `Edit` | Unlocked: deny a write that adds a new chunk without exactly one placeholder. Locked: deny every write. |
| `PreToolUse` on `Bash` | Locked: deny every command except the gate CLI. |
| `PostToolUse` on `Write`, `Edit` | Register the new chunk, store a hash of its code, set the lock, and tell Claude to stop and ask for the explanation. Relock an explained chunk whenever its code hash changes. |
| `UserPromptSubmit` | On each message you send while locked: check whether the placeholder is filled, and tell Claude to grade it. |
| `UserPromptExpansion` | Record pause when you type it. |
| `PreToolUse` on `Skill` | Deny Claude invoking pause itself. |

The last two rows make pausing a human-only action. A command you type fires `UserPromptExpansion`. A skill Claude calls goes through `PreToolUse`, where it is refused.

**Approval.** When Claude judges the rubric met and the follow-up answered, it runs `gate approve c07`. The script decides whether the lock opens, and checks three things Claude cannot fake:

- The comment differs from the empty placeholder and meets a minimum word count.
- The chunk's code hash is unchanged since the lock was set.
- No write by Claude touched the file while it was locked, which the lock itself guarantees.

Claude's judgment covers only the rubric. Everything else is mechanical.

**Re-gating.** Any change Claude makes to an explained chunk reopens its gate. The tag reverts to `EXPLAIN(human)`, your existing comment stays in place for you to update, and the lock is set.

**Fail closed.** A hook that times out, or exits with any code other than 2, does not block. The script therefore exits 2 on any internal error and stays fast.

**Protected paths.** While the mode is on, Claude's writes to `.comments-by-humans/` and to `.claude/settings*.json` are denied, so it cannot edit the state or switch hooks off. Hooks also fire inside subagents, so delegating a write does not bypass the lock.

## Plugin layout

The plugin is four skills, one hook file and one script. It needs Claude Code and Python 3, the same footprint as vibe-wise.

```
comments-by-humans/
├── .claude-plugin/plugin.json
├── skills/
│   ├── build/SKILL.md       /comments-by-humans:build
│   ├── review/SKILL.md      /comments-by-humans:review
│   ├── pause/SKILL.md       /comments-by-humans:pause
│   └── status/SKILL.md      /comments-by-humans:status
├── hooks/hooks.json
├── scripts/
│   ├── gate.py              hook entry points, state machine, gate CLI
│   └── comments.py          block-comment syntax per language
└── evals/
```

The `build` and `review` skills hold the behavior: chunking, the rubric and the questioning rules. `gate.py` holds everything that must not depend on Claude's judgment.

Project state lives in `.comments-by-humans/` at the repository root:

| File | Holds |
| --- | --- |
| `state.json` | Mode, current chunk, lock status, attempt count |
| `log.jsonl` | One line per chunk: file, line range, and the number of attempts before it passed |
| `config.json` | The settings listed below |
| `review/` | Review reports, one per review session |

The plugin suggests adding `.comments-by-humans/` to `.gitignore` and never edits that file itself.

## Review mode

Review mode runs the same loop over code Claude did not write: `/comments-by-humans:review <path or diff range>`. Claude writes no code. It presents one chunk at a time, in the order the code would have been built, and you explain each.

|  | Build mode | Review mode |
| --- | --- | --- |
| Chunks come from | Claude, as it writes | Existing files or a diff, split by the chunker |
| Order | As written | Construction order: dependencies first, entry points last |
| Your explanation goes | In the source, as a comment | In a report file by default; in the source with `--inline` |
| The gate holds back | Claude's next write | The next chunk: `gate next` refuses until the current one passes |
| After a pass | Next chunk | Claude shows its own concerns, and you record findings |
| Output | Commented code and a log | A review report |

Claude's concerns appear only after your explanation passes, so they do not anchor it.

The report lists each chunk with your explanation, the number of attempts it took and any findings.

One limit is inherent. The code is already on disk, so nothing stops you reading ahead. The gate governs the report, not what you look at.

A later extension is a merge gate: a CI check that fails unless the report for a pull request shows every chunk passed. [Fable Method's merge-quiz](https://github.com/orenluxy/fable-method) is the nearest existing design.

## Settings and escape hatches

Five settings tune how strict the gate is, and one value is logged per chunk. All defaults are proposals.

| Setting | Default | Meaning |
| --- | --- | --- |
| `depth` | `strict` | `light` scores What and Why. `normal` scores all four criteria. `strict` adds one follow-up question per chunk, answered in chat. |
| `target_chunk_lines` | 40 | Target size. A chunk always runs to the end of its function, procedure or feature, even past the target. |
| `num_attempts` | N/A | Not a setting. The log records the attempts made before each chunk passes. |
| `min_words` | 5 | Shortest comment the script will accept |
| `exempt` | lockfiles, output of build and codegen tools, data and config files | Paths Claude may write without a placeholder |
| `review.inline` | `false` | Write review explanations into the source instead of the report |

One command lifts the gate and one reports on it. Only you can run `pause`, and each use is logged.

| Command | Effect |
| --- | --- |
| `/comments-by-humans:pause` | Lift the gate until you run `build` or `review` again. Code written while paused is logged as ungated. |
| `/comments-by-humans:status` | Show the current chunk, its attempts so far, and the attempts each passed chunk took |

## Risks and open questions

The main risk is lenient grading: the hooks guarantee a comment exists, but only Claude judges whether it is good.

| Risk | Mitigation |
| --- | --- |
| Claude passes weak or wrong comments | Fixed rubric, an eval suite of scripted weak and wrong comments, and, in a v2, a second grader that could be a different model |
| A chunk you cannot explain blocks all further work | By design. Escalating hints, the `depth` setting, exempt paths and pause are the only relief |
| Write paths the hooks miss, such as MCP file tools | Match `mcp__.*` write tools while locked, and test each path in the eval suite |
| A hook fails open on a timeout or crash | Exit 2 on every internal error, and keep the script fast |
| A whole feature is too large to explain in one comment | Claude builds a feature as separate functions, each its own chunk, and the target is tuned from real sessions |
| A pasted or AI-written explanation passes, which breaks the proof | Reduced, not solved. Strict is the default depth, so every chunk carries a follow-up question you answer live |

Open questions:

- [x] Name: comments-by-humans for now.
- [x] When Claude later edits a chunk you already explained, does the gate reopen? Decided: yes, for any change.
- [x] Do approved comments keep a visible tag for later audits, or become plain comments? Decided: they keep a visible tag.
- [x] Fork vibe-wise or build clean? Decided: build clean.
- [x] Is the second grader worth its cost per chunk? Decided: not in v1. A v2 could add one, possibly a different model.

## Build plan

Five milestones, each with an exit test. The gate is built before the behavior, because the gate is the product.

1. **Gate skeleton.** Manifest, state file, the two locked-state `PreToolUse` hooks and the gate CLI. Exit: with a lock set by hand, Claude cannot write a file or run a command.
2. **Build loop.** The `build` skill, placeholder enforcement, the `PostToolUse` lock, the `UserPromptSubmit` check, rubric grading and approval. Exit: a five-chunk task completes end to end with no manual state edits.
3. **Hardening.** Human-only pause, re-gating on change, fail-closed errors, subagent and MCP write paths, and the eval suite. Exit: scripted weak and wrong comments are rejected, and no tested write path bypasses the lock.
4. **Review mode.** The chunker for existing code and diffs, the report, and the findings step. Exit: a full review of one real repository produces a report.
5. **Distribution.** Marketplace repository, README, and an install test on a clean machine.

The eval suite can drive the plugin headless with scripted learner profiles, the method [Codeminer42 describes](https://blog.codeminer42.com/plugins-are-how-you-ship-a-claude-code-harness/) for its own tutor plugin.
