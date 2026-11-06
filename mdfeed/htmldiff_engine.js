/* ===========================================================================
   1. htmldiff — a port of htmldiff.net (Rohland/htmldiff.net), itself a port
   of myobie/htmldiff. Word-level diff that understands HTML tags, so the
   output is markup you can render, with changes wrapped in <ins>/<del>.

   Kept faithful to the C# so the upstream spec suite still passes, with two
   deliberate departures, both marked DEPARTURE below.
   =========================================================================== */

const RE_OPENING_TAG = /^\s*<[^>]+>\s*$/;
const RE_CLOSING_TAG = /^\s*<\/[^>]+>\s*$/;
const RE_TAG_WORD    = /<[^\s>]+/;
const RE_WHITESPACE  = /^(\s|&nbsp;)+$/;
const RE_TAG_NAME    = /<\/?([^\s/>]+)[^>]*>/;

// DEPARTURE: upstream compiles its word-character class in ECMAScript mode, so
// \w is ASCII and "café" tokenizes as "caf" + "é" — every accented word reads
// as a change. Unicode letters count as word characters here, minus the CJK
// blocks, which upstream splits per character and which genuinely do diff
// better that way (no spaces to tokenize on).
const RE_WORD_CHAR = /[\p{L}\p{N}\p{M}_#@]/u;
const RE_CJK = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]/u;

const SPECIAL_CASE_WORD_TAGS = ["<img"];
const RE_SPECIAL_OPEN = /<(strong|b|i|em|big|small|u|sub|sup|strike|s|span)[>\s]+/i;
const SPECIAL_CLOSING = new Set([
  "</strong>", "</em>", "</b>", "</i>", "</big>", "</small>",
  "</u>", "</sub>", "</sup>", "</strike>", "</s>", "</span>",
]);

// A `<` only opens a tag when a name character follows, as real HTML parsers require.
// Treating every `<` as a tag start swallows the rest of the text node into one token
// on ordinary prose like "5 < 10": that token then satisfies RE_OPENING_TAG, normalize
// reduces it to `</p>` on both sides, the matcher calls the region equal, and every
// change after the `<` is reported as no change at all.
const RE_TAG_NAME_START = /[A-Za-z!/?]/;
const isStartOfTag    = (c, next) => c === "<" && RE_TAG_NAME_START.test(next || "");
const isEndOfTag      = (c) => c === ">";
const isStartOfEntity = (c) => c === "&";
const isEndOfEntity   = (c) => c === ";";
const isWhitespaceChar = (c) => /\s/.test(c);
const isWhitespaceWord = (s) => RE_WHITESPACE.test(s);
const isWordChar = (c) => RE_WORD_CHAR.test(c) && !RE_CJK.test(c);

function isTag(item) {
  if (item != null && SPECIAL_CASE_WORD_TAGS.some((t) => item.startsWith(t))) return false;
  return RE_OPENING_TAG.test(item) || RE_CLOSING_TAG.test(item);
}

function stripTagAttributes(word) {
  const m = RE_TAG_WORD.exec(word);
  return (m ? m[0] : "") + (word.endsWith("/>") ? "/>" : ">");
}

const stripAnyAttributes = (word) => (isTag(word) ? stripTagAttributes(word) : word);

function getTagName(word) {
  if (word == null) return "";
  const m = RE_TAG_NAME.exec(word);
  return m ? m[1].toLowerCase() : "";
}

const wrapText = (text, tagName, cssClass) =>
  `<${tagName} class='${cssClass}'>${text}</${tagName}>`;

/* --- tokenizer: text into words, tags, entities and whitespace runs -------- */

function findBlocks(text, blockExpressions) {
  const blocks = new Map();
  for (const re of blockExpressions) {
    const g = new RegExp(re.source, re.flags.includes("g") ? re.flags : re.flags + "g");
    for (const m of text.matchAll(g)) {
      if (m[0].length === 0) continue;
      // Upstream throws on overlapping block expressions. A regex typed into a
      // text field overlaps constantly, so first match wins instead.
      if (!blocks.has(m.index)) blocks.set(m.index, m.index + m[0].length);
    }
  }
  return blocks;
}

