#!/usr/bin/env node
// Serve htmldiff.html from localhost so the page can fetch its own inputs.
//
// The standalone file cannot: a cross-origin GET from a file:// document is
// blocked, which is why browser-based differs make you paste. Served from a
// local origin with a same-origin proxy beside it, the page gets URL fields
// and a re-fetch button, and comparing two live pages stops being a
// round-trip through the shell.
//
//   node htmldiff/cli.mjs                        # serve, open, paste or type URLs
//   node htmldiff/cli.mjs a.html b.html          # serve, opened on the diff
//   node htmldiff/cli.mjs https://a https://b    # same, both fetched first
//   node htmldiff/cli.mjs --static a.html b.html   # no server: a plain file://
//
// (`npx @mparrett/htmldiff` once the package is published; see README.)
//
// Zero dependencies, by design — `npx` should not pull a tree to show a diff.

import { createServer } from "node:http";
import { readFile, writeFile, mkdtemp } from "node:fs/promises";
import { randomBytes } from "node:crypto";
import { spawn } from "node:child_process";
import { tmpdir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const HERE = dirname(fileURLToPath(import.meta.url));
const PAGE = join(HERE, "htmldiff.html");

const DEFAULT_USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 htmldiff";
const DEFAULT_TIMEOUT = 20_000;
const DEFAULT_MAX_BYTES = 8 * 1024 * 1024;

const usage = `htmldiff — compare two web pages

  htmldiff [old] [new]        old/new may each be a file path or an http(s) URL

Options
  -p, --port <n>       port to serve on (default: an open one)
  -H, --header <h>     extra request header, repeatable ('Name: value')
      --user-agent <s> override the User-Agent sent when fetching
      --timeout <ms>   per-request timeout (default: ${DEFAULT_TIMEOUT})
      --max-bytes <n>  refuse responses larger than this (default: ${DEFAULT_MAX_BYTES})
  -k, --insecure       skip TLS certificate checks
      --static         write a standalone file:// page instead of serving
      --no-open        print the URL or path, don't launch a browser
  -h, --help           show this
`;

// ---------------------------------------------------------------- fetching

function isUrl(value) {
  try {
    return ["http:", "https:"].includes(new URL(value).protocol);
  } catch {
    return false;
  }
}

/** GET a URL. Returns { text, finalUrl } — final after any redirects. */
async function fetchUrl(url, options) {
  const { headers, timeout, maxBytes } = options;
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), timeout);
  try {
    const response = await fetch(url, {
      headers,
      redirect: "follow",
      signal: abort.signal,
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status} ${response.statusText}`);
    }

    // Read in chunks so an oversize body is refused rather than truncated —
    // a truncated body diffs as if the page ended there.
    const chunks = [];
    let total = 0;
    for await (const chunk of response.body) {
      total += chunk.length;
      if (total > maxBytes) {
        throw new Error(`response exceeds ${maxBytes} bytes; raise --max-bytes to fetch it`);
      }
      chunks.push(chunk);
    }

    const body = Buffer.concat(chunks);
    const declared = /charset=["']?\s*([\w-]+)/i.exec(
      response.headers.get("content-type") || "",
    )?.[1];
    return { text: decode(body, declared), finalUrl: response.url || url };
  } finally {
    clearTimeout(timer);
  }
}

function decode(body, declared) {
  const sniffed = declared || /<meta[^>]+charset=["']?\s*([\w-]+)/i.exec(
    body.subarray(0, 2048).toString("latin1"),
  )?.[1];
  for (const charset of [sniffed, "utf-8"]) {
    if (!charset) continue;
    try {
      return new TextDecoder(charset, { fatal: false }).decode(body);
    } catch {
      /* unknown label — fall through to utf-8 */
    }
  }
  return body.toString("utf-8");
}

// ------------------------------------------------------------- page assembly

/** Escape a JSON blob for a <script> body — the inputs are HTML, so anything
 *  the HTML tokenizer reacts to has to go.
 *
 *  Escaping only "</" closes the "</script>" hole and opens its sibling: the
 *  tokenizer enters script-data-double-escaped state at "<!--" and leaves it
 *  at the next "</script>", which that escape has just hidden. An input with
 *  "<!--" and no "-->" before the page's next <script> then swallows the rest
 *  of the document, and the tab renders blank with no diagnostic. Escaping the
 *  "<" itself has no such sibling: no "<" reaches the tokenizer, so no state
 *  it recognizes can be entered. JSON and JS both read the escape back as "<".
 *
 *  Keep in step with `build()` in ./htmldiff, which seeds the same page. */
const embed = (value) => JSON.stringify(value).replace(/</g, "\\u003c");

async function buildPage({ preload, server }) {
  const page = await readFile(PAGE, "utf-8");
  const parts = [];
  if (preload) parts.push(`window.__htmldiffPreload = ${embed(preload)};`);
  if (server) parts.push(`window.__htmldiffServer = ${embed(server)};`);
  if (!parts.length) return page;
  if (!page.includes("</head>")) throw new Error("htmldiff.html has no </head> to inject into");
  // The replacement is a *function* on purpose. As a string, `$$`, `$&`, "$`"
  // and `$'` are substitution patterns even when the search value is a plain
  // string — and the replacement here is fetched page content. `$$` alone
  // silently halves to `$` (React streaming payloads carry "$$typeof", and
  // prices and jQuery's `$$()` do it too), which diffs as a change that isn't
  // there; "$`" splices this file's own head into the JSON and breaks it.
  return page.replace("</head>", () => `<script>${parts.join("\n")}</script>\n</head>`);
}

function preloadFor(sides) {
  const [old, changed] = sides;
  if (!old && !changed) return null;
  return {
    a: old?.text ?? "",
    b: changed?.text ?? "",
    aLabel: old?.label ?? null,
    bLabel: changed?.label ?? null,
  };
}

// ------------------------------------------------------------------- server

/**
 * A localhost fetch proxy is an SSRF surface: any page you have open could
 * reach 127.0.0.1:<port> and use this process as a fetcher. The defences are
 * that it binds to loopback only, requires a per-run token that only the
 * served page is given, sends no CORS headers so no foreign origin can read a
 * reply, and rejects requests carrying a cross-site Origin.
 *
 * The Host check matters more than it looks. `fetch()` omits Origin on
 * same-origin GETs, so the Origin test alone is close to vestigial, and `GET /`
 * hands out the token unauthenticated. That is a DNS-rebinding chain: a page on
 * http://evil.example:<port> that rebinds to 127.0.0.1 becomes same-origin,
 * reads the token from `/`, and has a readable proxy into the private network.
 * Rebinding cannot forge the Host header, so pinning it to loopback closes it.
 *
 * Private and link-local *targets* are deliberately not blocked — diffing a
 * staging box against production is the main reason this exists.
 */
function startServer({ port, fetchOptions, preload }) {
  const token = randomBytes(24).toString("base64url");

  const handle = async (request, response) => {
    const listening = server.address().port;

    const host = request.headers.host || "";
    const allowedHosts = [
      `127.0.0.1:${listening}`,
      `localhost:${listening}`,
      `[::1]:${listening}`,
    ];
    if (!allowedHosts.includes(host)) {
      response.writeHead(403).end("unexpected Host; this server is loopback-only");
      return;
    }

    const origin = request.headers.origin;
    if (origin && origin !== `http://${host}`) {
      response.writeHead(403).end("cross-origin request refused");
      return;
    }

    // Node's HTTP parser accepts absolute-form targets that WHATWG URL rejects
    // (`GET http://[ HTTP/1.1` is enough), so this is reachable from the wire
    // and has to answer rather than throw. It sits *after* the Host gate to
    // keep loopback-only the first thing this server enforces.
    let url;
    try {
      url = new URL(request.url, "http://127.0.0.1");
    } catch {
      response.writeHead(400).end("malformed request target");
      return;
    }

    if (url.pathname === "/") {
      const page = await buildPage({
        preload,
        server: { token, endpoint: "/fetch" },
      });
      response.writeHead(200, {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "no-store",
      });
      response.end(page);
      return;
    }

    if (url.pathname === "/fetch") {
      if (url.searchParams.get("token") !== token) {
        response.writeHead(403, { "content-type": "application/json" });
        response.end(JSON.stringify({ error: "bad or missing token" }));
        return;
      }
      const target = url.searchParams.get("url") || "";
      if (!isUrl(target)) {
        response.writeHead(400, { "content-type": "application/json" });
        response.end(JSON.stringify({ error: "only http and https URLs can be fetched" }));
        return;
      }
      try {
        const { text, finalUrl } = await fetchUrl(target, fetchOptions);
        process.stderr.write(
          `fetched ${finalUrl} (${Buffer.byteLength(text).toLocaleString()} bytes)\n`,
        );
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify({ text, finalUrl }));
      } catch (error) {
        response.writeHead(502, { "content-type": "application/json" });
        response.end(JSON.stringify({ error: String(error.message || error) }));
      }
      return;
    }

    response.writeHead(404).end("not found");
  };

  // An `async` handler passed straight to createServer turns any throw into an
  // unhandled rejection, which takes the process down and the session with it.
  // `buildPage` re-reads htmldiff.html on every GET /, so editing that file
  // while a server runs was enough: its deliberate "no </head> to inject into"
  // error is written to be reported, and instead printed a stack trace and
  // exited.
  const server = createServer((request, response) => {
    handle(request, response).catch((error) => {
      process.stderr.write(`request failed: ${error?.stack || error}\n`);
      if (!response.headersSent) response.writeHead(500, { "content-type": "text/plain" });
      if (!response.writableEnded) response.end("htmldiff: internal error, see the terminal\n");
    });
  });

  return new Promise((ok, fail) => {
    server.once("error", fail);
    server.listen(port ?? 0, "127.0.0.1", () => ok({ server, token }));
  });
}

