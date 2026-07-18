# AGENTS.MD

## rules

- **never edit `pyproject.toml`.** use `uv add` / `uv remove` only. only dog edit this file
- **`uv` only.** no `pip`, `npm`, `pnpm`, or `yarn`.
- **reply in vietnamese** with proper accents. be concise.

## architecture

```tree
web_gui.py    — entrypoint, pywebview winforms window, api class (js-python bridge)
module.py     — api logic: oauth pkce, opencode go scraping, openai usage, account storage
ui/index.html — vue 3 + tailwind v4 cdn, no build step, it just is 1 screen only. don't fucking add react, npm, webpack, or any node garbage.
```

- data stored in `%localappdata%/UsageTracker/`
- opencode auth read from `~/.local/share/opencode/auth.json`
- `webview.create_window(…, js_api=api)` exposes `api` methods to js via `window.pywebview.api`

## commands

- never fucking run any dev command like `uv run web_gui.py`, user will run this for you

```powershell
# lint
uv run ruff check .

# format
uv run ruff format .

# type check
uv run ty check
```

## uv quick reference

```powershell
uv add <pkg> # add production dep
uv add --dev <pkg> # add dev dep
uv remove --dev <pkg> # remove dev dep
uvx <tool> # run one-off tool, no install

```

## gotchas

- `pywebview==5.4`, `httpx==0.28.1` is pinned as dev dependency — don't bump without asking like stupid dog.
- all deps are pinned exactly (`add-bounds = "exact"` in uv config).

## docs

- [pywebview API](https://pywebview.flowrl.com/guide/api.html)
- [pywebview JS API example](https://github.com/r0x0r/pywebview/blob/docs/examples/js_api.py)

