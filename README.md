# Life Dashboard for Markdown / Obsidian

A small, local-first dashboard that turns a few canonical Markdown files into one read-only view:

- **Now** — one current task, up to two next tasks, and waiting items;
- **Life wheel** — the latest scored snapshot plus domain notes;
- **Month** — daily logs with sleep, body, work, anchors, and narrative titles;
- **Project pool** — active projects sorted by your own soft due dates.

The Markdown stays authoritative. The dashboard is disposable and rebuilt on every request. It does not write back to your vault.

![Life Dashboard icon](static/pwa/app-icon-192.png)

## Quick start

```bash
git clone https://github.com/shuyurrrr/life-dashboard-markdown.git
cd life-dashboard-markdown
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Try the included, fully fictional sample vault
life-dashboard --vault-root sample-vault --month 2026-11 --output output/demo.html
open output/demo.html
```

Or run the live read-only server:

```bash
life-dashboard-serve --vault-root sample-vault --month 2026-11
# open http://127.0.0.1:8899
```

The server binds to `127.0.0.1` by default. Keep it loopback-only unless you know how you are authenticating the reverse proxy. A token-protected `/dashboard/` route is included for private remote access; place a random 32+ character token at `~/.config/life-dashboard/token` or pass `--work-token-file`.

## Use with your own Obsidian vault

Copy the files from [`templates/`](templates/) into any folder in your vault, then point `--vault-root` at that folder. The expected layout is:

```text
<root>/
  today.md                         # optional live replacement for today's archive
  任务台.md                         # shared priority surface
  人生轮.md                         # latest life-wheel review
  工作/case-ledger.md               # optional project / case pool
  daily-log/YYYY-MM/YYYY-MM-DD.md  # one archived day per file
```

The filenames are Chinese because they match the original workflow; headings and field names are the actual parser contract. You may keep the files inside Obsidian, but Obsidian itself is not required—plain Markdown works.

## Core contracts

### Priority ledger

- `NOW`: at most **1** open task.
- `NEXT`: at most **2** open tasks.
- `WAITING` and `BACKLOG`: unbounded.
- Completed tasks disappear from the live surface instead of becoming overdue ghosts.

### Time semantics

- `official_due`: an external deadline.
- `soft_due`: your own operational target and the primary sort key.
- `daily-log`: facts that happened, not a second task manager.

### Privacy

The included sample data is fictional. The program reads only the paths you provide and does not upload vault content. The PWA service worker is network-only and deliberately does not cache private dashboard pages.

## Templates and sample data

- [`templates/`](templates/) — blank-ish Obsidian/Markdown templates.
- [`sample-vault/`](sample-vault/) — fictional data that renders immediately.

## Development

```bash
pip install -e '.[dev]'
pytest -q
```

## License

MIT