// -------------------------------------------------------------------- open

function openInBrowser(target) {
  const command =
    process.platform === "darwin"
      ? ["open", target]
      : process.platform === "win32"
        ? ["cmd", "/c", "start", "", target]
        : ["xdg-open", target];
  try {
    spawn(command[0], command.slice(1), { stdio: "ignore", detached: true }).unref();
  } catch (error) {
    process.stderr.write(`could not open a browser (${error.message}); it is at ${target}\n`);
  }
}

// -------------------------------------------------------------------- main

async function main() {
  let parsed;
  try {
    parsed = parseArgs({
      allowPositionals: true,
      options: {
        port: { type: "string", short: "p" },
        header: { type: "string", short: "H", multiple: true, default: [] },
        "user-agent": { type: "string", default: DEFAULT_USER_AGENT },
        timeout: { type: "string" },
        "max-bytes": { type: "string" },
        insecure: { type: "boolean", short: "k", default: false },
        static: { type: "boolean", default: false },
        // parseArgs has no --no-<flag> negation, so this is its own option
        "no-open": { type: "boolean", default: false },
        help: { type: "boolean", short: "h", default: false },
      },
    });
  } catch (error) {
    process.stderr.write(`${error.message}\n\n${usage}`);
    return 2;
  }

  const { values, positionals } = parsed;
  if (values.help) {
    process.stdout.write(usage);
    return 0;
  }
  if (positionals.length > 2) {
    process.stderr.write("at most two positionals: old and new\n");
    return 2;
  }

  const headers = { "user-agent": values["user-agent"], accept: "text/html,*/*" };
  for (const raw of values.header) {
    const at = raw.indexOf(":");
    if (at < 1) {
      process.stderr.write(`headers are 'Name: value': ${raw}\n`);
      return 2;
    }
    headers[raw.slice(0, at).trim()] = raw.slice(at + 1).trim();
  }

  if (values.insecure) {
    // fetch() has no per-request TLS knob, and reaching for undici internals
    // to get one costs more than it buys for an explicitly-requested opt-out.
    process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
    process.stderr.write("warning: TLS certificate checks are off\n");
  }

  const fetchOptions = {
    headers,
    timeout: Number(values.timeout ?? DEFAULT_TIMEOUT),
    maxBytes: Number(values["max-bytes"] ?? DEFAULT_MAX_BYTES),
  };

  const sides = [];
  for (const value of positionals) {
    if (isUrl(value)) {
      try {
        const { text, finalUrl } = await fetchUrl(value, fetchOptions);
        process.stderr.write(
          `fetched ${finalUrl} (${Buffer.byteLength(text).toLocaleString()} bytes)\n`,
        );
        sides.push({ text, label: finalUrl });
      } catch (error) {
        process.stderr.write(`${value}: ${error.message || error}\n`);
        return 1;
      }
    } else {
      try {
        sides.push({ text: await readFile(resolve(value), "utf-8"), label: null });
      } catch {
        process.stderr.write(`no such file: ${value}\n`);
        return 1;
      }
    }
  }
  const preload = preloadFor(sides);

  if (values.static) {
    const directory = await mkdtemp(join(tmpdir(), "htmldiff-"));
    const out = join(directory, "htmldiff.html");
    await writeFile(out, await buildPage({ preload, server: null }), "utf-8");
    if (values["no-open"]) process.stdout.write(`${pathToFileURL(out).href}\n`);
    else openInBrowser(out);
    return 0;
  }

  let server;
  try {
    ({ server } = await startServer({
      port: values.port ? Number(values.port) : undefined,
      fetchOptions,
      preload,
    }));
  } catch (error) {
    // Everywhere else the CLI answers a bad invocation with one line
    // ("no such file: x"); a busy -p was the one that printed a 12-line dump.
    if (error?.code === "EADDRINUSE") {
      process.stderr.write(`port ${values.port} is already in use\n`);
      return 1;
    }
    if (error?.code === "EACCES") {
      process.stderr.write(`not allowed to listen on port ${values.port}\n`);
      return 1;
    }
    throw error;
  }
  const address = `http://127.0.0.1:${server.address().port}/`;
  process.stderr.write(`htmldiff serving ${address}  (ctrl-c to stop)\n`);
  if (values["no-open"]) process.stdout.write(`${address}\n`);
  else openInBrowser(address);

  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => {
      server.close();
      process.exit(0);
    });
  }
  return null; // keep the process alive for the server
}

const code = await main();
if (code !== null) process.exit(code);
