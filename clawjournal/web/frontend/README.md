# ClawJournal Workbench Frontend

This is the React + TypeScript + Vite app for the ClawJournal browser
workbench. The Python daemon serves the built files from `dist/` at
`http://localhost:8384`.

Most users do not need this directory. Use it when you are changing the UI,
rebuilding the bundled workbench, or debugging frontend/API behavior.

## Quick Start

From the repository root:

```bash
cd clawjournal/web/frontend
npm install
npm run build
cd ../../..
clawjournal serve
```

Open `http://localhost:8384`. If `clawjournal serve` shows a placeholder page,
the frontend has not been built yet or `dist/` is stale.

## Choose A Workflow

### Build the workbench users see

Use this path before testing the real installed experience:

```bash
cd clawjournal/web/frontend
npm install
npm run build
clawjournal serve
```

`npm run build` writes `dist/`. The daemon in
`clawjournal/workbench/daemon.py` serves that folder and injects the local API
token into `index.html`, so browser requests to `/api/*` work without manual
setup.

### Iterate on UI code

Use Vite when you want fast reloads while editing React components:

```bash
clawjournal serve --no-browser
cd clawjournal/web/frontend
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/api` to
`http://localhost:8384`.

Auth note: the daemon injects `window.__CLAWJOURNAL_API_TOKEN__` only when it
serves the built `dist/index.html`. The Vite dev server does not inject that
token. If dev-server API calls return `401`, verify the behavior through the
built app at `http://localhost:8384`, or temporarily add a local-only token
script while developing. Never commit a pasted token.

## Useful Commands

```bash
npm run dev      # Vite dev server on localhost:5173
npm run build    # Type-check and build dist/
npm run lint     # ESLint
npm run preview  # Preview built static files without the ClawJournal daemon
```

`npm run preview` is only a static Vite preview. It does not replace
`clawjournal serve` because it does not run the scanner, SQLite workbench API,
or token injection.

## Code Map

- `src/App.tsx` defines the top-level routes and sidebar.
- `src/views/` contains the main screens: Dashboard, Insights, Search,
  Sessions, Session Detail, Share, and Policies.
- `src/components/` contains reusable UI pieces.
- `src/api.ts` wraps the workbench API under `/api`.
- `src/types.ts` keeps frontend response types in one place.
- `src/theme.ts` holds shared colors and typography.
- `vite.config.ts` sets the dev port to `5173` and proxies `/api` to the
  daemon on `8384`.

## Troubleshooting

- Placeholder page on `localhost:8384`: run `npm install && npm run build`.
- Stale UI after edits: rebuild, then restart or refresh `clawjournal serve`.
- `401` from `localhost:5173`: use the built daemon-served app, or inject the
  local API token only for the current dev session.
- API looks empty: run `clawjournal scan` or let `clawjournal serve` finish its
  background scan.
- Port conflict: run the daemon on another port with `clawjournal serve --port
  <port>`. If you change the daemon port, update the Vite proxy target too.
