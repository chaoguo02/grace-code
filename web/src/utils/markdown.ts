const ESCAPE_MAP: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };

function escapeHtml(s: string): string {
  return s.replace(/[&<>"]/g, (c) => ESCAPE_MAP[c] || c);
}

/**
 * Render markdown-formatted text to a sanitized HTML string.
 *
 * Step 1: Extract fenced code blocks and replace with placeholders.
 * Step 2: Escape ALL remaining HTML (prevents XSS via <script>, <img onerror>, etc.).
 * Step 3: Apply markdown formatting (bold, italic, code, headings) to already-escaped text.
 * Step 4: Restore escaped code blocks.
 *
 * Returns null when *text* is empty/whitespace.
 * Safe for use with React's dangerouslySetInnerHTML because all user content
 * is escaped BEFORE any formatting tags are injected.
 */
export function renderMarkdownSafe(text: string | undefined | null): { __html: string } | null {
  if (!text || !text.trim()) return null;

  const codeBlocks: { lang: string; body: string }[] = [];
  let html = text.replace(
    /```(\w*)\n([\s\S]*?)```/g,
    (_: string, lang: string, body: string) => {
      const idx = codeBlocks.length;
      codeBlocks.push({ lang, body });
      return `\0CODE${idx}\0`;
    },
  );

  const tableBlocks: string[] = [];
  html = html.replace(
    /(?:^|\n)((?:\|.*\|\n?)+)/g,
    (_match: string, block: string) => {
      const rows = block.trim().split(/\n+/).filter(Boolean);
      if (rows.length < 2) return block;
      const cells = rows.map((row) => row.trim().split("|").map((cell) => cell.trim()).filter(Boolean));
      if (cells.length < 2 || cells[0].length === 0) return block;
      const bodyRows = cells.slice(2).filter((row) => row.length > 0);
      const tableIndex = tableBlocks.length;
      tableBlocks.push(JSON.stringify({ header: cells[0], rows: bodyRows }));
      return `\0TABLE${tableIndex}\0`;
    },
  );

  // Escape ALL user content BEFORE injecting any formatting tags
  html = escapeHtml(html);

  // Links, headings, blockquotes, bold, italic, inline code
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  html = html.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>');
  html = html.replace(/\*\*(\S.*?\S)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(\S.*?\S)\*/g, '<em>$1</em>');
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\n/g, '<br/>');

  html = html.replace(/\0TABLE(\d+)\0/g, (_match: string, idx: string) => {
    const parsed = JSON.parse(tableBlocks[+idx]) as { header: string[]; rows: string[][] };
    const head = parsed.header.map((cell) => `<th>${escapeHtml(cell)}</th>`).join("");
    const rows = parsed.rows.map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(cell)}</td>`).join("")}</tr>`).join("");
    return `<table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
  });

  // Restore code blocks
  html = html.replace(/\0CODE(\d+)\0/g, (_match: string, idx: string) => {
    const cb = codeBlocks[+idx];
    return `<pre><code>${escapeHtml(cb.body)}</code></pre>`;
  });

  return { __html: html };
}