function convertHtmlToWords(text, blockExpressions) {
  const blockLocations = findBlocks(text, blockExpressions || []);
  const blockCheckRequired = blockLocations.size > 0;

  let mode = "char";
  let globbingUntil = -1;
  let isGrouping = false;
  let currentWord = [];
  const words = [];

  const flush = () => {
    if (currentWord.length) {
      words.push(currentWord.join(""));
      currentWord = [];
    }
  };

  const globbing = (index, c) => {
    if (!blockCheckRequired) return false;
    if (index === globbingUntil) {
      globbingUntil = -1;
      isGrouping = false;
      flush();
    }
    if (blockLocations.has(index)) {
      isGrouping = true;
      globbingUntil = blockLocations.get(index);
    }
    if (isGrouping) {
      currentWord.push(c);
      mode = "char";
      return true;
    }
    return false;
  };

  // Step by code point, not by UTF-16 unit. Indexing by unit splits an astral
  // character in half; each lone surrogate then fails isWordChar (\p{L} doesn't match
  // an unpaired half), so the halves become separate words and a change boundary can
  // land inside one character — the redline renders U+FFFD where the emoji was.
  // `index` stays a UTF-16 offset because findBlocks keys on match indices from this
  // same string.
  for (let index = 0; index < text.length; ) {
    const c = String.fromCodePoint(text.codePointAt(index));
    const step = c.length;
    const next = text[index + step];
    if (globbing(index, c)) { index += step; continue; }

    if (mode === "char") {
      if (isStartOfTag(c, next)) { flush(); currentWord.push("<"); mode = "tag"; }
      else if (isStartOfEntity(c)) { flush(); currentWord.push(c); mode = "entity"; }
      else if (isWhitespaceChar(c)) { flush(); currentWord.push(c); mode = "ws"; }
      else if (isWordChar(c) && (currentWord.length === 0 || isWordChar(currentWord[currentWord.length - 1]))) {
        currentWord.push(c);
      } else { flush(); currentWord.push(c); }

    } else if (mode === "tag") {
      currentWord.push(c);
      if (isEndOfTag(c)) { flush(); mode = "char"; }

    } else if (mode === "ws") {
      if (isStartOfTag(c, next)) { flush(); currentWord.push(c); mode = "tag"; }
      else if (isStartOfEntity(c)) { flush(); currentWord.push(c); mode = "entity"; }
      else if (isWhitespaceChar(c)) { currentWord.push(c); }
      else { flush(); currentWord.push(c); mode = "char"; }

    } else if (mode === "entity") {
      if (isStartOfTag(c, next)) { flush(); currentWord.push(c); mode = "tag"; }
      else if (isWhitespaceChar(c)) { flush(); currentWord.push(c); mode = "ws"; }
      else if (isEndOfEntity(c)) {
        let switchToNextMode = true;
        if (currentWord.length) {
          currentWord.push(c);
          words.push(currentWord.join(""));
          // join a trailing &nbsp; onto the whitespace run before it
          if (words.length > 2 &&
              isWhitespaceWord(words[words.length - 2]) &&
              isWhitespaceWord(words[words.length - 1])) {
            const w1 = words[words.length - 2];
            const w2 = words[words.length - 1];
            words.splice(words.length - 2, 2);
            currentWord = (w1 + w2).split("");
            mode = "ws";
            switchToNextMode = false;
          }
        }
        if (switchToNextMode) { currentWord = []; mode = "char"; }
      }
      else if (isWordChar(c)) { currentWord.push(c); }
      else { flush(); currentWord.push(c); mode = "char"; }
    }
    index += step;
  }
  flush();
  return words;
}

/* --- longest matching block, by binary search on the run length ----------- */

/* What findMatch has to produce is the longest run of words the two sides
   share. The previous shape bucketed every block-of-N by key and, for each old
   word, walked that key's whole bucket — so on markup that repeats
   structurally (tables, listings, nav) buckets grew with the document and the
   scan ran O(words x repeats). One price changed in a product table measured
   0.7s at 70 KB and 39.7s at 561 KB, synchronous on the main thread.

   `repeatingWordsAccuracy` was meant to bound that by dropping keys that
   repeat too often, but its guard (`at.length > newWords.length * 1`) could
   not fire: a bucket can never hold more entries than there are words. Making
   it fire is not the fix. Measured on a 2,000-row table, dropping repeated
   keys makes identical rows diff as one giant replace — 44,000 words marked
   changed where 2 changed — and on varied rows it is 11x *slower* still,
   because every dropped key sends `find` down the blockSize ladder and
   `collect` fragments into more passes. The option is gone rather than fixed,
   so nobody re-derives that dead end from the leftover knob.

   "Is there a shared run of length L" is monotone in L, so binary search it:
   about log2(words) probes, each hashing every window of that length once.
   That is O(words log words) with no dependence on how often anything
   repeats. */

const HASH_BASE = 0x9e3779b1 | 0; // odd, so it has an inverse modulo 2^32

