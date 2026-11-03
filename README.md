# AI Usage for Omarchy

An unofficial Omarchy bar widget for account-wide ChatGPT activity, Codex rate
limits, Claude subscription limits, and OpenCode Go quotas.

## Features

- Discovers the current Codex login, Omarchy's Codex account registry, and
  additional isolated Codex homes.
- Shows ChatGPT, Claude, and OpenCode Go limits in one consistent source grid.
- Supports any number of accounts with consistent, theme-derived colors.
- Shows combined ChatGPT, Claude, and OpenCode Go totals for the latest seven
  days and seven seven-day periods.
- Optionally shows Claude five-hour and weekly subscription limits, plus OpenCode
  Go rolling and weekly limits.
- Uses the active Omarchy theme and refreshes every 15 minutes by default.

## Install

```bash
omarchy plugin add https://github.com/oorestisime/omarchy-ai-usage --enable
```

The plugin requires Python 3 and the Codex CLI. Your primary ChatGPT account
should already be signed in through Codex. Claude and OpenCode Go support are
optional and are enabled only when their existing logins are found.

Click the robot icon to open the panel. Middle-click the icon, press `R` in the
panel, or use the refresh button to update immediately.

## Additional ChatGPT accounts

The existing `~/.codex` login is discovered automatically. To monitor another
ChatGPT account without changing it, sign in to an isolated Codex home:

```bash
~/.config/omarchy/plugins/oorestisime.ai-usage/scripts/add-account opencode
```

Use the ChatGPT account associated with your other client. The daily buckets
are account-wide, so compatible clients do not need to share local session
databases.

For custom Codex homes, create `~/.config/ai-usage/accounts.json`:

```json
{
  "accounts": [
    { "label": "Work", "home": "~/.codex-work" }
  ]
}
```

The previous `chatgpt-usage` config and data locations remain discoverable for
backward compatibility.

## Data sources and caveats

ChatGPT usage and limits come from Codex app-server's `account/usage/read` and
`account/rateLimits/read` RPC methods. Their response schema is not part of the
public OpenAI API contract and may change with Codex releases.

Daily history is whatever the account service currently returns. The newest
bucket can lag behind the current day, and different accounts can expose
different history lengths. The seven-week view is calculated locally from those
daily buckets.

Claude limits come from the usage service used by Claude Code, using its
existing local OAuth login. The last normalized Claude result is cached for up
to 24 hours so a temporary usage-service rate limit does not blank the panel.
OpenCode Go limits come from OpenCode v2's local usage API, which keeps
credential handling inside OpenCode. A legacy OpenCode Go API key is supported
as a fallback.

Claude's token history is calculated from usage metadata in local Claude Code
history and deduplicated by response ID. OpenCode Go history is aggregated from
indexed metadata in OpenCode's local database and includes only models routed
through the `opencode` provider. These sources describe activity on this
machine; unlike ChatGPT's service buckets, they are not account-wide.

## Privacy

- Never returns or stores prompts, responses, or transcript content. Claude
  history files are scanned locally for timestamps, response IDs, and token
  counts only.
- Queries only timestamps, provider IDs, and token counts from OpenCode's local
  database; message content is never selected.
- Invokes Codex app-server separately for each configured Codex home.
- Never includes raw account IDs or credentials in collector output.
- Sends Claude's OAuth credential only to Anthropic's own usage endpoint.
- Leaves OpenCode v2 credential handling to the OpenCode CLI; the legacy Go
  credential fallback is sent only to OpenCode's own usage endpoint.

## Development

```bash
python3 scripts/collect.py --pretty
python3 -m unittest discover -s tests -v
bash -n scripts/add-account
omarchy plugin validate .
qmllint -I /usr/share/omarchy/shell BarWidget.qml Panel.qml
```

## License

[MIT](LICENSE)
