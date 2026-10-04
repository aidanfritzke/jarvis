# comments-by-humans

The model writes the code. You explain it.

comments-by-humans stops an AI coding assistant after every chunk of code it writes until you have explained that chunk, in your own words, in a comment above it. The assistant grades your comment against a fixed rubric and, when it falls short, asks one question at a time until it passes, without ever giving you the answer. The stop is enforced mechanically, not by a prompt: while a comment is pending, the assistant's write and shell tools are denied.

The same loop pointed at existing code is a review tool: the gate walks a path or a diff one chunk at a time, in construction order, and writes a review report.

It runs two ways, on one gate engine (`scripts/gate.py`):

| Front end | Models | Enforced by |
| --- | --- | --- |
| **Claude Code plugin** | Whatever model Claude Code runs | Claude Code hooks |
| **Standalone agent** (`agent/cbh.py`) | Any model: Anthropic, OpenAI, Gemini, Ollama, OpenRouter, Groq, Mistral, DeepSeek, xAI, Together, LM Studio, vLLM, any OpenAI-compatible server, or any command-line model | The agent's own tool layer |

Requires Python 3 and nothing else; the Claude Code front end also needs Claude Code. Tested with Claude Code 2.1.289 and Python 3.11.

## Install

**Claude Code:**

```
/plugin marketplace add aidanfritzke/jarvis
/plugin install comments-by-humans@jarvis
```

To install from a branch, pin it: `/plugin marketplace add aidanfritzke/jarvis#jarvis-comments-by-humans-model-agnostic`. From a shell, the same commands are `claude plugin marketplace add ...` and `claude plugin install ...`.

**Any other model:** clone this repository (or copy its `comments-by-humans/` directory) and run `python3 comments-by-humans/agent/cbh.py`. Nothing to install.

Either way, add `.comments-by-humans/` to your project's `.gitignore`. The gate suggests this but never edits the file itself.

## Use it with any model

The standalone agent is a small coding agent whose only way to touch your project is a set of tools that run the gate's checks: `read_file`, `list_files`, `search`, `write_file`, `edit_file`, `run_command` and `gate`. There is no pause tool, so only you can pause. It has no dependencies beyond Python 3.

```
python3 comments-by-humans/agent/cbh.py --provider anthropic build "add a retry helper"
python3 comments-by-humans/agent/cbh.py --provider openai --model <model> review src/
python3 comments-by-humans/agent/cbh.py --provider ollama --model <model>
python3 comments-by-humans/agent/cbh.py --provider command --command "llm -m <model>"
```

With no action it opens an interactive session in the current directory (`--project` to change it). Type `/build <task>`, `/review <target> [--inline]`, `/pause`, `/status`, `/edit` (opens your `$EDITOR` at the waiting placeholder), `/help` or `/quit`.

