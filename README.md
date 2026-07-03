# Hermes Claude Code Relay Plugin

Standalone Hermes Agent plugin that adds Telegram/Discord/CLI slash commands for relaying work to the official `claude` / Claude Code CLI.

This is useful when Hermes itself is running a lightweight model or when Anthropic direct API/OAuth requests are being routed into extra-usage billing, but the official Claude Code CLI works with the user's plan, MCP servers, and project config.

## Commands

| Command | Purpose |
|---|---|
| `/claude <prompt>` | Send a prompt to Claude Code and keep sticky Claude mode active for this chat. |
| `/claudemodel <model>` | Set the Claude Code model for this chat's relay only. Does **not** change Hermes' own `/model`. |
| `/endclaude` | Exit sticky Claude mode; future plain messages go back to Hermes. |
| `/stopclaude` | Stop the currently running Claude Code relay subprocess for this chat, while keeping sticky Claude mode active for the next prompt. |

## Requirements

- Hermes Agent with plugin support.
- Official Claude Code CLI installed as `claude` on PATH, or at `~/.local/bin/claude(.exe)`.
- Claude Code already authenticated (`claude` works from the terminal).
- A Hermes gateway restart after enabling the plugin.

## Install from GitHub

```bash
cd "${HERMES_HOME:-$HOME/.hermes}"
mkdir -p plugins
git clone https://github.com/RoRoGitr/hermes-claude-relay-plugin.git plugins/claude-code-relay
hermes plugins enable claude-code-relay
hermes gateway restart
```

On Roman's Windows AppData install, `HERMES_HOME` is usually:

```text
C:\Users\roman\AppData\Local\hermes
```

So the plugin folder should end up at:

```text
C:\Users\roman\AppData\Local\hermes\plugins\claude-code-relay
```

## Usage

Start a new Claude Code relay session in a project folder:

```text
/claude --project ERE-Software fix the failing tests
```

Continue the same Claude Code session by sending a plain message in the same chat:

```text
also update the docs
```

Change only the relay model for this chat:

```text
/claudemodel fable
```

Supported friendly aliases:

| Alias | Model |
|---|---|
| `fable`, `fable5`, `fable-5` | `claude-fable-5` |
| `opus`, `opus4.8`, `opus-4.8` | `claude-opus-4-8` |
| `sonnet`, `sonnet5`, `sonnet-5` | `claude-sonnet-5` |
| `haiku`, `haiku4.5`, `haiku-4.5` | `claude-haiku-4-5-20251001` |

Exit sticky mode:

```text
/endclaude
```

Stop active Claude Code run:

```text
/stopclaude
```

## Project root

Bare `--project` names are resolved under:

```text
~/Claude/RoClaude_Code/Projects
```

Override this for other machines:

```bash
export CLAUDE_RELAY_PROJECT_ROOT="/path/to/projects"
```

Then restart Hermes gateway.

## Notes

- `/claudemodel` affects the relayed Claude Code subprocess only.
- Hermes' own model is still controlled by `/model`.
- Sticky state is stored in `${HERMES_HOME}/claude_relay_sessions.json`.
- The relay runs Claude Code with `--permission-mode bypassPermissions`, so only enable this plugin for trusted users/chats.
