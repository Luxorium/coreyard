# Security Policy

## Reporting a vulnerability

Please report security issues privately via GitHub's **Report a vulnerability** button under the
Security tab, rather than opening a public issue.

Include what you found, how to reproduce it, and what an attacker could do with it. You'll get an
acknowledgement within a few days.

## Scope

CoreYard runs on an operator's own machine and talks to three places: a yard management system's
SQL Server over SMB, an SMB file share, and the Shopify Admin API. Reports that matter most:

- Anything that could turn a read-only database path into a write.
- Credential exposure — in logs, process arguments, temp files, or committed files.
- SQL injection through the site-supplied `schema.json` mapping or values read from the database.
- Anything that could publish data to a storefront that was not meant to be public.

## Design notes

- **Read-only by construction.** Every database path issues `SELECT` only.
- **Secrets live in `.env`**, gitignored, never in source. The SMB password is written to a
  `0600` temporary auth file for `smbclient` rather than passed on the command line, where it
  would be visible in the process list.
- **No vendor schema is committed.** Table and column names come from a local `schema.json`,
  also gitignored.
- **Shopify scopes** should be the minimum the sinks need: `read_products, write_products,
  read_inventory, write_inventory, read_locations, read_files, write_files`.

## If you find a leaked credential in the history

Rotate it first, then report. Rotation is always the fastest fix; scrubbing history is secondary.
