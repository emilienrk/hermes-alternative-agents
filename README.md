# hermes-alternative-agents

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin to talk, from the same Telegram
chat as Hermes, to three other coding agents:

| Agent | Command prefix | Pays with |
|---|---|---|
| [Antigravity](https://antigravity.google/) (`agy`, Google's agent CLI) | `/agy` | your Antigravity subscription |
| [Claude Code](https://code.claude.com/) | `/cc` | your Claude subscription |
| [opencode](https://opencode.ai/) on DeepSeek | `/oc` | a DeepSeek key of its own |

Messages routed to an agent are taken before Hermes' dispatch: they cost no Hermes LLM tokens.

The agents are not bare CLIs behind a bot. Before every turn the plugin hands them what Hermes
knows (its personality, memory and recent Telegram thread, its skills, its MCP servers), and a
bundled MCP bridge gives them Hermes' own tools: shared memory, session search, skill writing, cron
jobs, text-to-speech, Telegram messages. On top of the chat come scheduled tasks an agent runs
instead of Hermes' paid LLM, voice notes in and out, presets (saved prompts that open a
conversation), and handing a conversation from Hermes to an agent and back.

## Contents

- [How messages are routed](#how-messages-are-routed)
- [Commands](#commands)
- [The agents](#the-agents)
- [Presets](#presets)
- [Scheduled tasks](#scheduled-tasks)
- [Install](#install)
- [Settings](#settings)
- [Security](#security)
- [Data](#data)
- [Development](#development)

## How messages are routed

Only from the one Telegram chat you configure (examples with agy; `/cc…` and `/oc…` work the same):

| You send | Goes to |
|---|---|
| a reply to one of an agent's messages | that agent |
| `/agy_btw <question>`, or a reply to a btw answer | the agent's side conversation |
| anything, while agy mode is on (`/agy_on`) | agy, except `/` commands and messages starting with "Hermes" |
| `/agy <message>` | agy |
| anything else | Hermes, as usual |

One mode at a time: `/cc_on` turns agy mode off, and the other way round. While a mode is on, a
banner stays pinned at the top of the chat; its button (**Back to Hermes**, `leave_button`) goes back to Hermes, as `/agy_off` does.
At each gateway start the pinned banner is checked against the mode that is on, and fixed (with a
message) when it shows another one. In agy mode, `/stop` and `/new` act on agy. A reply
still reaches the agent you reply to, whatever the mode.

Photos, files and voice notes ride along. Voice notes go to agy as audio (default) or are
transcribed locally by Hermes' Whisper first (`/agy_voice local`); Claude Code and opencode take no
audio, so they always get the transcript. Messages sent while an agent works are queued and handed
over together.

## Commands

The `/cc_…` (Claude Code) and `/oc_…` (opencode) commands are the same as agy's.

| Command | |
|---|---|
| `/agy [message]` | Status card, or a message for agy |
| `/agy_on`, `/agy_off` | agy mode on (pinned banner) / back to Hermes |
| `/agy_new [message]` | New agy conversation |
| `/agy_conv [n]` | Pick a recent conversation and resume it, in agy mode (never one a preset opened) |
| `/agy_stop` | Stop the running turn and drop the queue (`/stop` too, in agy mode) |
| `/agy_retry` | Send your last message again |
| `/agy_btw <question>` | Side question, answered right away even while agy works, in a separate conversation that knows the main one's last exchanges. Reply to its answer to continue it |
| `/agy_model [n or name]` | Pick the model |
| `/agy_effort [level]` | Reasoning effort: agy's model variant (`gemini-3.8-flash-high` → `-low`), Claude Code's `--effort` (low to max), opencode's variant (low, high, max) |
| `/agy_verbose [on\|off]` | Live status of the agent's actions, or just its answer |
| `/agy_voice [direct\|local]` | Voice notes as audio for agy, or transcribed locally |
| `/agy_voice reply [off\|mirror\|always]` | The answer as a voice note too (mirror: when you sent one) |
| `/agy_handoff [hermes\|cc\|oc] [message]` | agy takes over, in a new conversation seeded with the Hermes thread or the other agent's last exchanges (buttons when another agent has a conversation; a preset's is never offered), then agy mode on |
| `/agy_back [message]` | Back to Hermes, which gets a recap of the agy conversation |
| `/agy_task` | Scheduled tasks: list; `add <when> \| <prompt>`, `rm <n>`, `run <n>` |
| `/agy_usage` | Quota left (Antigravity's weekly and 5-hour limits; Claude's as the last turn reported them; the DeepSeek balance), then turns, tokens and time through the plugin |
| `/agy_doctor` | Check every link of the chain |
| `/<preset>` | Open a conversation with a saved prompt (see [Presets](#presets)) |
| `/preset` | List, save or remove presets |

## The agents

### Antigravity (agy)

agy works in its workspace (`~/agy-chat`), whose generated `.agents/` holds this channel's rules, the
MCP config (the bridge and Hermes' MCP servers) and links to Hermes' skills. The first message of a
conversation carries the context (time, Hermes' memory and thread). Add the workspace to agy's
`trustedWorkspaces` (see [Install](#install)), or agy ignores the generated files.

### Claude Code

Claude Code runs apart from your own Claude Code setup: no settings, `CLAUDE.md`, plugins, hooks or
auto-memory of yours (`--setting-sources ""`, `--strict-mcp-config`). What it gets is generated
under `<claude_workspace>/.hermes/`: a system prompt (this channel's rules, the tools, Hermes'
personality), an MCP config and a plugin whose skills are links to Hermes' skills (`hermes:<name>`).
Claude Code keeps a conversation's system prompt as it was on its first turn, so what changes (the
time, Hermes' memory, new messages of your thread with Hermes) opens each message instead, and only
when it changed.

It runs on your Claude subscription: API keys (`ANTHROPIC_*`) are removed from its environment. Its
commits carry no Claude attribution. `AskUserQuestion`, plan mode and Claude Code's session-bound
scheduling tools are off (nobody answers mid-turn, the process ends with the turn): `/cc_task` and
the `cc_task` tool schedule its work.

### opencode (DeepSeek)

opencode runs on DeepSeek (`deepseek/deepseek-flash` by default; `/oc_model` lists DeepSeek's
models). It pays with a key of its own, never Hermes': put it in
`~/.hermes/alternative-agents/.env` (setting `opencode_env_file`), mode 600:

```bash
mkdir -m 700 -p ~/.hermes/alternative-agents
install -m 600 /dev/null ~/.hermes/alternative-agents/.env
echo 'DEEPSEEK_API_KEY=sk-…' >> ~/.hermes/alternative-agents/.env
```

The key reaches opencode through its environment only (never `opencode.json`); DeepSeek keys from
the gateway's environment are removed. Without the key, `/oc` says so and runs nothing;
`/oc_doctor` checks the key and the balance.

A turn stops at 30 actions or $0.05 (`opencode_max_actions`, `opencode_max_cost_usd`), with the
actions so far; the conversation stays, "continue" goes on.

opencode runs mostly apart from your own opencode setup: `OPENCODE_CONFIG_DIR` points it at
`<opencode_workspace>/.hermes/`, project config is off, and so is what it would read of Claude
Code's (`~/.claude/CLAUDE.md`, `~/.claude/skills`, `~/.agents/skills`). opencode still merges your
global `~/.config/opencode/opencode.json` (but not into bare conversations): keep there only what
this agent may get too.
That directory gets an `opencode.json`, an `AGENTS.md` (rules, tools, Hermes' personality) and
`skills/`, links to Hermes' skills. Messages go through stdin (no size limit, nothing in `ps`), and
it runs with `--auto`: no permission prompts.

### Hermes' MCP servers, on demand

Claude Code and agy load MCP tools' schemas only when they need them. opencode sends every tool's
schema with each request: with Hermes' two MCP servers (84 tools), that was ~78 KB per request. So,
by default (`mcp_on_demand: [opencode]`), opencode reaches them through the bridge instead, with two
tools: `mcp_tool_info`, whose description lists every MCP tool in one line and which returns a
tool's parameters, and `mcp_tool_call`. The bridge connects with Hermes' own MCP client (same
config, same auth).

Fixed cost of a one-word turn, measured on this setup (88 skills, two MCP servers):

| Agent | Tokens |
|---|---|
| agy 1.4.3 | ~15 000 |
| opencode 1.18.35, MCP servers given directly | ~45 000 |
| opencode 1.18.35, MCP servers on demand | ~23 600 |

## Presets

A preset is a saved prompt that opens a conversation: `/<name>` turns on its agent's mode (opencode
by default), starts a new conversation and sends the prompt as its first message, in the background.
Telegram shows a waiting mark, then the agent's answer, never the prompt. Your next messages go on in
that conversation, under the preset's pinned banner, until you leave the mode, which ends it.

A preset's conversation is private and leaves no trace once over (`/oc_off`, the banner's button,
another mode, `/oc_new`, `/oc_conv`): the plugin deletes it from the agent's own storage (opencode
session, agy's conversation files, Claude Code's transcript), its turns are logged without text,
`/oc_conv` never lists it, a `/oc_btw` or `/cc_handoff` doesn't see it, and the agents' rules tell them
the presets directory and those conversations are off limits. The directory also holds a README saying
so, for any other AI that lands there. Rules, not a lock: an agent runs as your user and can read what
you can.

- `/preset` lists them (name, agent, length; never the prompt).
- `/preset <name>` in reply to a message saves that message as the prompt (new or replaced). A new
  name must not be a command already; it reaches the `/` menu after a gateway restart.
- `/preset rm <name>` removes one.

Prompts are files, `<HERMES_HOME>/alternative-agents/presets/<name>.md` (directory 700, files 600).
The plugin reads one only to send it: through stdin, without the usual context block, and recorded
nowhere else. `/oc_retry` right after sends it again.

On opencode, a preset's conversation is bare: DeepSeek gets the prompt, then your messages as you
wrote them, and nothing else. No opencode system prompt, rules, Hermes context or tools: that
conversation runs from `<opencode_workspace>/.hermes-bare/`, whose agent has no tools and whose
plugin empties the system prompt. Leaving the mode (`/oc_off`, the banner's button, another mode)
ends that conversation: `/oc` then starts an ordinary one. A preset with `bare: false` gets opencode's
usual setup instead. On agy and Claude Code, the prompt opens a conversation with their usual setup.

Optional settings, per preset:

```yaml
presets:
  reviewer: {backend: claude, banner: "🔍 Code review mode", description: "Review the current branch"}
  tutor: {bare: false}  # opencode with its tools and Hermes' context
```

## Scheduled tasks

`/agy_task add every day at 8am | summarize my unread mails` schedules a prompt that agy runs
unattended; its answer arrives in Telegram. The agents can also schedule their own tasks with the
`agy_task`, `cc_task` and `oc_task` tools ("every Monday, check …").

Hermes keeps the schedule: each task is a Hermes cron job in `no_agent` mode whose script, generated
under `<HERMES_HOME>/scripts/alternative-agents-tasks/`, runs the agent. So it costs no Hermes LLM
tokens, it shows up in `hermes cron list`, and a failed run is reported like any cron failure. An
answer that is just `[SILENT]` sends nothing, for checks that should only speak up when something is
wrong. `when` takes Hermes' schedule forms: `every day at 8am`, `weekdays at 7am`, `0 7 * * 1-5`,
`every 6h`, `in 2h` (once), read in Hermes' timezone (`timezone` in `config.yaml`).

## Install

Requirements:

- Hermes Agent ≥ 0.21.6 running its gateway with the Telegram platform.
- For agy: the [Antigravity CLI](https://antigravity.google/), installed for the gateway's user and
  logged in once (run `agy` interactively).
- For Claude Code: the [Claude Code CLI](https://code.claude.com/), installed for that user and logged
  in to a Claude subscription once (`claude`, then `/login`).
- For opencode: the [opencode CLI](https://opencode.ai/), tested with 1.18.35
  (`npm install --prefix ~/.local/share/opencode-cli opencode-ai@1.18.35`, then link
  `~/.local/share/opencode-cli/node_modules/.bin/opencode` into `~/.local/bin`), and a
  [DeepSeek key](#opencode-deepseek).

```bash
hermes plugins install emilienrk/hermes-alternative-agents --enable
hermes config set plugins.entries.alternative-agents.settings.chat_id <your Telegram chat id>
hermes config set plugins.entries.alternative-agents.settings.user_name <your name>
hermes config set plugins.entries.alternative-agents.settings.backends '[agy, claude, opencode]'
```

`chat_id` is your private chat with the bot, whose id is your Telegram user id: the one already in
Hermes' `TELEGRAM_ALLOWED_USERS`, or ask [@userinfobot](https://t.me/userinfobot).

Then:

1. For agy, add its workspace (`~/agy-chat` by default) to `trustedWorkspaces` in
   `~/.gemini/antigravity-cli/settings.json`.
2. Optionally, put the commands in Telegram's `/` menu, in `config.yaml` (Telegram shows at most 100;
   raise `max_commands` if some are cut):
   ```yaml
   platforms:
     telegram:
       extra:
         command_menu:
           max_commands: 80
           priority: [agy, agy_on, agy_off, agy_new, agy_conv, cc, cc_on, cc_off, cc_new, cc_conv,
                      oc, oc_on, oc_off, oc_new, oc_conv, preset]
   ```
3. Restart the gateway, then send `/agy_doctor`, `/cc_doctor` or `/oc_doctor` in Telegram.

Update with `hermes plugins update alternative-agents`, then restart the gateway.

## Settings

Under `plugins.entries.alternative-agents.settings` in `config.yaml` (also editable in the Desktop app):

| Key | Default | |
|---|---|---|
| `chat_id` | — | **Required.** The only Telegram chat routed to the agents. Nothing is routed while it's empty. |
| `user_name` | `the user` | How the agents' context refers to you. |
| `backends` | `[agy]` | Agents reachable from Telegram: `agy`, `claude`, `opencode`. |
| `presets` | `{}` | Per-preset settings: `backend`, `banner`, `description`, `bare` (opencode, default `true`). |
| `machine_notes` | — | Free text about the machine added to the agents' context. |
| `skip_skills` | `[]` | Hermes skill names hidden from the agents. |
| `timeout_minutes` | `30` | Longest turn. |
| **agy** | | |
| `agy_path` | `agy` on `PATH`, else `~/.local/bin/agy` | The agy CLI. |
| `workspace` | `~/agy-chat` | agy's working directory: `inbox/` (your files), `out/` (its files), `.agents/` (generated). |
| `default_model` | `gemini-3.8-flash-high` | Model until `/agy_model` picks another. |
| `mark` | `🛸` | Emoji in agy's footer, status and banner (`""` for none). |
| **Claude Code** | | |
| `claude_path` | `claude` on `PATH`, else `~/.local/bin/claude` | The Claude Code CLI. |
| `claude_workspace` | `~/claude-chat` | Claude Code's working directory: `inbox/`, `out/`, `.hermes/` (generated). |
| `claude_model` | `opus` | Model until `/cc_model` picks another: an alias (`opus`, `sonnet`, `haiku`, `fable`) or a full id. |
| `claude_effort` | `""` | Effort until `/cc_effort` picks another (`""`: Claude Code's default). |
| `claude_mark` | `✳️` | Emoji in Claude Code's footer, status and banner. |
| **opencode** | | |
| `opencode_path` | `opencode` on `PATH`, else `~/.local/bin/opencode` | The opencode CLI. |
| `opencode_workspace` | `~/opencode-chat` | opencode's working directory: `inbox/`, `out/`, `.hermes/` (generated). |
| `opencode_model` | `deepseek/deepseek-flash` | Model until `/oc_model` picks another. |
| `opencode_effort` | `""` | Model variant until `/oc_effort` picks another (`""`: the model's default). |
| `opencode_mark` | `🐋` | Emoji in opencode's footer, status and banner. |
| `opencode_env_file` | `~/.hermes/alternative-agents/.env` | Holds `DEEPSEEK_API_KEY`, opencode's own key. |
| `opencode_max_actions` | `30` | A turn stops after this many actions (`0`: no limit). |
| `opencode_max_cost_usd` | `0.05` | A turn stops once it has cost this much (`0`: no limit). |
| **Display** | | |
| `react_working`, `react_queued` | `👀`, `✍` | Reactions on your messages while an agent works / while they wait (`""` for none). |
| `footer` | `true` | `— 🛸 agy · 2 min` under answers. |
| `verbose` | `false` | Live status of the agent's actions by default. |
| `leave_button` | `Back to Hermes` | Label of the button under the pinned mode banner. |
| **Voice** | | |
| `voice_input` | `direct` | `direct`: the audio goes to agy (Google); `local`: Hermes' Whisper transcribes it first. Claude Code and opencode always get `local`. |
| `voice_reply` | `off` | `off`, `mirror` (voice answer when you sent a voice note), `always`. |
| `tts_provider` | `""` | `""`: Hermes' TTS settings; `edge`: Edge TTS directly (free, no key; needs `edge-tts` ≥ 7.2.8 in Hermes' venv). |
| `tts_voice` | Hermes' `tts.edge.voice` | Edge voice, e.g. `fr-FR-DeniseNeural`. |
| **What the agents get from Hermes** | | |
| `share_soul`, `share_memory`, `share_thread` | `true` | Personality, memory, recent Telegram thread, in the agents' context. |
| `share_skills`, `share_mcp_servers` | `true` | Hermes' skills and MCP servers. |
| `mcp_on_demand` | `[opencode]` | Agents that reach Hermes' MCP servers through the bridge, their tools described on demand (see [above](#hermes-mcp-servers-on-demand)). |
| `bridge_tools` | all | Tools of the bundled bridge: `memory`, `session_search`, `skill_manage`, `cronjob_manage`, `text_to_speech`, `telegram_send`, `hermes_thread`, `tasks` (`agy_task` / `cc_task` / `oc_task`). `[]` turns it off. |

`/agy_verbose` and `/agy_voice` (and `/cc_…`, `/oc_…`) override the display and voice defaults from
Telegram, per agent. The agents' context is written in English; they answer in your language.

## Security

The agents run as the gateway's user without permission prompts (agy with
`--dangerously-skip-permissions`, Claude Code with `--permission-mode bypassPermissions`, opencode
with `--auto`): **whoever can write in the configured chat can run anything that user can**. Set
`chat_id` to your own DM with the bot, and think twice before running the gateway as a user with
`sudo`.

API keys from the gateway's environment are not passed to the agents, but their MCP configs hold the
bearer tokens of Hermes' MCP servers (written with mode 600). opencode's DeepSeek key is in its
environment, so the commands it runs can read it.

## Data

Plugin state (mode, model, conversation, tasks) and the turn logs live in
`<HERMES_HOME>/plugin-data/alternative-agents/` (`state.json` and `turns.log` for agy,
`claude-…` for Claude Code, `opencode-…` for opencode), outside the install directory, so updates keep
them.

### Coming from agy-chat

The plugin was called `agy-chat`. Stop the gateway, move `plugin-data/agy-chat/` to
`plugin-data/alternative-agents/` and `plugins/agy-chat/` to `plugins/alternative-agents/`, rename
`agy-chat` to `alternative-agents` in `plugins.enabled` and `plugins.entries` of `config.yaml`, then
recreate your tasks (`/agy_task`): their scripts ran `plugins/agy-chat/agy_run.py`.

## Development

The tests drive whole turns against a fake agy (`tests/fake_agy.py`), a fake Claude Code
(`tests/fake_claude.py`), a fake opencode (`tests/fake_opencode.py`) and a fake Telegram adapter.
They import Hermes' gateway modules, so run them with Hermes' interpreter:

```bash
~/.hermes/hermes-agent/venv/bin/python tests/test_chat.py
hermes plugins validate .
ruff check .
```

## License

MIT
