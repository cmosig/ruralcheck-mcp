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
| Local area | municipality, nearest village and town, public transport, shops and services, lakes and rivers to swim in, phone signal and internet, drive times |
| Land | land-register plot, paths across it and whether a road reaches it, slope and aspect, what the plot can see |
| Hazards | river flooding, wildfire history, earthquake shaking, protected areas |
| Living there | modelled road and rail noise, sun hours with the hills taken into account, solar yield, wind, night sky, snow, air quality, climate now and around 2050, woods |
| Region | farmland prices, household income and population, visitor numbers |

`list_checks` returns the current list with a description, price and time for each.

Everything is computed from public data. It is an estimate. It is not a survey, a
valuation or legal advice. It covers the parts of Europe where the ground is
measured finely enough, and `check_coverage` says whether a point is one of them.

## Tools

Every tool needs an API key from a paid RuralCheck plan. Keys are made on your
account page. Without one a client can still connect and list the tools.

| Tool | Spends credit | What it does |
|---|---|---|
| `find_place` | no | A village or town name to coordinates. Street addresses are not searchable. |
| `check_coverage` | no | Whether a point can be analysed, and how finely its ground is measured. |
| `list_checks` | no | Every check, with its price as a share of one credit. |
| `quote_cost` | no | What a set of checks would cost and how long it would take, without running it. |
| `analyse_plot` | depends on the checks | Runs the checks you name for one point and returns the results. |
| `get_report` | no | Fetches a report that took longer than one tool call waits. |
| `sample_report` | no | A real full report for a plot in the Black Forest, to see what each check returns. |

Answers are JSON, returned as text: the figures, their caveats, and the
attribution for the data behind them. Map outlines and pictures are left out.

## Prices

Most checks spend no credit. Terrain, noise, sun, solar, view, wind and access use
the 5 m ground model and spend a share of a credit: noise alone is 0.15, and every
check together is 1.0. If any check you asked for fails, the whole charge is
refunded. `quote_cost` gives the price before you spend anything.

## Use it

### Hosted

Add `https://ruralcheck.eu/mcp` as a remote MCP server and send your key as
`Authorization: Bearer <key>`. In Claude Code:

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

`ruralcheck-mcp --http --port 8765` serves streamable HTTP instead of stdio. Over
HTTP each caller sends their own key in the `Authorization` header, and the server
keeps none.

| Variable | Default | Meaning |
|---|---|---|
| `RURALCHECK_API_KEY` | none | Your key, for stdio. Required. |
| `RURALCHECK_BASE_URL` | `https://ruralcheck.eu` | Where the API is. |
| `RURALCHECK_WAIT_S` | `45` | How long one tool call waits for a report before handing back an id. |

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
API changes. It spends 0.15 of a credit.

The contract lives under `/api/v1/` on ruralcheck.eu: `checks`, `cost`,
`coverage`, `search`, `sample`, and `reports` with a long-polling `?wait=` and
`?drawing=false`.

## Licence

The code in this repository is MIT licensed. The reports it fetches are covered by
the RuralCheck terms and by the licences listed in each report's `attribution`.