| Provider | Endpoint | Key from |
| --- | --- | --- |
| `anthropic` | Anthropic Messages API; defaults to `claude-opus-5-5` | `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `gemini` | Gemini's OpenAI-compatible endpoint | `GEMINI_API_KEY` |
| `openrouter`, `groq`, `mistral`, `deepseek`, `xai`, `together` | Each service's OpenAI-compatible endpoint | `<NAME>_API_KEY` |
| `ollama`, `lmstudio` | `localhost:11434/v1`, `localhost:1234/v1` | none |
| `openai-compatible` | Any Chat Completions server, with `--base-url` | `OPENAI_API_KEY` if set |
| `command` | Any command that reads a transcript on stdin and prints the reply | the command's own |

- **Models:** every provider except `anthropic` needs `--model`; the agent does not guess model names.
- **Overrides:** `--base-url` and `--api-key-env` override a preset.
- **Settings:** settings also come from `CBH_PROVIDER`, `CBH_MODEL`, `CBH_BASE_URL`, `CBH_COMMAND` and the other `CBH_*` variables, or from `~/.config/comments-by-humans/agent.json` with the same keys.
- **Tool calling:** models with native tool calling use it. For models without it, and always for `--provider command`, the agent uses a plain-text tool protocol (`--tool-mode text`) in which the model writes `<tool name="write_file" path="...">...</tool>` blocks.
- **Shell commands:** `run_command` asks before running a shell command unless you pass `--yes`.
- **Anthropic API settings:** on the Anthropic API, the agent sends effort `high` for current models (`--effort`). It also opts into server-side refusal fallback for Claude Opus 5, Opus 5.5, Sonnet 5.5 and Fable 5.1 (`--no-fallbacks` to turn it off).
- **History:** history is append-only, as current reasoning models require.

The agent and the plugin share the gate, the state in `.comments-by-humans/`, the rubric and the review reports, so you can switch between them on the same project.

## Use it in Claude Code

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

In Claude Code, six hook entries call one script, `scripts/gate.py`. It reads `.comments-by-humans/state.json` and exits at once when the mode is off. The standalone agent calls the same checks through `gate.py`'s library API (`api_pre_tool`, `api_post_tool`, `api_prompt`, `api_command`, `api_cli`) before and after each of its own tools, with the same results.

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

- **Grading can be lenient, and it varies by model.** The gate guarantees that a comment exists and that you submitted it; only the model judges whether it is good. The rubric is fixed and strict depth adds a live follow-up. Run the eval suite below against the model you plan to use: it checks that weak and wrong comments are rejected.
- **Chunk extents are heuristic.** A chunk runs from its placeholder through the first statement below it, everything nested inside it, and any line glued to it without a blank line. This works across brace and indentation languages without a parser, but unusual layouts can make a chunk larger or smaller than intended.
- **Shell writes are caught heuristically.** While unlocked, the Bash hook blocks redirects, `tee`, in-place `sed`/`perl`, copies and patches into source files. A determined script could still write a file in a way the heuristic misses. While locked, every command except the gate CLI is denied.
- **Unknown file types are not gated.** Files whose comment syntax the plugin does not know are written freely and logged as ungated. Notebook edits are logged the same way.
- **Your own edits are yours.** The gate governs the assistant, not you. If you change a chunk's code while it is locked, approval refuses until you run build (or review) again to accept the new code.
- **The standalone agent is deliberately small.** It has seven tools, no subagents, no MCP, no web access and no context compaction, so very long sessions can outgrow a small model's context window. Start a new session when that happens; the gate's state carries over.

## Development

```
comments-by-humans/
├── .claude-plugin/plugin.json
├── skills/                    build, review, pause, status
├── hooks/hooks.json
├── scripts/
│   ├── gate.py                gate engine: state machine, Claude Code hook entry points, gate CLI,
│   │                          and the library API the agent calls
│   └── comments.py            comment syntax per language, markers, chunk extents, review chunker
├── agent/                     the model-agnostic front end
│   ├── cbh.py                 command line and interactive session
│   ├── harness.py             agent loop, gated tools, text tool protocol
│   ├── providers.py           anthropic, openai-compatible and command providers, presets
│   ├── prompt.md              the system prompt: build, review, rubric, questioning rules
│   └── run_evals.py           runs evals/ cases against the agent with any provider
├── tests/
│   ├── test_gate.py           every hook driven with real-shaped payloads (no model calls)
│   ├── test_agent.py          the agent with scripted models and mock provider servers
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

Model-graded evals. For the Claude Code plugin (needs the OS sandbox for Bash; on Linux install `bubblewrap` and `socat`):

```
claude plugin eval ./comments-by-humans --scaffold --allow-tools "Bash(python3 *)" Write Edit
```

The same cases against the standalone agent, with any provider and any judge model:

```
python3 comments-by-humans/agent/run_evals.py --provider openai --model <model> --runs 3
python3 comments-by-humans/agent/run_evals.py --provider ollama --model <model> --judge-provider anthropic
```

End-to-end runs with a scripted learner (each costs real model calls). `--frontend agent` drives the standalone agent with the provider flags above, and `--learner-command` lets any command-line model play the learner:

```
python3 comments-by-humans/evals/learner/drive.py --profile weak-first --expect-chunks 5 \
  --task "Create textstats.py with five functions, each its own chunk: ..."
python3 comments-by-humans/evals/learner/drive.py --profile diligent --review src/ --workdir path/to/repo
python3 comments-by-humans/evals/learner/drive.py --frontend agent --provider openai --model <model> \
  --learner-command "llm -m <model>" --profile wrong-first --expect-chunks 3 --task "..."
```

Profiles live in `evals/learner/profiles.json`: `diligent`, `weak-first`, `wrong-first` and `pleads`.

Install test on a clean home directory:

```
comments-by-humans/tests/install_test.sh                      # this checkout
comments-by-humans/tests/install_test.sh aidanfritzke/jarvis   # the published marketplace
```

The design spec is in [`docs/comments-by-humans-spec.md`](../docs/comments-by-humans-spec.md).
