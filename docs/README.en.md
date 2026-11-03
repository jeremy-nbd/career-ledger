# Career Ledger

**Remember what you did. Know what you can substantiate.**

A local-first career evidence app with an optional tool-using agent. Capture work notes, review experience cards alongside their original sources, map confirmed experience to job requirements, and export Markdown with an evidence index.

It also includes a **daily journal and weekly reports**: record completed work, blockers and next steps by date, then generate a report for a custom start/end date range (both dates inclusive) with links to each original entry. Local mode groups exact text and deduplicates identical items; Agent mode can synthesize themes with validated citations. Historical plans and blockers still need review. Reports preserve source snapshots and export as Markdown. A last-seven-days shortcut pre-fills editable dates; single-day, cross-week, month and year ranges are supported. Backdated entries and same-day additions are supported; missing days are never invented.

## Run

Requires Node.js 22+. No runtime packages, database installation, or build step.

```bash
git clone https://github.com/jeremy-nbd/career-ledger.git
cd career-ledger
npm start
```

Open http://127.0.0.1:4317. The interface is currently in Chinese. Load the fictional demo into an empty ledger to explore three reviewed cards, an unresolved contribution claim, and two sample job descriptions.

## Why this exists

A note saying “I participated in evaluation design; teammates implemented the model integration” should retain that scope. It must not silently become “I independently built the entire system.”

Generated cards start as drafts. Users review ownership, outcomes, and exact source quotations before confirming. Confirmation is a self-review, not independent verification. Citation existence is enforced in code; semantic support still requires human review.

## Modes

- **Local rules:** preserves source excerpts and identifies keyword overlap. No model calls; no claims of semantic matching or qualification scoring.
- **Agent:** uses `read_source` and `search_cards`, then proposes a card or a job-to-evidence mapping through validated tools. It cannot confirm cards or merge records. Invalid citations are returned for correction. Runs are bounded to seven requests, each with a 45-second timeout.

To enable the agent, copy `.env.example` to `.env` and set `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL`. Your endpoint must support Chat Completions function calling, `tool_choice: required`, and `max_tokens`. Restart and select Agent mode in the header; the browser remembers your choice. Relevant notes, cards and job descriptions are sent to that provider. The header distinguishes configured model and active mode from local data storage. Mocked protocol tests cover integration behavior. On 2026-11-02, a fictional experience-draft task succeeded against `qwen3.5-plus`, including source reading and citation validation; live job matching and weekly synthesis remain unverified for that model.

## Persistence and limits

Data lives in ignored `data/ledger.json`. Set `CAREER_DATA_DIR` to use another directory. Export a full backup from the header. Stop the server before restoring a JSON backup as `ledger.json`; keep a copy of the previous file. Use only one process per data directory.

Job runs store immutable evidence snapshots. Markdown exports use approved wording deterministically. The MVP exports experience sections, not a complete typeset resume. It supports neither job-board scraping nor automated applications.

The server binds to loopback and checks Host and Origin. It has no authentication or multi-user isolation. Do not expose it directly to the public internet. Local data is not encrypted.

Entries, experience cards and reports can be moved to Trash and restored. Deleting a source also trashes active cards citing it; restoring that source attempts to restore those cards. Cards with other missing sources stay in Trash until their evidence is restored. Deleting a card alone preserves its source. Existing report snapshots remain unchanged, while new generation excludes deleted inputs. Backups include trashed records; this is reversible deletion, not permanent erasure.

## Development

```bash
npm run dev
npm run check
npm test
```

Tests exercise evidence validation, review gates, revision conflicts, snapshots, concurrent persistence, tool loops and API failures. Model responses are mocked. HTTP tests require permission to bind ephemeral loopback ports.

[Architecture](architecture.md) · [Contributing](../CONTRIBUTING.md) · [MIT License](../LICENSE)
