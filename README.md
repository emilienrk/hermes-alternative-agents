# hermes-agy-chat

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin to talk to
[Antigravity](https://antigravity.google/) (`agy`, Google's agent CLI) or
[Claude Code](https://code.claude.com/) from Telegram, as an alternative to Hermes' own LLM.
Messages routed to an agent are taken before Hermes' dispatch: they cost no Hermes LLM tokens,
only your Antigravity or Claude subscription.

The agents are not bare CLIs behind a bot: before every turn the plugin hands them what Hermes
knows — Hermes' personality, memory and recent Telegram thread, its skills, its MCP servers —
and a bundled MCP bridge gives them a few Hermes tools (shared memory, session search, skill
writing, cron jobs, text-to-speech, Telegram messages). Each of these can be turned off.

It also brings what makes an agent more than a chat: scheduled tasks that an agent runs instead
of Hermes' paid LLM, voice replies, and handing a conversation over from Hermes to an agent and back.

Enable the agents you want with the `backends` setting (`[agy]` by default, `[agy, claude]` for
both). Each has its own commands — `/agy…` for agy, `/cc…` for Claude Code — conversations and
settings.

## What reaches an agent

Only from the one Telegram chat you configure (examples with agy; `/cc…` works the same for Claude Code):

| You send | Goes to |
|---|---|
| a reply to one of an agent's messages | that agent |
| `/agy_btw <question>`, or a reply to a btw answer | the agent's side conversation (see `/agy_btw`) |
| anything, while agy mode is on (`/agy_on`) | agy, except `/` commands and messages starting with "Hermes" |
| `/agy <message>` | agy |
| anything else | Hermes, as usual |

One mode at a time: `/cc_on` turns agy mode off, and the other way round. A reply still reaches the
agent you reply to, whatever the mode.

Photos, files and voice notes ride along. Voice notes go to agy as audio (default) or are
transcribed locally by Hermes' Whisper first (`/agy_voice local`); Claude Code takes no audio, so
they are always transcribed for it. Messages sent while an agent works are queued and handed over
together.

## Commands

The `/cc_…` commands are the same for Claude Code.

| Command | |
|---|---|
| `/agy [message]` | Status card, or a message for agy |
| `/agy_on`, `/agy_off` | agy mode on (pinned banner) / back to Hermes |
| `/agy_new [message]` | New agy conversation |
| `/agy_stop` | Stop the running turn and drop the queue (`/stop` too, in agy mode) |
| `/agy_model [n or name]` | Pick agy's model |
| `/agy_effort [low\|medium\|high]` | Reasoning effort: switches to that variant of the current model (`gemini-3.8-flash-high` → `-low`); for Claude Code, its `--effort` (low to max) |
| `/agy_verbose [on\|off]` | Live status of agy's actions, or just its answer |
| `/agy_voice [direct\|local]` | Voice notes as audio for agy, or transcribed locally |
| `/agy_voice reply [off\|mirror\|always]` | agy's answer as a voice note too (mirror: when you sent one) |
| `/agy_conv [n]` | Resume a past conversation |
| `/agy_retry` | Send your last message again |
| `/agy_btw <question>` | Side question, answered right away even while agy works, in a separate conversation that knows the main one's last exchanges (Claude Code: a copy of the main conversation, at low effort). Reply to its answer to continue it |
| `/agy_handoff [message]` | Hand the Hermes conversation over to agy (agy mode on, seeded with the thread) |
| `/agy_back [message]` | Back to Hermes, which gets a recap of the agy conversation |
| `/agy_task` | List agy's scheduled tasks; `add <when> \| <prompt>`, `rm <n>`, `run <n>` |
| `/agy_usage` | Antigravity quota left (weekly and 5-hour limits, like agy's `/usage`), then turns, tokens and time through the plugin. `/cc_usage`: the Claude subscription's 5-hour and weekly limits as the last turn reported them |
| `/agy_doctor` | Check every link of the chain |

## Claude Code

Claude Code runs apart from your own Claude Code setup: no settings, `CLAUDE.md`, plugins, hooks
or auto-memory of yours (`--setting-sources ""`, `--strict-mcp-config`). What it gets is generated
under `<claude_workspace>/.hermes/`: a system prompt (this channel's rules, the tools, Hermes'
personality), an MCP config (the bridge and Hermes' MCP servers) and a plugin whose skills are links
to Hermes' skills (`hermes:<name>`). Claude Code keeps a conversation's system prompt as it was on
its first turn, so what changes — the time, Hermes' memory, new messages of your thread with
Hermes — opens each message instead, and only when it changed.

It runs on your Claude subscription: API keys (`ANTHROPIC_*`) are removed from its environment.
Its commits carry no Claude attribution. `AskUserQuestion`, plan mode and Claude Code's own
session-bound scheduling tools are off (nobody answers mid-turn, the process ends with the turn):
`/cc_task` and the `cc_task` tool schedule its work.

## Scheduled tasks

`/agy_task add every day at 8am | summarize my unread mails` schedules a prompt that agy runs
unattended; its answer arrives in Telegram. agy can also schedule its own tasks with the
`agy_task` tool ("every Monday, check …"). `/cc_task` and `cc_task` do the same for Claude Code.

Hermes keeps the schedule: each task is a Hermes cron job in `no_agent` mode whose script, generated
under `<HERMES_HOME>/scripts/agy-tasks/`, runs the agent. So it costs no Hermes LLM tokens, it shows up in
`hermes cron list`, and a failed run is reported like any cron failure. `when` takes Hermes' schedule
forms: `every day at 8am`, `weekdays at 7am`, `0 7 * * 1-5`, `every 6h`, `in 2h` (once). Times are
read in Hermes' timezone (`timezone` in `config.yaml`; empty means the machine's, often UTC).

## Requirements

- Hermes Agent ≥ 0.21.6 running its gateway with the Telegram platform.
- For agy: the [Antigravity CLI](https://antigravity.google/) installed for the user running the
  gateway, and logged in once (run `agy` interactively on the machine).
- For Claude Code: the [Claude Code CLI](https://code.claude.com/) installed for that user and logged
  in to a Claude subscription once (`claude`, then `/login`).

## Install

```bash
hermes plugins install emilienrk/hermes-agy-chat --enable
hermes config set plugins.entries.agy-chat.settings.chat_id <your Telegram chat id>
hermes config set plugins.entries.agy-chat.settings.user_name <your name>
hermes config set plugins.entries.agy-chat.settings.backends '[agy, claude]'   # optional: Claude Code too
```

`chat_id` is your private chat with the bot, whose id is your Telegram user id: the one already in
Hermes' `TELEGRAM_ALLOWED_USERS`, or ask [@userinfobot](https://t.me/userinfobot).

Then:

1. For agy, add its workspace (`~/agy-chat` by default) to `trustedWorkspaces` in
   `~/.gemini/antigravity-cli/settings.json`, so agy loads the generated rules and MCP servers.
2. Optionally, put the commands in Telegram's `/` menu, in `config.yaml` (add `cc`, `cc_on`… for
   Claude Code):
   ```yaml
   platforms:
     telegram:
       extra:
         command_menu:
           priority: [agy, agy_on, agy_off, agy_new, agy_stop, agy_model, agy_effort, agy_verbose,
                      agy_voice, agy_conv, agy_retry, agy_btw, agy_handoff, agy_back, agy_task,
                      agy_usage, agy_doctor]
   ```
3. Restart the gateway, then send `/agy_doctor` (or `/cc_doctor`) in Telegram.

Update with `hermes plugins update agy-chat`, then restart the gateway.

## Settings

Under `plugins.entries.agy-chat.settings` in `config.yaml` (also editable in the Desktop app):

| Key | Default | |
|---|---|---|
| `chat_id` | — | **Required.** The only Telegram chat routed to the agents. Nothing is routed while it's empty. |
| `user_name` | `the user` | How the agents' context refers to you. |
| `backends` | `[agy]` | Agents reachable from Telegram: `agy`, `claude`. |
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
| **Display** | | |
| `react_working`, `react_queued` | `👀`, `✍` | Reactions on your messages while an agent works / while they wait (`""` for none). |
| `footer` | `true` | `— 🛸 agy · 2 min` under answers. |
| `verbose` | `false` | Live status of the agent's actions by default. |
| **Voice** | | |
| `voice_input` | `direct` | `direct`: the audio goes to agy (Google); `local`: Hermes' Whisper transcribes it first. Claude Code always gets `local`. |
| `voice_reply` | `off` | `off`, `mirror` (voice answer when you sent a voice note), `always`. |
| `tts_provider` | `""` | `""`: Hermes' TTS settings; `edge`: Edge TTS directly (free, no key; needs `edge-tts` ≥ 7.2.8 in Hermes' venv, older ones get a 403). |
| `tts_voice` | Hermes' `tts.edge.voice` | Edge voice, e.g. `fr-FR-DeniseNeural`. |
| **What the agents get from Hermes** | | |
| `share_soul`, `share_memory`, `share_thread` | `true` | Personality, memory, recent Telegram thread, in the agents' context. |
| `share_skills`, `share_mcp_servers` | `true` | Hermes' skills and MCP servers. |
| `bridge_tools` | all | Tools of the bundled bridge: `memory`, `session_search`, `skill_manage`, `cronjob_manage`, `text_to_speech`, `telegram_send`, `hermes_thread`, `tasks` (`agy_task` / `cc_task`). `[]` turns it off. |

`/agy_verbose`, `/agy_voice` (and `/cc_…`) override the display and voice defaults from Telegram, per agent.

The agents' context is written in English; they answer in your language.

## Security

The agents run as the gateway's user without permission prompts (agy with
`--dangerously-skip-permissions`, Claude Code with `--permission-mode bypassPermissions`): **whoever
can write in the configured chat can run anything that user can**. Set `chat_id` to your own DM with
the bot, and think twice before running the gateway as a user with `sudo`. API keys from the
gateway's environment are not passed to the agents, but their MCP configs hold the bearer tokens of
Hermes' MCP servers (written with mode 600).

## Data

Plugin state (mode, model, conversation, tasks) and the turn logs live in
`<HERMES_HOME>/plugin-data/agy-chat/` (`state.json` and `turns.log` for agy, `claude-state.json` and
`claude-turns.log` for Claude Code), outside the install directory, so updates keep them.

## Development

The tests drive whole turns against a fake agy (`tests/fake_agy.py`), a fake Claude Code
(`tests/fake_claude.py`) and a fake Telegram adapter.
They import Hermes' gateway modules, so run them with Hermes' interpreter:

```bash
~/.hermes/hermes-agent/venv/bin/python tests/test_chat.py
hermes plugins validate .
```

## License

MIT
