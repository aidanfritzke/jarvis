# jarvis

A Claude Code plugin marketplace.

| Plugin | What it does |
| --- | --- |
| [comments-by-humans](comments-by-humans/) | Claude writes the code; you explain it. A hook-enforced gate stops Claude after every chunk until your own comment above it passes a fixed rubric. Also reviews existing code one chunk at a time. |

## Install

```
/plugin marketplace add aidanfritzke/jarvis
/plugin install comments-by-humans@jarvis
```

To use a branch before it is merged, pin it when adding the marketplace, for example `aidanfritzke/jarvis#jarvis-comments-by-humans`.

## Layout

```
.claude-plugin/marketplace.json   the marketplace catalog
comments-by-humans/               the plugin (see its README)
docs/                             design documents
```
