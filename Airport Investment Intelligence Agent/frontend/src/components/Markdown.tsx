import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { safeHttpUrl } from "../lib/url";
import { ExternalLink } from "./ExternalLink";

// Safe rendering of model text (spec section 9): raw HTML is skipped, images are dropped so no
// remote resource is loaded, and only absolute http(s) links survive. Each block gets dir="auto",
// so a Hebrew paragraph renders right-to-left next to an English one.
const components: Components = {
  a: ({ href, children }) => <ExternalLink href={href}>{children}</ExternalLink>,
  p: ({ children }) => <p dir="auto">{children}</p>,
  li: ({ children }) => <li dir="auto">{children}</li>,
  ul: ({ children }) => <ul dir="auto">{children}</ul>,
  ol: ({ children, start }) => (
    <ol dir="auto" start={start}>
      {children}
    </ol>
  ),
  blockquote: ({ children }) => <blockquote dir="auto">{children}</blockquote>,
  h1: ({ children }) => <h4 dir="auto">{children}</h4>,
  h2: ({ children }) => <h4 dir="auto">{children}</h4>,
  h3: ({ children }) => <h5 dir="auto">{children}</h5>,
  h4: ({ children }) => <h5 dir="auto">{children}</h5>,
  h5: ({ children }) => <h6 dir="auto">{children}</h6>,
  h6: ({ children }) => <h6 dir="auto">{children}</h6>,
  pre: ({ children }) => <pre dir="ltr">{children}</pre>,
  // GitHub-style tables from the model scroll horizontally like the result tables.
  table: ({ children }) => (
    <div className="table-scroll">
      <table dir="auto">{children}</table>
    </div>
  ),
};

const remarkPlugins = [remarkGfm];

const urlTransform = (url: string) => safeHttpUrl(url);

export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        skipHtml
        disallowedElements={["img"]}
        urlTransform={urlTransform}
        components={components}
        remarkPlugins={remarkPlugins}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
