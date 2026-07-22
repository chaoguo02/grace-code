import { renderMarkdownSafe } from "../utils/markdown";

interface MarkdownRendererProps {
  content?: string | null;
  className?: string;
}

export function MarkdownRenderer({ content, className }: MarkdownRendererProps) {
  const rendered = renderMarkdownSafe(content);
  if (!rendered) return null;
  return <div className={className} dangerouslySetInnerHTML={rendered} />;
}
