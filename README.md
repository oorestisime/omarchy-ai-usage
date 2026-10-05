# AI Usage for Omarchy

An unofficial Omarchy bar widget for account-wide ChatGPT activity, Codex rate
limits, and OpenCode Go quotas.

## Features

- Discovers the current Codex login, Omarchy's Codex account registry, and
  additional isolated Codex homes.
- Keeps subscription limits and lifetime totals separate by ChatGPT account.
- Supports any number of accounts with consistent, theme-derived colors.
- Shows stacked totals for the latest seven days and seven seven-day periods.
- Optionally shows OpenCode Go rolling, weekly, and monthly limits.
- Uses the active Omarchy theme and refreshes every 15 minutes by default.

## Install

```bash
omarchy plugin add https://github.com/oorestisime/omarchy-ai-usage --enable
```

The plugin requires Python 3 and the Codex CLI. Your primary ChatGPT account
should already be signed in through Codex. OpenCode Go support is optional and
is enabled only when its existing login is found.

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

OpenCode Go limits come from its usage service using the existing credential in
`~/.local/share/opencode/auth.json`; its credential layout and endpoint may also
change independently.

## Privacy

- Does not read prompts, responses, transcripts, or OpenCode session databases.
- Invokes Codex app-server separately for each configured Codex home.
- Never includes raw account IDs or credentials in collector output.
- Sends the OpenCode Go credential only to OpenCode's own usage endpoint.

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