/** Inverse of an odd `a` modulo 2^32, by Newton iteration — the rolling hash
 *  divides out the word leaving the window, and this is what it multiplies by
 *  instead. Four rounds take 3 correct bits to well past 32. */
function inverseModulo32(a) {
  let x = a;
  for (let round = 0; round < 4; round++) x = Math.imul(x, (2 - Math.imul(a, x)) | 0) | 0;
  return x | 0;
}

const HASH_BASE_INVERSE = inverseModulo32(HASH_BASE);

/** Hash of every `length`-word window of `ids`, rolled rather than recomputed
 *  (recomputing each window would put the length back in the cost). */
function windowHashes(ids, length) {
  const windows = ids.length - length + 1;
  const hashes = new Int32Array(windows);

  let top = 1; // HASH_BASE^(length-1), the weight of the newest word
  for (let k = 1; k < length; k++) top = Math.imul(top, HASH_BASE);

  let hash = 0;
  let power = 1;
  for (let k = 0; k < length; k++) {
    hash = (hash + Math.imul(ids[k], power)) | 0;
    power = Math.imul(power, HASH_BASE);
  }
  hashes[0] = hash;

  for (let i = 1; i < windows; i++) {
    hash = Math.imul((hash - ids[i - 1]) | 0, HASH_BASE_INVERSE);
    hash = (hash + Math.imul(ids[i + length - 1], top)) | 0;
    hashes[i] = hash;
  }
  return hashes;
}

/** The earliest run of exactly `length` words shared by `a` and `b`, or null.
 *  Earliest in `a`, then earliest in `b` — the order the previous scan settled
 *  ties in, kept so an ambiguous diff renders the same as it always has.
 *
 *  `probeBudget` bounds total verification work. HASH_BASE is a fixed public
 *  constant, so a crafted input can force many windows onto the same hash
 *  without sharing content — the equality check below still returns an exact
 *  answer, but verifying every collision reopens the O(words x repeats)
 *  blowup this rewrite exists to close (a quadratic match phase). Once the budget
 *  runs out, findRun reports "not found" for this length rather than
 *  finishing the scan; findMatch's binary search then narrows to a shorter
 *  length it can verify within budget, so any run this returns is always a
 *  real, fully-verified match — just possibly shorter than optimal under an
 *  adversarial collision. */
function findRun(a, b, length, probeBudget) {
  if (length <= 0 || length > a.length || length > b.length) return null;

  const inB = new Map();
  const hashesB = windowHashes(b, length);
  for (let j = hashesB.length - 1; j >= 0; j--) {
    const at = inB.get(hashesB[j]);
    if (at) at.push(j);
    else inB.set(hashesB[j], [j]);
  }

  const hashesA = windowHashes(a, length);
  let budget = probeBudget;
  for (let i = 0; i < hashesA.length; i++) {
    const at = inB.get(hashesA[i]);
    if (!at) continue;
    // Descending above so this reads ascending, giving the earliest j.
    for (let index = at.length - 1; index >= 0; index--) {
      const j = at[index];
      // Two windows can share a hash without sharing content. Comparing keeps
      // the answer exact instead of merely probable.
      let k = 0;
      while (k < length && a[i + k] === b[j + k]) k++;
      if (k === length) return { startInA: i, startInB: j, size: length };
      // A mismatch still costs up to `k` comparisons; charge them so a run
      // of near-miss collisions can't hide under a cheap-looking probe count.
      budget -= k || 1;
      if (budget <= 0) return null;
    }
  }
  return null;
}

