# Model API formats

In **Server Settings & Agents**, choose the provider's API format before fetching or saving a model:

| Format | Endpoint | Runtime |
| --- | --- | --- |
| Anthropic (Messages) | `/v1/messages` | Claude Code |
| OpenAI Chat (Chat Completions) | `/v1/chat/completions` | Pi |
| OpenAI Responses | `/v1/responses` | Codex |

The format controls model discovery authentication and runtime selection, independently of the model name. Existing CTF agents default to `auto`, which preserves their original Codex routing.

Enter the provider base URL, including any gateway prefix. Full endpoint URLs are also accepted. Model discovery is optional: enter the exact model ID manually if the provider does not expose a model list. Leave the API key blank when editing to keep the saved key.

For Windows API mode, install Pi from this CTF directory:

```powershell
npm install --prefix .cairn-launcher/pi-runtime @mariozechner/pi-coding-agent@0.73.0
```

Node.js must be on PATH. The host runner uses project-local `.cairn-pi` session storage and reads credentials from the worker environment. It does not require `/bin/sh` to launch Pi. Tool execution may still require the relevant host tools.

CTF runs on port 8001; Pentest runs on port 8000. Each has its own database, dependencies and runtime configuration.
