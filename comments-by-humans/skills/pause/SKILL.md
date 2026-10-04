---
name: pause
description: Pause the comments-by-humans gate until the human runs build or review again. Only the human can run this command.
disable-model-invocation: true
---

# comments-by-humans: pause

The human paused the gate. A hook recorded the pause when they typed this command, and logged it.

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate.py" status`

Confirm in one or two lines that the gate is paused, that code written while paused is logged as ungated, and that `/comments-by-humans:build` or `/comments-by-humans:review` turns it back on (any chunk that was waiting stays pending). Then help with whatever the human asks next.
