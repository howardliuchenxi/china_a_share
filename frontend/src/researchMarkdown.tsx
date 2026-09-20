import { Fragment, type ReactNode } from "react";

/**
 * Deterministic renderer for the markdown subset the research model emits.
 *
 * The agent answers use bold spans, short headings, bullet lists, GFM tables,
 * and single newline separation. Rendering builds React nodes directly (never
 * innerHTML), so untrusted text stays escaped and `**` markers turn into real
 * bold instead of literal asterisks.
 */

interface MarkdownTable {
  kind: "table";
  header: string[];
  rows: string[][];
}

interface MarkdownList {
  kind: "list";
  ordered: boolean;
  items: string[];
}

interface MarkdownHeading {
  kind: "heading";
  level: number;
  text: string;
}

interface MarkdownParagraph {
  kind: "paragraph";
  lines: string[];
}

type MarkdownBlock = MarkdownTable | MarkdownList | MarkdownHeading | MarkdownParagraph;

const HEADING_PATTERN = /^(#{1,4})\s+(.*)$/;
const BULLET_PATTERN = /^[-*]\s+(.*)$/;
const ORDERED_PATTERN = /^\d+[.、]\s+(.*)$/;

function splitTableRow(line: string): string[] {
  const trimmed = line.trim();
  const body = trimmed.startsWith("|") ? trimmed.slice(1) : trimmed;
  const withoutEdge = body.endsWith("|") ? body.slice(0, -1) : body;
  return withoutEdge.split("|").map((cell) => cell.trim());
}

function isTableSeparator(line: string): boolean {
  const cells = splitTableRow(line);
  return cells.length > 0
    && cells.every((cell) => /^:?-{3,}:?$/.test(cell))
    && cells.some((cell) => cell.length > 0);
}

function isTableLine(line: string): boolean {
  return line.trim().startsWith("|");
}

function parseBlocks(text: string): MarkdownBlock[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: MarkdownBlock[] = [];
  let paragraph: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length > 0) {
      blocks.push({ kind: "paragraph", lines: paragraph });
      paragraph = [];
    }
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index] ?? "";
    const trimmed = line.trim();

    if (trimmed === "") {
      flushParagraph();
      continue;
    }

    const heading = trimmed.match(HEADING_PATTERN);
    if (heading) {
      flushParagraph();
      blocks.push({ kind: "heading", level: heading[1].length, text: heading[2] });
      continue;
    }

    if (isTableLine(line) && index + 1 < lines.length && isTableSeparator(lines[index + 1] ?? "")) {
      flushParagraph();
      const header = splitTableRow(line);
      index += 2;
      const rows: string[][] = [];
      while (index < lines.length && isTableLine(lines[index] ?? "")) {
        rows.push(splitTableRow(lines[index] ?? ""));
        index += 1;
      }
      index -= 1;
      blocks.push({ kind: "table", header, rows });
      continue;
    }

    const bulletItem = trimmed.match(BULLET_PATTERN);
    const orderedItem = trimmed.match(ORDERED_PATTERN);
    if (bulletItem || orderedItem) {
      flushParagraph();
      const ordered = Boolean(orderedItem);
      const itemPattern = ordered ? ORDERED_PATTERN : BULLET_PATTERN;
      const items: string[] = [];
      while (index < lines.length) {
        const candidate = (lines[index] ?? "").trim();
        const item = candidate.match(itemPattern);
        if (!item) break;
        items.push(item[1]);
        index += 1;
      }
      index -= 1;
      blocks.push({ kind: "list", ordered, items });
      continue;
    }

    paragraph.push(trimmed);
  }

  flushParagraph();
  return blocks;
}

function parseInline(text: string): ReactNode[] {
  const segments = text.split(/\*\*([^*]+)\*\*/g);
  if (segments.length === 1) return [text];
  return segments.map((segment, index) => (
    index % 2 === 1 ? <strong key={index}>{segment}</strong> : segment
  ));
}

function renderInlineLines(lines: string[], blockKey: number): ReactNode {
  return (
    <Fragment key={blockKey}>
      {lines.flatMap((line, lineIndex) => (
        <Fragment key={lineIndex}>
          {lineIndex > 0 && <br />}
          {parseInline(line)}
        </Fragment>
      ))}
    </Fragment>
  );
}

export function ResearchMarkdown({ text }: { text: string }): ReactNode {
  const blocks = parseBlocks(text);
  return (
    <div className="research-markdown">
      {blocks.map((block, blockIndex) => {
        if (block.kind === "heading") {
          const HeadingTag = `h${Math.min(block.level + 2, 6)}` as "h3" | "h4" | "h5" | "h6";
          return (
            <HeadingTag key={blockIndex} className="research-markdown-heading">
              {parseInline(block.text)}
            </HeadingTag>
          );
        }
        if (block.kind === "paragraph") {
          return (
            <p key={blockIndex} className="research-markdown-paragraph">
              {renderInlineLines(block.lines, blockIndex)}
            </p>
          );
        }
        if (block.kind === "list") {
          const ListTag = block.ordered ? "ol" : "ul";
          return (
            <ListTag key={blockIndex} className="research-markdown-list">
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>{parseInline(item)}</li>
              ))}
            </ListTag>
          );
        }
        return (
          <table key={blockIndex} className="research-markdown-table">
            <thead>
              <tr>
                {block.header.map((cell, cellIndex) => (
                  <th key={cellIndex}>{parseInline(cell)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex}>{parseInline(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        );
      })}
    </div>
  );
}
