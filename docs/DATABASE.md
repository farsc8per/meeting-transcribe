# mt's database

mt keeps people, customers and action items in one SQLite file. Its structure is fixed by
[`schema.sql`](../schema.sql) — that file is the contract.

## Where it is

`mt doctor` prints the path under **workspace & database**. By default it is
`~/mt-data/DB/mt.db`, created empty by `make.sh` (`mt init`). To use a different file, set
`"db"` in `config.json`.

## The rules

1. **mt never restructures a database it did not create.** `mt init` only builds a *new*
   file. `mt doctor` only reads, and lists any table or column your DB is missing.
2. **You can keep your own database** — set `"db"` to it — as long as it has every table and
   column in `schema.sql`. Extra tables and columns of your own are fine; mt ignores them.
3. **To receive updates, follow the schema.** New mt versions only ever *add* columns, and the
   CHANGELOG says when. If you rename or drop a column mt uses, you are running your own
   fork: updates will break and you own the fix.
4. **Back up before any import.** `sqlite3 your.db ".backup your-backup.db"` — not `cp` on a
   file something has open.

## Bringing your own data in

If you already track customers, contacts or action items somewhere (another SQLite DB, a
spreadsheet, a notes folder), you have two choices:

- **Import into mt's database (recommended).** Your data is copied into `mt.db`; your
  original is untouched.
- **Point mt at your database** and add mt's missing tables/columns to it. Only do this if
  other tools of yours already write that file and you want one source of truth.

Either way, hand your AI assistant the prompt below.

### Prompt for your assistant

> I use `mt` (meeting transcription). Its database contract is `schema.sql` in the mt folder
> (`<path to mt>`), explained in `docs/DATABASE.md`. I want to bring my existing data into it.
>
> My existing data is: `<path or description — e.g. ~/notes/crm.db, a Google Sheet export>`.
>
> 1. Read `schema.sql` and `docs/DATABASE.md`, and inspect my existing data. Do not change
>    anything yet.
> 2. Back up both databases (`sqlite3 <db> ".backup <db>.bak-<date>"`) before any write.
> 3. Propose a column-by-column mapping from my data into mt's tables (`customers`,
>    `contacts`, `projects`, `action_items` matter most). List anything that has no home,
>    and anything ambiguous, and ask me before guessing. Show me the plan and wait.
> 4. After I approve: import into mt's database (`mt doctor` shows its path) — or, if I chose
>    to keep my own DB, add only the missing tables/columns from `schema.sql` to it, never
>    renaming or dropping anything of mine.
> 5. Run `mt doctor` and show me the **workspace & database** section. It must say the
>    database matches the schema. Report row counts before and after.
>
> `customers.short_name` must match the folder names under `<workspace>/Customers/`, and
> people's names in `contacts.name` should be exactly how they appear in meetings — that is
> how mt connects a voice to a contact. To post weekly updates to Slack, set
> `customers.slack_internal` to the account's channel as `#channel (<channel id>)`.

## When an update adds to the database

Some releases add tracking features that need new columns or tables (the change log for
items, titles, owners linked to contacts, links). Every one of these upgrades only **adds**
— no rename, no drop — and each takes a backup first (`backups/` beside the database).

- **mt's own database** (made by `mt init`) is upgraded for you: by the updater, and again
  when `mt web` starts if anything is still missing. You do nothing.
- **Your own database** (`"db"` in `config.json`) is never changed without you asking.
  `mt doctor` lists what is missing; `mt migrate --check` shows the same. Apply it with
  `mt migrate --own-db`, or hand your assistant the prompt below.

### Prompt for your assistant (updates)

> I use `mt`, pointed at my own SQLite database. An mt update added tracking features that
> need new columns/tables. mt is at `<path to mt>`.
>
> 1. Run `<path to mt>/mt migrate --check` and show me the list. Change nothing yet.
> 2. If anything of mine already uses a name it wants to add, stop and tell me.
> 3. After I say go: `<path to mt>/mt migrate --own-db` (it backs the database up first),
>    then `<path to mt>/mt doctor`, and show me both outputs.
> 4. From then on, when you write to this database yourself, follow the conventions below.

### Conventions that came with these upgrades

- **Links** (`external_links`): one row per thing an item points at. `system` is one of
  `jira, confluence, monday, slack, gmail, gdrive, gcal, file, web`; `role` is one of
  `tracks` (the ticket/epic/request that *is* this work), `customer_share`, `source`,
  `discussion`, `deliverable`, `related`, `reference`. Put the full URL in `url` when you
  have it. Exactly one link per item has `is_primary = 1` — the one the list opens. The
  easiest way to get all of that right is `tracking.add_link(...)` rather than a raw INSERT.
- **Dates on an item**: `due_date` is a real deadline; `follow_up_date` is when you next touch
  it (roll it forward each time). `start_date` / `end_date` are the plan.
- **History**: every change to an item is logged in `item_events` automatically. When your
  assistant edits a row, set `updated_by` (its name) and `updated_at` in the same UPDATE so
  the log says who did it.
