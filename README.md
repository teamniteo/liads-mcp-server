# linkedin-ads-mcp

An MCP server for LinkedIn Ads (Campaign Manager) that an agent can use to build, run and read small, capped campaigns.

Writes work like AdLoop does for Google Ads: every `draft_*` tool stores a plan and returns a preview; nothing changes in LinkedIn until `confirm_and_apply`, which you put on your client's ask list. Rules the server enforces in code, whatever the caller asks:

- New campaign groups, campaigns and ads are created as `DRAFT`. Going live is only `enable_entity`, its own plan and its own approval, and the campaign is checked again just before.
- Campaigns always have audience expansion and the LinkedIn Audience Network off, manual CPC bidding, a daily and a lifetime budget, and an end date.
- Budgets and bids stay under the caps in your config; a rise in budget or bid is its own plan.
- Ad accounts outside `ads.allowed_accounts` are refused.
- Image ads use hidden posts (`feedDistribution: NONE`); nothing is posted to your company page feed.
- Images and conversion CSVs are read only from `uploads.allowed_dir`.
- Emails in conversion uploads are SHA-256 hashed before they leave your machine.
- Writes are never retried, so a timeout can't create a duplicate.
- The only hosts contacted are `api.linkedin.com` and `www.linkedin.com`. No telemetry.
- Every applied step goes to a JSONL audit log.

## Tools

| Kind | Tools |
|---|---|
| Read | `health_check`, `list_ad_accounts`, `get_ad_account`, `list_campaign_groups`, `list_campaigns`, `list_creatives`, `get_targeting_facets`, `search_targeting`, `audience_count`, `get_analytics` (incl. job title, seniority, company size, industry and country pivots), `list_conversions`, `list_plans` |
| Draft (stores a plan) | `draft_campaign` (in an existing or a new campaign group), `draft_campaign_update`, `draft_image_ad`, `draft_thought_leader_ad`, `draft_conversion`, `draft_conversion_upload`, `pause_entity`, `enable_entity` |
| Apply | `confirm_and_apply(plan_ids, dry_run=true)` |

LinkedIn has no validate-only mode, so `dry_run=true` re-runs every rule above and checks the ad account without writing.

## Setup

1. **LinkedIn developer app** at https://www.linkedin.com/developers/apps, linked to your company page (a page admin confirms it). Under Products, add **Advertising API** (Development tier is enough: read any number of ad accounts, edit up to five) and **Conversions API**. Under Auth, add the redirect URL `http://localhost:8787/callback`.
2. **Roles:** the LinkedIn user who signs in needs a role that can edit on each ad account (Account Manager or Campaign Manager).
3. **Config** (keep it out of git):

   ```yaml
   linkedin:
     client_path: /path/to/.linkedin-ads/client.json   # {"client_id": "...", "client_secret": "..."}
     token_path: /path/to/.linkedin-ads/token.json     # written by `linkedin-ads-mcp login`, mode 0600
     api_version: "202610"                             # LinkedIn-Version; bump once a year
     redirect_port: 8787
   ads:
     allowed_accounts: [512345678]                     # ad account IDs this server may change
   safety:
     max_daily_budget: 20                              # in the ad account's currency
     max_lifetime_budget: 1500
     max_bid: 15
     log_file: /path/to/.linkedin-ads/audit.log
   uploads:
     allowed_dir: /path/to/images-and-csvs
   ```

4. **Sign in:** `LINKEDIN_ADS_CONFIG=/path/to/config.yaml linkedin-ads-mcp login`. It opens LinkedIn's consent page and stores the tokens. Scopes: `r_ads rw_ads r_ads_reporting rw_conversions w_organization_social` (the last one is for the hidden posts behind image ads).
5. **Run** as a stdio MCP server: `linkedin-ads-mcp`, with `LINKEDIN_ADS_CONFIG` set. In Claude Code, allow the read and draft tools and put `confirm_and_apply` on the ask list.

`health_check` shows when the access and refresh tokens expire and how long the pinned API version is still supported.

## Conversion CSV

Columns: `email`, `li_fat_id` (LinkedIn's click ID, when you stored it), `happened_at` (`YYYY-MM-DD` or ISO time), `value` (optional), `event_id` (optional, for de-duplication). Rows without an email or click ID, and rows older than 90 days, are skipped and counted in the preview.

## Development

```sh
uv run --extra test pytest
```

Tests mock LinkedIn's API with respx. Payloads follow LinkedIn's Marketing API docs (version 202610); check them against a test ad account without a payment method before relying on them: campaigns, image ads and conversion uploads there can't spend.

## License

MIT
