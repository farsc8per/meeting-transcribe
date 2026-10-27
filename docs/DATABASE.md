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
