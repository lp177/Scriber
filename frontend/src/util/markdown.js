// Tiny, dependency-free and XSS-safe Markdown -> HTML renderer.
//
// Security model: the raw source is HTML-escaped FIRST (& < > "), so no raw
// markup from the input can ever reach the DOM. Only AFTER escaping do we apply
// a small, fixed set of Markdown transforms that emit our own trusted tags.
// Links are restricted to http(s) URLs; because the URL was escaped up front, a
// double quote in it becomes &quot; and cannot break out of the href attribute.
//
// Supported: h1-h3 (`#`), bold (`**`), italic (`*` / `_`), inline `code`,
// fenced ```code``` blocks, unordered (`- `) and ordered (`1.`) lists,
// links `[text](http…)`, paragraphs and single line breaks.

/** Escape the HTML-significant characters so the input can never inject markup. */
function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * Apply inline transforms to a single (already HTML-escaped) line of text.
 * Code spans and links are stashed behind NUL-byte placeholders before the
 * emphasis rules run, so markers inside them are left untouched and the restore
 * step cannot collide with ordinary content.
 */
function renderInline(text) {
  const tokens = [];
  const stash = (html) => {
    tokens.push(html);
    return "\u0000" + (tokens.length - 1) + "\u0000";
  };

  let out = text;

  // Inline code spans (highest precedence).
  out = out.replace(/`([^`]+)`/g, (_, code) => stash(`<code>${code}</code>`));

  // Links [label](url) — only http(s) URLs are allowed through.
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (whole, label, url) => {
    if (/^https?:\/\//i.test(url)) {
      return stash(
        `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`,
      );
    }
    return whole;
  });

  // Emphasis: bold before italic so "**x**" is not mis-parsed as italic.
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  out = out.replace(/_([^_\n]+)_/g, "<em>$1</em>");

  // Restore the stashed code spans and links.
  out = out.replace(/\u0000(\d+)\u0000/g, (_, index) => tokens[Number(index)]);
  return out;
}

/**
 * Render a Markdown string to a safe HTML string.
 * @param {string} src Markdown source text.
 * @returns {string} HTML fragment (already escaped and sanitized).
 */
export function renderMarkdown(src) {
  if (src === null || src === undefined || src === "") {
    return "";
  }

  const lines = escapeHtml(String(src)).split(/\r?\n/);
  const out = [];
  let paragraph = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      out.push(`<p>${paragraph.map(renderInline).join("<br>")}</p>`);
      paragraph = [];
    }
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block: everything until the closing fence is literal.
    if (/^```/.test(line)) {
      flushParagraph();
      const code = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) {
        code.push(lines[i]);
        i += 1;
      }
      i += 1; // Skip the closing fence (or fall off the end).
      out.push(`<pre><code>${code.join("\n")}</code></pre>`);
      continue;
    }

    // Blank line ends the current paragraph.
    if (/^\s*$/.test(line)) {
      flushParagraph();
      i += 1;
      continue;
    }

    // Headings (levels 1-3).
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      const level = heading[1].length;
      out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }

    // Unordered list.
    if (/^\s*-\s+/.test(line)) {
      flushParagraph();
      const items = [];
      while (i < lines.length && /^\s*-\s+/.test(lines[i])) {
        items.push(`<li>${renderInline(lines[i].replace(/^\s*-\s+/, ""))}</li>`);
        i += 1;
      }
      out.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    // Ordered list.
    if (/^\s*\d+\.\s+/.test(line)) {
      flushParagraph();
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(`<li>${renderInline(lines[i].replace(/^\s*\d+\.\s+/, ""))}</li>`);
        i += 1;
      }
      out.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    // Otherwise accumulate into the current paragraph.
    paragraph.push(line);
    i += 1;
  }

  flushParagraph();
  return out.join("\n");
}
