import { useId, useLayoutEffect, useRef, useState } from "react";

interface ExpandableTextProps {
  /** Full text content. */
  text: string;
  /**
   * When set, text longer than this will be truncated with a "Show more" toggle.
   * The truncated preview shows maxLength characters + "…".
   */
  maxLength?: number;
  /**
   * CSS max-height applied when collapsed. Use instead of or with maxLength.
   * Content below the fold is revealed on expand.
   */
  maxCollapsedHeight?: number;
  /** Additional class for the text container. */
  className?: string;
  /** Label for the expand button. Default: "Show more". */
  expandLabel?: string;
  /** Label for the collapse button. Default: "Show less". */
  collapseLabel?: string;
  /** If true, renders as a <pre> block (monospace, preserves whitespace). */
  pre?: boolean;
}

/**
 * Renders text with an optional character limit and/or max-height.
 * When content exceeds the limit, a toggle button lets the user expand or collapse.
 */
export function ExpandableText({
  text,
  maxLength,
  maxCollapsedHeight,
  className,
  expandLabel = "Show more",
  collapseLabel = "Show less",
  pre = false,
}: ExpandableTextProps) {
  const reactId = useId();
  const contentRef = useRef<HTMLPreElement & HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);

  const charTruncated = maxLength != null && text.length > maxLength;

  // Only show toggle for maxCollapsedHeight when content actually overflows
  const [heightOverflows, setHeightOverflows] = useState(false);
  useLayoutEffect(() => {
    if (maxCollapsedHeight == null) return;
    const el = contentRef.current;
    if (!el) return;
    // Temporarily remove max-height to measure natural height
    const prev = el.style.maxHeight;
    el.style.maxHeight = "none";
    const overflows = el.scrollHeight > maxCollapsedHeight;
    el.style.maxHeight = prev;
    setHeightOverflows(overflows);
  }, [text, maxCollapsedHeight]);

  const needsToggle =
    charTruncated || (maxCollapsedHeight != null && heightOverflows);

  const displayText =
    !expanded && charTruncated ? text.slice(0, maxLength) + "…" : text;

  const containerStyle: React.CSSProperties | undefined =
    !expanded && maxCollapsedHeight != null
      ? { maxHeight: maxCollapsedHeight, overflow: "hidden" }
      : undefined;

  const contentId = `${reactId}-content`;
  const Tag = pre ? "pre" : "div";

  return (
    <div className="expandable-text">
      <Tag
        ref={contentRef}
        id={contentId}
        className={className}
        style={containerStyle}
      >
        {displayText}
      </Tag>
      {needsToggle && (
        <button
          type="button"
          className="expandable-text-toggle"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          aria-controls={contentId}
        >
          {expanded ? collapseLabel : expandLabel}
        </button>
      )}
    </div>
  );
}
