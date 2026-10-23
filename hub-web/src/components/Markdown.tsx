import { isValidElement, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import styles from "./Markdown.module.css";

/** The plain text under a React node (a code block's source). */
function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return "";
}

function CodeBlock({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard?.writeText(textOf(children)).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <div className={styles.codeWrap}>
      <button type="button" className={styles.copy} onClick={copy}>
        {copied ? "Copied" : "Copy"}
      </button>
      <pre className={styles.pre}>{children}</pre>
    </div>
  );
}

/** Untrusted markdown, sanitized (rehype-sanitize's default schema), with copy buttons on code blocks. */
export function Markdown({ source }: { source: string }) {
  return (
    <div className={styles.md}>
      <ReactMarkdown rehypePlugins={[rehypeSanitize]} components={{ pre: CodeBlock }}>
        {source}
      </ReactMarkdown>
    </div>
  );
}
