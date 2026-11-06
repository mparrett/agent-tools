# mdserve

A standalone markdown/code browser that converts files to styled HTML and serves them locally with navigation, search, syntax highlighting, and a light/dark theme toggle.

## Quick Start

```bash
uv run mdserve/mdserve.py docs/              # serve a directory
uv run mdserve/mdserve.py .                  # serve current directory
uv run mdserve/mdserve.py docs/ 9000         # custom port (default: 8037)
```

## Supported File Types

`.md`, `.markdown`, `.py`, `.js`, `.ts`

Markdown is rendered to HTML via mistune. Code files get syntax-highlighted via highlight.js.

## Modes

### Serve (default)

Scans a directory, converts all supported files to HTML in a temp directory, generates an index page, and serves everything in your browser.

```bash
uv run mdserve/mdserve.py docs/
```

### Convert Only

Converts a single file to `<filename>.html` in the same directory. No server.

```bash
uv run mdserve/mdserve.py --convert-only path/to/file.md
```

### Export Markdown with Inline Images

Outputs markdown to stdout with relative images base64-encoded as reference-style links. Useful for pasting into Google Docs.

```bash
uv run mdserve/mdserve.py --export-md-inline path/to/file.md > output.md
```

## Options

| Flag | Description |
|------|-------------|
| `--no-browser` | Don't auto-open the browser |
| `--include PATTERN` | Only serve files matching pattern (repeatable) |
| `--exclude PATTERN` | Skip files matching pattern (repeatable) |
| `--no-cleanup` | Keep generated HTML after server stops |
| `--no-copy-images` | Don't copy images referenced in markdown |
| `--verbose`, `-v` | Print each file as it's converted |

`--include` and `--exclude` are mutually exclusive.

## Keyboard Shortcuts (in browser)

| Key | Action |
|-----|--------|
| `Left` / `Right` | Navigate between documents |
| `Home` | Jump to index |
| `Esc` | Close raw source modal |
| `/` or `Ctrl+K` | Focus the file filter |

## How It Works

1. Walks the directory tree (skipping `node_modules`, `.git`, `venv`, etc.)
2. Converts each file to a self-contained HTML page with navigation data baked in
3. Generates an index page with search and file type filters
4. Copies relative images referenced in markdown to the serve directory
5. Serves everything via Python's `http.server` on `localhost:8037`
6. Cleans up the temp directory on exit (unless `--no-cleanup`)

The pill in the bottom-right corner cycles the theme **auto → light → dark** (auto follows your OS). The choice is saved in the browser and, on file pages, swaps the highlight.js stylesheet to match.
