/**
 * Fuzzy matching — VS Code-style file picker.
 *
 * Characters must appear in order but can have gaps.
 * Higher score = better match (consecutive chars + boundary matches).
 */

export function fuzzyScore(query: string, target: string): number {
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  if (!q) return 0;

  let qi = 0;
  let score = 0;
  let consecutive = 0;

  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      qi++;
      consecutive++;
      // Bonus for: start of string, after separator, uppercase letter
      const boundary = ti === 0 ||
        t[ti - 1] === "/" || t[ti - 1] === "_" || t[ti - 1] === "-" ||
        t[ti - 1] === "." ||
        (t[ti] !== t[ti].toLowerCase() && (ti === 0 || t[ti - 1] === t[ti - 1].toLowerCase()));
      score += boundary ? 10 + consecutive : 1 + consecutive;
    } else {
      consecutive = 0;
    }
  }

  // All query chars matched?
  if (qi < q.length) return -1;
  return score;
}

/** Sort by fuzzy score descending, then alphabetically. */
export function fuzzyFilter<T>(
  items: T[],
  query: string,
  getText: (item: T) => string,
  limit = 10,
): T[] {
  const q = query.trim();
  if (!q) return items.slice(0, limit);

  const scored = items
    .map((item) => ({ item, score: fuzzyScore(q, getText(item)) }))
    .filter((s) => s.score >= 0)
    .sort((a, b) => b.score - a.score || getText(a.item).localeCompare(getText(b.item)));

  return scored.slice(0, limit).map((s) => s.item);
}