function findMatch(oldWords, newWords, startInOld, endInOld, startInNew, endInNew, opts) {
  const normalize = (word) => {
    let w = stripAnyAttributes(word);
    if (opts.ignoreWhitespace && isWhitespaceWord(w)) return " ";
    if (opts.ignoreCase) w = w.toLowerCase();
    return w;
  };

  // Number the normalized words so the hash and the comparison both work on
  // integers. It also drops the old key scheme's habit of joining a block with
  // "", which collided "ab"+"c" with "a"+"bc".
  const numbers = new Map();
  const idOf = (word) => {
    let id = numbers.get(word);
    if (id === undefined) numbers.set(word, (id = numbers.size + 1));
    return id;
  };
  const a = new Int32Array(endInOld - startInOld);
  for (let i = startInOld; i < endInOld; i++) a[i - startInOld] = idOf(normalize(oldWords[i]));
  const b = new Int32Array(endInNew - startInNew);
  for (let j = startInNew; j < endInNew; j++) b[j - startInNew] = idOf(normalize(newWords[j]));

  // Bounds every findRun call below to work linear in this call's input, so
  // the floor probe, the ladder above it, and the whole binary search stay
  // within a constant number of budgets — see findRun's docstring.
  const probeBudget = Math.max(4096, 64 * (a.length + b.length));

  // Probe the floor first: `find` walks blockSize down when nothing matches,
  // and this keeps each rung of that ladder to a single pass.
  let best = findRun(a, b, opts.blockSize, probeBudget);
  if (!best) return null;

  let low = opts.blockSize + 1;
  let high = Math.min(a.length, b.length);
  while (low <= high) {
    const middle = (low + high) >> 1;
    const run = findRun(a, b, middle, probeBudget);
    if (run) {
      best = run;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }

  const bestInOld = startInOld + best.startInA;
  const bestInNew = startInNew + best.startInB;
  return { startInOld: bestInOld, startInNew: bestInNew, size: best.size,
           endInOld: bestInOld + best.size, endInNew: bestInNew + best.size };
}

function htmlDiff(oldText, newText, options) {
  const opt = Object.assign({
    ignoreWhitespace: false,
    ignoreCase: false,
    orphanMatchThreshold: 0,
    blockExpressions: [],
  }, options);

  if (oldText === newText) return newText;

  const oldWords = convertHtmlToWords(oldText, opt.blockExpressions);
  const newWords = convertHtmlToWords(newText, opt.blockExpressions);
  const matchGranularity = Math.min(4, Math.min(oldWords.length, newWords.length));

  const find = (a0, a1, b0, b1) => {
    // Big texts usually hold a match longer than the maximum granularity; if
    // this one doesn't, walk the block size down until something matches.
    for (let size = matchGranularity; size > 0; size--) {
      const m = findMatch(oldWords, newWords, a0, a1, b0, b1, {
        blockSize: size,
        ignoreWhitespace: opt.ignoreWhitespace,
        ignoreCase: opt.ignoreCase,
      });
      if (m) return m;
    }
    return null;
  };

  // This was a right-leaning recursion whose depth equalled the number of
  // change sites exactly — 400 changes, 400 frames — the third instance of the
  // pattern already unwound in `insertTag` and `alignRun`. The bucket scan
  // above used to become prohibitive before the depth could reach a
  // RangeError, so it was only ever latent; it does not need to be again.
  //
  // An explicit stack, popped in reverse, keeps `matches` in document order:
  // everything left of a match, then the match, then everything right of it.
  const matches = [];
  const pending = [{ a0: 0, a1: oldWords.length, b0: 0, b1: newWords.length }];
  while (pending.length) {
    const work = pending.pop();
    if (work.match) { matches.push(work.match); continue; }

    const m = find(work.a0, work.a1, work.b0, work.b1);
    if (!m) continue;
    if (m.endInOld < work.a1 && m.endInNew < work.b1)
      pending.push({ a0: m.endInOld, a1: work.a1, b0: m.endInNew, b1: work.b1 });
    pending.push({ match: m });
    if (work.a0 < m.startInOld && work.b0 < m.startInNew)
      pending.push({ a0: work.a0, a1: m.startInOld, b0: work.b0, b1: m.startInNew });
  }

  matches.push({ startInOld: oldWords.length, startInNew: newWords.length, size: 0,
                 endInOld: oldWords.length, endInNew: newWords.length });

  const kept = removeOrphans(matches, oldWords, newWords, opt.orphanMatchThreshold);

  const operations = [];
  let positionInOld = 0, positionInNew = 0;
  for (const match of kept) {
    const atOld = positionInOld === match.startInOld;
    const atNew = positionInNew === match.startInNew;
    let action = null;
    if (!atOld && !atNew) action = "replace";
    else if (atOld && !atNew) action = "insert";
    else if (!atOld) action = "delete";

    if (action) {
      operations.push({ action, startInOld: positionInOld, endInOld: match.startInOld,
                        startInNew: positionInNew, endInNew: match.startInNew });
    }
    if (match.size !== 0) {
      operations.push({ action: "equal", startInOld: match.startInOld, endInOld: match.endInOld,
                        startInNew: match.startInNew, endInNew: match.endInNew });
    }
    positionInOld = match.endInOld;
    positionInNew = match.endInNew;
  }

  const out = [];
  const stack = [];
  for (const op of operations) {
    if (op.action === "equal") {
      out.push(newWords.slice(op.startInNew, op.endInNew).join(""));
    } else if (op.action === "delete") {
      insertTag(out, "del", "diffdel", oldWords.slice(op.startInOld, op.endInOld), stack);
    } else if (op.action === "insert") {
      insertTag(out, "ins", "diffins", newWords.slice(op.startInNew, op.endInNew), stack);
    } else if (op.action === "replace") {
      insertTag(out, "del", "diffmod", oldWords.slice(op.startInOld, op.endInOld), stack);
      insertTag(out, "ins", "diffmod", newWords.slice(op.startInNew, op.endInNew), stack);
    }
  }
  return out.join("");
}

/* A match with a gap on both sides that is tiny relative to those gaps is an
   accident of two unrelated texts sharing a word. Folding it into the
   surrounding change reads better than three fragments. */
function removeOrphans(matches, oldWords, newWords, orphanMatchThreshold) {
  const kept = [];
  let prev = null, curr = null;
  const sum = (words, from, to) => {
    let n = 0;
    for (let i = from; i < to; i++) n += words[i].length;
    return n;
  };

  for (const next of matches) {
    if (curr === null) {
      prev = { startInOld: 0, startInNew: 0, size: 0, endInOld: 0, endInNew: 0 };
      curr = next;
      continue;
    }
    const touchesLeft  = prev.endInOld === curr.startInOld && prev.endInNew === curr.startInNew;
    const touchesRight = curr.endInOld === next.startInOld && curr.endInNew === next.startInNew;
    if (touchesLeft || touchesRight) {
      kept.push(curr);
      prev = curr; curr = next;
      continue;
    }
    const oldDistance = sum(oldWords, prev.endInOld, next.startInOld);
    const newDistance = sum(newWords, prev.endInNew, next.startInNew);
    const currLength  = sum(newWords, curr.startInNew, curr.endInNew);
    if (currLength > Math.max(oldDistance, newDistance) * orphanMatchThreshold) kept.push(curr);
    prev = curr; curr = next;
  }
  if (curr !== null) kept.push(curr); // the last match is always vital
  return kept;
}

/* Wrapping a run of words in one <ins>/<del> would nest it across tags and
   produce invalid markup, so the run is split at every tag boundary and each
   text stretch wrapped on its own. Formatting tags (<b>, <span>, …) get the
   extra treatment of marking their *content* as changed rather than
   duplicating the tag. */
function insertTag(out, tag, cssClass, words, stack) {
  while (true) {
    if (words.length === 0) break;

    const untilFirstTag = extractConsecutiveWords(words, (w) => !isTag(w));
    if (untilFirstTag.length > 0) out.push(wrapText(untilFirstTag.join(""), tag, cssClass));
    if (words.length === 0) break;

    // What's left starts with a tag but may hold non-tags further on.
    const firstNonTag = words.findIndex((w) => !isTag(w));
    const lastTagInBlock = firstNonTag === -1 ? words.length - 1 : firstNonTag - 1;

    let injection = "";
    let injectionIsBefore = false;

    if (RE_SPECIAL_OPEN.test(words[0])) {
      stack.push(words[0]);
      injection = "<ins class='mod'>";
      if (tag === "del") {
        words.shift();
        while (words.length && RE_SPECIAL_OPEN.test(words[0])) words.shift();
      }
    } else if (SPECIAL_CLOSING.has(words[0].toLowerCase())) {
      const openingTag = stack.length ? stack.pop() : null;
      if (openingTag !== null && getTagName(openingTag) === getTagName(words[lastTagInBlock])) {
        injection = "</ins>";
        injectionIsBefore = true;
      } else if (openingTag !== null) {
        stack.push(openingTag); // mismatched pair — put it back
      }
      if (tag === "del") {
        words.shift();
        while (words.length && SPECIAL_CLOSING.has(words[0].toLowerCase())) words.shift();
      }
    }

    if (words.length === 0 && injection.length === 0) break;

    const tags = extractConsecutiveWords(words, isTag).join("");
    out.push(injectionIsBefore ? injection + tags : tags + injection);

    // Anything left starts with a non-tag, which is exactly what the top of
    // this loop handles. Upstream self-calls here; that recursion overflowed
    // the stack at a few thousand elements in one change block (deleting a
    // whole document does it), and the loop is equivalent.
  }
}

function extractConsecutiveWords(words, condition) {
  let firstFailure = null;
  for (let i = 0; i < words.length; i++) {
    const word = words[i];
    if (i === 0 && word === " ") words[i] = "&nbsp;";
    if (!condition(word)) { firstFailure = i; break; }
  }
  return firstFailure !== null ? words.splice(0, firstFailure) : words.splice(0, words.length);
}
