# RuralCheck MCP server

<!-- mcp-name: eu.ruralcheck/ruralcheck -->

This server lets an AI agent analyse a plot of rural land in Europe with
[RuralCheck](https://ruralcheck.eu). Ask Claude, ChatGPT or any other MCP client
whether a field floods, how loud the road is, how many hours of sun the house gets
in January, or which land-register plot it is. The agent calls RuralCheck and gets
back computed figures with their sources.

> **Status: preview.** The API behind this server is not public yet. The tools, the
> contract and the tests are real. The hosted endpoint and the API keys arrive when
> ruralcheck.eu launches.

## What it can answer

A report is a set of checks. You ask only for the ones you need, and each comes
back with its figure, its caveats and where it came from.

| Group | Checks |
|---|---|
| Local area | municipality, nearest village and town, public transport, shops and services, bodies of water (lakes, rivers, the sea, beaches and public pools a road or path reaches), phone signal and internet, drive times |
| Land | land-register plot, paths across it and whether a road reaches it, terrain (slope and height), what the plot can see |
| Hazards | river flooding, wildfire history, earthquake shaking, protected areas |
| Living there | modelled road and rail noise, sun hours with the hills taken into account, solar yield, wind, night sky, snow, air quality, climate now and around 2050, woods |
| Region | household income and population, visitor numbers, the rules a buyer meets in the region (Spain and France so far) |

`list_checks` returns the current list with a description, price and time for each.

Everything is computed from public data. It is an estimate. It is not a survey, a
valuation or legal advice. It covers the parts of Europe where the ground is
measured finely enough, and `check_coverage` says whether a point is one of them.

## Tools

Every tool needs a paid RuralCheck plan, which includes every check. An account can
start one new report a minute through `analyse_plot`; fetching one with `get_report`
does not count, and more than one a minute is the Business plan. Without a plan a
client can still connect and list the tools.

Nothing here deletes a plot for good. A deleted plot goes to the Trash on the
account, and is removed 30 days later or when the reader empties the Trash on the
website.

| Tool | What it does |
|---|---|
| `find_place` | A village or town name to coordinates. Street addresses are not searchable. |
| `check_coverage` | Whether a point can be analysed, and how finely its ground is measured. |
| `list_checks` | Every check, with what it covers and how long it takes. |
| `analyse_plot` | Runs the checks you name for one point and returns the results. |
| `get_report` | Fetches a report that took longer than one tool call waits. |
| `sample_report` | A real full report for a plot in the Black Forest, to see what each check returns. |
| `upload_listings` | Professional plan. Sends an agent's listings: each becomes a saved plot with a full report and a client link, run one after another. |
| `get_listing_upload` | An upload's progress: each listing's status, its client link, or why it was not run. |
| `list_listing_uploads` | The account's uploads and how far each has got. |
| `list_plots` | The plots the account keeps, with the key figure of each check. Or the Trash. |
| `get_plot` | One kept plot with its report, as it was when it was kept. |
| `save_plot` | Keeps a finished report in My plots. Only a report that ran every check. |
| `update_plot` | Renames a kept plot or replaces its note. |
| `file_plot` | Puts a kept plot in a folder, or takes it out of one. |
| `delete_plot` | Moves a kept plot to the Trash. It can be restored for 30 days. |
| `restore_plot` | Takes a plot out of the Trash. |
| `list_folders` | The account's folders. |
| `create_folder`, `rename_folder` | Makes or renames a folder. |
| `delete_folder` | Deletes a folder. Its plots stay, in no folder. |

Answers are JSON, returned as text: the figures, their caveats, and the
attribution for the data behind them. Map outlines and pictures are left out.

## Use it

### Hosted

The server is at `https://ruralcheck.eu/mcp`. It signs you in with OAuth, so Claude
and ChatGPT need nothing but the address:

- **Claude**: Customize, Connectors, **+**, Add custom connector, enter the address,
  then Connect and allow it on the RuralCheck page.
- **ChatGPT**: turn on Developer mode under Settings, Security and login. Then open
  Plugins, select **+**, enter the address, and allow it on the RuralCheck page.

Clients that take a header can use an API key from your account page instead. In
Claude Code:

```bash
claude mcp add --transport http ruralcheck https://ruralcheck.eu/mcp \
  --header "Authorization: Bearer $RURALCHECK_API_KEY"
```

### On your own machine

Python 3.10 or newer. With [uv](https://docs.astral.sh/uv/):

```bash
uvx --from git+https://github.com/cmosig/ruralcheck-mcp ruralcheck-mcp
```

Claude Desktop, Cursor and most other clients take a block like this:

```json
{
  "mcpServers": {
    "ruralcheck": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/cmosig/ruralcheck-mcp", "ruralcheck-mcp"],
      "env": { "RURALCHECK_API_KEY": "rc_..." }
    }
  }
}
```

`ruralcheck-mcp --http --port 8765` serves streamable HTTP instead of stdio, with
the OAuth sign-in in front of it. A request without a token gets a 401 that points
at `/.well-known/oauth-protected-resource/mcp`, which names ruralcheck.eu as the
authorization server. The token a client comes back with is an ordinary API key.
The server keeps none: it checks each token with the API and passes it on.

| Variable | Default | Meaning |
|---|---|---|
| `RURALCHECK_API_KEY` | none | Your key, for stdio. Required. |
| `RURALCHECK_BASE_URL` | `https://ruralcheck.eu` | Where the API is. |
| `RURALCHECK_WAIT_S` | `45` | How long one tool call waits for a report before handing back an id. |
| `RURALCHECK_PUBLIC_URL` | `https://ruralcheck.eu` | Where the hosted server is reached, for its OAuth metadata. |
| `RURALCHECK_AUTH_URL` | the public URL | The OAuth authorization server. |

## Attribution

The data behind each answer is published under licences that require credit. Every
report carries an `attribution` list. Show it to whoever reads the results.

## Develop

```bash
python -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m pytest
```

`tests/test_server.py` runs against a fake API and needs no network.
`tests/test_live.py` drives every tool against a real instance and is skipped
unless `RURALCHECK_LIVE_URL` and `RURALCHECK_API_KEY` are set. Run it whenever the
API changes.

The contract lives under `/api/v1/` on ruralcheck.eu: `checks`, `coverage`,
`search`, `sample`, `reports` with a long-polling `?wait=` and
`?drawing=false`, and `plots` and `folders` for My plots.

## Licence

The code in this repository is MIT licensed. The reports it fetches are covered by
the RuralCheck terms and by the licences listed in each report's `attribution`.
