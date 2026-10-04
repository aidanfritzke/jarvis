"""Model providers for the comments-by-humans agent. Python standard library only.

A provider answers one question: given the system prompt, the conversation so far and the
tools, what does the model say next? Three kinds cover any model:

* anthropic   The Anthropic Messages API, with native tool use.
* openai      Any OpenAI-compatible Chat Completions API, with native tool calls: OpenAI,
              Gemini, OpenRouter, Groq, Mistral, DeepSeek, xAI, Together, Ollama, LM Studio,
              vLLM, llama.cpp and other self-hosted servers.
* command     Any command-line model. The conversation goes to the command's stdin as a
              transcript and its stdout is the reply. Tools use the agent's text protocol.

Conversation messages are provider-neutral dicts:

  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "calls": [{"id", "name", "args"}], "raw": ..., "raw_kind": str}
  {"role": "tool", "id": str, "name": str, "content": str, "error": bool}

History is append-only: an assistant turn is sent back exactly as the provider returned it
("raw"), which current models require for their reasoning blocks.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid


class ProviderError(Exception):
    pass


class Reply:
    def __init__(self, text="", calls=None, raw=None, stop=None, model=None, usage=None):
        self.text = text or ""
        self.calls = calls or []
        self.raw = raw
        self.stop = stop
        self.model = model
        self.usage = usage or {}


class Provider:
    kind = "base"
    native_tools = True
    label = "provider"

    def chat(self, system, messages, tools=None):
        raise NotImplementedError


def _post(url, body, headers, timeout, retries=3):
    data = json.dumps(body).encode("utf-8")
    hdrs = {"content-type": "application/json", "user-agent": "comments-by-humans-agent"}
    hdrs.update(headers)
    delay = 2.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:2000]
            retryable = e.code in (408, 409, 429) or e.code >= 500
            if retryable and attempt < retries:
                wait = e.headers.get("retry-after")
                time.sleep(float(wait) if wait and wait.replace(".", "", 1).isdigit() else delay)
                delay *= 2
                continue
            raise ProviderError("HTTP %d from %s: %s" % (e.code, url, detail))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise ProviderError("cannot reach %s: %s" % (url, e))
    raise ProviderError("unreachable")


# ---------------------------------------------------------------------------
# Anthropic Messages API

# Models whose Claude API requests opt into server-side refusal fallback by default.
_FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
_EFFORT_MODELS = ("claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-opus-4-6",
                  "claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-4-6")


class AnthropicProvider(Provider):
    kind = "anthropic"

    def __init__(self, model, api_key=None, auth_token=None, base_url="https://api.anthropic.com",
                 max_tokens=16000, effort="default", fallbacks=None, timeout=600):
        self.model = model
        self.api_key = api_key
        self.auth_token = auth_token
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        if effort == "default":
            effort = "high" if model.startswith(_EFFORT_MODELS) else None
        self.effort = effort or None
        if fallbacks is None:
            fallbacks = self.base_url == "https://api.anthropic.com" and model in _FALLBACK_MODELS
        self.fallbacks = fallbacks
        self.timeout = timeout
        self.label = "anthropic:%s" % model

    def _messages(self, messages):
        out = []
        for m in messages:
            if m["role"] == "user":
                role, blocks = "user", [{"type": "text", "text": m["content"]}]
            elif m["role"] == "assistant":
                role = "assistant"
                if m.get("raw_kind") == self.kind and m.get("raw") is not None:
                    blocks = list(m["raw"])
                else:
                    blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                    blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                               for c in m.get("calls", [])]
            else:
                role = "user"
                block = {"type": "tool_result", "tool_use_id": m["id"], "content": m["content"]}
                if m.get("error"):
                    block["is_error"] = True
                blocks = [block]
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})
        return out

    def chat(self, system, messages, tools=None):
        body = {"model": self.model, "max_tokens": self.max_tokens, "system": system,
                "messages": self._messages(messages)}
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"],
                              "input_schema": t["parameters"]} for t in tools]
        if self.effort:
            body["output_config"] = {"effort": self.effort}
        headers = {"anthropic-version": "2023-06-01"}
        betas = []
        if self.api_key:
            headers["x-api-key"] = self.api_key
        elif self.auth_token:
            headers["authorization"] = "Bearer " + self.auth_token
            betas.append("oauth-2025-04-20")
        if self.fallbacks:
            body["fallbacks"] = "default"
            betas.append("server-side-fallback-2026-07-01")
        if betas:
            headers["anthropic-beta"] = ",".join(betas)
        try:
            data = _post(self.base_url + "/v1/messages", body, headers, self.timeout)
        except ProviderError as e:
            if self.fallbacks and "fallback" in str(e).lower() and "HTTP 400" in str(e):
                self.fallbacks = False  # a gateway or account that does not take the parameter
                return self.chat(system, messages, tools)
            raise
        content = data.get("content") or []
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        calls = [{"id": b["id"], "name": b["name"], "args": b.get("input") or {}}
                 for b in content if b.get("type") == "tool_use"]
        stop = data.get("stop_reason")
        if stop == "refusal" and not text:
            text = "[The model declined this request.]"
        if stop == "max_tokens" and not calls:
            text += "\n[The reply hit the max_tokens limit.]"
        return Reply(text, calls, raw=content, stop=stop, model=data.get("model"), usage=data.get("usage"))


# ---------------------------------------------------------------------------
# OpenAI-compatible Chat Completions


class OpenAIProvider(Provider):
    kind = "openai"

    def __init__(self, model, base_url, api_key=None, timeout=600, extra=None, label=None):
        if not model:
            raise ProviderError("this provider needs --model")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.extra = extra or {}
        self.label = label or "openai-compatible:%s" % model

    def _messages(self, system, messages):
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                if m.get("raw_kind") == self.kind and m.get("raw") is not None:
                    out.append(dict(m["raw"]))
                    continue
                msg = {"role": "assistant", "content": m.get("content") or None}
                if m.get("calls"):
                    msg["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                        "name": c["name"], "arguments": json.dumps(c["args"])}} for c in m["calls"]]
                elif msg["content"] is None:
                    msg["content"] = ""
                out.append(msg)
            else:
                out.append({"role": "tool", "tool_call_id": m["id"], "content": m["content"]})
        return out

    def chat(self, system, messages, tools=None):
        body = {"model": self.model, "messages": self._messages(system, messages)}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
                for t in tools]
        body.update(self.extra)
        headers = {"authorization": "Bearer " + self.api_key} if self.api_key else {}
        data = _post(self.base_url + "/chat/completions", body, headers, self.timeout)
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("unexpected response: %s" % json.dumps(data)[:1000])
        content = msg.get("content")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        calls, echo = [], []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments")
            if isinstance(raw_args, dict):  # Ollama and a few others send an object
                args, raw_args = raw_args, json.dumps(raw_args)
            else:
                try:
                    args = json.loads(raw_args or "{}")
                    if not isinstance(args, dict):
                        args = {"__invalid__": raw_args}
                except ValueError:
                    args = {"__invalid__": raw_args}
            cid = tc.get("id") or "call_%s" % uuid.uuid4().hex[:12]
            calls.append({"id": cid, "name": fn.get("name") or "", "args": args})
            echo.append({"id": cid, "type": "function",
                         "function": {"name": fn.get("name") or "", "arguments": raw_args or "{}"}})
        raw = {"role": "assistant", "content": content if content else (None if echo else "")}
        if echo:
            raw["tool_calls"] = echo
        return Reply(content or "", calls, raw=raw, stop=choice.get("finish_reason"),
                     model=data.get("model"), usage=data.get("usage"))


# ---------------------------------------------------------------------------
# Any command-line model, through the text tool protocol


def render_transcript(system, messages):
    parts = ["[system]\n" + system.strip()]
    for m in messages:
        parts.append("[%s]\n%s" % (m["role"], m["content"].strip()))
    parts.append("Write the assistant's next message, and nothing else.")
    return "\n\n".join(parts) + "\n"


class CommandProvider(Provider):
    kind = "command"
    native_tools = False

    def __init__(self, command, timeout=900):
        if not command:
            raise ProviderError("the command provider needs --command")
        self.command = command
        self.timeout = timeout
        self.label = "command:%s" % command.split()[0]

    def chat(self, system, messages, tools=None):
        prompt = render_transcript(system, messages)
        try:
            out = subprocess.run(self.command, shell=True, input=prompt, capture_output=True, text=True,
                                 timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise ProviderError("the model command timed out after %ds" % self.timeout)
        if out.returncode != 0:
            raise ProviderError("the model command exited %d: %s" % (out.returncode, out.stderr[-1500:]))
        return Reply(out.stdout.strip(), raw=None, stop="end_turn")


# ---------------------------------------------------------------------------
# Presets and construction

PRESETS = {
    "anthropic": {"kind": "anthropic", "base_url": "https://api.anthropic.com", "key_env": "ANTHROPIC_API_KEY",
                  "model": "claude-opus-5-5"},
    "openai": {"kind": "openai", "base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY"},
    "gemini": {"kind": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "key_env": "GEMINI_API_KEY"},
    "openrouter": {"kind": "openai", "base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY"},
    "groq": {"kind": "openai", "base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
    "mistral": {"kind": "openai", "base_url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY"},
    "deepseek": {"kind": "openai", "base_url": "https://api.deepseek.com/v1", "key_env": "DEEPSEEK_API_KEY"},
    "xai": {"kind": "openai", "base_url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY"},
    "together": {"kind": "openai", "base_url": "https://api.together.xyz/v1", "key_env": "TOGETHER_API_KEY"},
    "ollama": {"kind": "openai", "base_url": "http://localhost:11434/v1", "key_env": None},
    "lmstudio": {"kind": "openai", "base_url": "http://localhost:1234/v1", "key_env": None},
    "openai-compatible": {"kind": "openai", "base_url": None, "key_env": "OPENAI_API_KEY", "key_optional": True},
    "command": {"kind": "command"},
}


def make_provider(name, model=None, base_url=None, api_key_env=None, command=None, effort="default",
                  fallbacks=None, max_tokens=16000, timeout=600):
    """Build a provider from a preset name plus overrides."""
    if name not in PRESETS:
        raise ProviderError("unknown provider %r; choose one of: %s" % (name, ", ".join(sorted(PRESETS))))
    preset = PRESETS[name]
    kind = preset["kind"]
    if kind == "command":
        return CommandProvider(command, timeout=timeout)
    base = base_url or preset.get("base_url")
    if not base:
        raise ProviderError("provider %s needs --base-url" % name)
    key_env = api_key_env or preset.get("key_env")
    key = os.environ.get(key_env) if key_env else None
    if kind == "anthropic":
        token = None if key else os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if not key and not token:
            raise ProviderError("set %s (or ANTHROPIC_AUTH_TOKEN) for the anthropic provider" % key_env)
        return AnthropicProvider(model or preset["model"], api_key=key, auth_token=token, base_url=base,
                                 max_tokens=max_tokens, effort=None if effort == "none" else effort,
                                 fallbacks=fallbacks, timeout=timeout)
    if key_env and not key and preset.get("key_env") and not preset.get("key_optional"):
        raise ProviderError("set %s for the %s provider" % (key_env, name))
    return OpenAIProvider(model or preset.get("model"), base, api_key=key, timeout=timeout,
                          label="%s:%s" % (name, model or preset.get("model")))
