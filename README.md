# jarvis

A Claude Code plugin marketplace.

| Plugin | What it does |
| --- | --- |
| [comments-by-humans](comments-by-humans/) | The model writes the code; you explain it. A mechanical gate stops the assistant after every chunk until your own comment above it passes a fixed rubric. Also reviews existing code one chunk at a time. Runs as a Claude Code plugin, or as a standalone agent with any model. |

## Install

```
/plugin marketplace add aidanfritzke/jarvis
/plugin install comments-by-humans@jarvis
```

To use a branch before it is merged, pin it when adding the marketplace, for example `aidanfritzke/jarvis#jarvis-comments-by-humans-model-agnostic`.

For any other model, run the standalone agent: `python3 comments-by-humans/agent/cbh.py --help`.

## Layout

```
.claude-plugin/marketplace.json   the marketplace catalog
comments-by-humans/               the plugin (see its README)
docs/                             design documents
```
