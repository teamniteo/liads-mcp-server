# liads-mcp-server

An MCP server for LinkedIn Ads (Campaign Manager). It lets an AI agent read results and build, run and pause small LinkedIn campaigns, with safety rules in the code instead of in the prompt.

**Status:** early. The 38 tests run against a mocked LinkedIn API; the payloads follow LinkedIn's Marketing API docs (version `202610`) but haven't been run against a live ad account yet. Try it first in an ad account without a payment method, where nothing can spend.

## How writes work

Every write tool (`draft_*`, `pause_entity`, `enable_entity`) only stores a plan and returns a preview. Nothing changes in LinkedIn until `confirm_and_apply`, which you put on your MCP client's ask list. `confirm_and_apply(dry_run=true)` re-checks every rule and the ad account without writing; LinkedIn has no validate-only mode, so this is the closest thing to one.

Whatever the caller asks, the server:

- creates campaign groups, campaigns and ads as `DRAFT`; switching on is only `enable_entity`, its own plan and its own approval, and the campaign is checked again just before;
- keeps audience expansion and the LinkedIn Audience Network off, bids manual CPC, and requires a daily budget, a lifetime budget and an end date;
- refuses budgets and bids over the caps in your config; a rise in budget or bid is its own plan;
- refuses ad accounts that aren't in `ads.allowed_accounts`;
- builds image ads on hidden posts (`feedDistribution: NONE`), so nothing appears on your company page feed;
- reads images and conversion CSVs only from `uploads.allowed_dir`;
- hashes emails (SHA-256) before a conversion upload leaves your machine;
- never retries a write, so a timeout can't create a duplicate;
- talks only to `api.linkedin.com` and `www.linkedin.com`, with no telemetry;
- writes every applied step to a JSONL audit log.

## Tools

| Kind | Tools |
|---|---|
| Read | `health_check`, `list_ad_accounts`, `get_ad_account`, `list_campaign_groups`, `list_campaigns`, `list_creatives`, `get_targeting_facets`, `search_targeting`, `audience_count`, `get_analytics`, `list_conversions`, `list_plans` |
| Draft (stores a plan) | `draft_campaign` (in an existing or a new campaign group), `draft_campaign_update`, `draft_image_ad`, `draft_thought_leader_ad`, `draft_conversion`, `draft_conversion_upload`, `pause_entity`, `enable_entity` |
| Apply | `confirm_and_apply(plan_ids, dry_run=true)` |

`get_analytics` reports by campaign or ad, and by who clicked: job title, job function, seniority, company size, industry, company, country and region. Conversions there are LinkedIn's count, which includes view-through.

## Setup

1. **LinkedIn developer app** at https://www.linkedin.com/developers/apps, linked to your company page (a page admin confirms it).
   - Under Products, add **Advertising API** (the Development tier is enough: it reads any number of ad accounts and edits up to five) and **Conversions API**.
   - Under Auth, add the redirect URL `http://localhost:8787/callback`.
2. **Roles:** the LinkedIn user who signs in needs an editing role on each ad account (Account Manager or Campaign Manager).
3. **Install** (Python 3.11+): `pip install git+https://github.com/teamniteo/liads-mcp-server`, or run it with `uvx --from git+https://github.com/teamniteo/liads-mcp-server liads-mcp-server`.
4. **Config** in a folder outside git, named by `LIADS_CONFIG`:

   ```yaml
   linkedin:
     client_path: /path/to/liads/client.json   # {"client_id": "...", "client_secret": "..."}
     token_path: /path/to/liads/token.json     # written by `liads-mcp-server login`, mode 0600
     api_version: "202610"                     # LinkedIn-Version; bump once a year
     redirect_port: 8787
   ads:
     allowed_accounts: [512345678]             # ad account IDs this server may change
   safety:
     max_daily_budget: 20                      # in the ad account's currency
     max_lifetime_budget: 1500
     max_bid: 15
     log_file: /path/to/liads/audit.log
   uploads:
     allowed_dir: /path/to/images-and-csvs
   ```

5. **Sign in:** `LIADS_CONFIG=/path/to/config.yaml liads-mcp-server login`. It opens LinkedIn's consent page and stores the tokens. Scopes: `r_ads rw_ads r_ads_reporting rw_conversions w_organization_social` (the last one is for the hidden posts behind image ads).
6. **Connect** it as a stdio MCP server running `liads-mcp-server` with `LIADS_CONFIG` set. For Claude Code:

   ```json
   {
     "mcpServers": {
       "linkedin-ads": {
         "command": "liads-mcp-server",
         "env": { "LIADS_CONFIG": "/path/to/liads/config.yaml" }
       }
     }
   }
   ```

   Allow the read and draft tools, and keep `confirm_and_apply` on the ask list.

`health_check` shows when the access and refresh tokens expire and how long the pinned API version is still supported. LinkedIn retires each version about a year after release: bump `api_version` before then.

## Conversion upload CSV

Columns: `email`, `li_fat_id` (LinkedIn's click ID, if you stored it), `happened_at` (`YYYY-MM-DD` or an ISO time), `value` (optional) and `event_id` (optional, for de-duplication). Rows without an email or click ID, and rows older than 90 days, are skipped and counted in the preview.

## Development

```sh
uv run --extra test pytest
```

The tests mock LinkedIn's API with respx: rules on every write, plan references, no retries on writes, the upload host check, email hashing, and Rest.li query encoding.

## License

MIT
