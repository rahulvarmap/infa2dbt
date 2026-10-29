type SyntaxKind = 'sql' | 'yaml' | 'json' | 'text';
type HighlightToken = { text: string; kind?: string };

const SQL_TOKENS =
  /({{[\s\S]*?}})|(--.*$)|(\/\*.*?\*\/)|('(?:''|[^'])*')|("(?:\\.|[^"])*")|(\b(?:with|select|distinct|from|where|and|or|not|in|is|null|as|case|when|then|else|end|cast|join|left|right|inner|outer|on|group|by|order|having|union|all|over|partition|qualify|limit|true|false)\b)|(\b[a-z_][\w$]*(?=\s*\())|(\b\d+(?:\.\d+)?\b)/gim;
const YAML_TOKENS =
  /({{[\s\S]*?}})|(^\s*#.*$)|("(?:\\.|[^"])*")|('(?:''|[^'])*')|(\b[\w.-]+(?=\s*:))|(\b(?:true|false|null)\b)|(\b\d+(?:\.\d+)?\b)/gim;
const JSON_TOKENS =
  /("(?:\\.|[^"])*"(?=\s*:))|("(?:\\.|[^"])*")|(\b(?:true|false|null)\b)|(-?\b\d+(?:\.\d+)?\b)/gim;

export function syntaxForFile(path: string): SyntaxKind {
  if (path.endsWith('.sql')) return 'sql';
  if (path.endsWith('.yml') || path.endsWith('.yaml')) return 'yaml';
  if (path.endsWith('.json')) return 'json';
  return 'text';
}

function highlightLine(line: string, syntax: SyntaxKind): HighlightToken[] {
  if (syntax === 'text') return [{ text: line }];
  const regex = syntax === 'sql'
    ? SQL_TOKENS
    : syntax === 'yaml'
      ? YAML_TOKENS
      : JSON_TOKENS;
  const tokens: HighlightToken[] = [];
  let cursor = 0;
  regex.lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(line)) !== null) {
    if (match.index > cursor) {
      tokens.push({ text: line.slice(cursor, match.index) });
    }
    const groups = match.slice(1);
    const groupIndex = groups.findIndex((value) => value !== undefined);
    const kind = syntax === 'sql'
      ? ['jinja', 'comment', 'comment', 'string', 'string', 'keyword', 'function', 'number'][groupIndex]
      : syntax === 'yaml'
        ? ['jinja', 'comment', 'string', 'string', 'key', 'keyword', 'number'][groupIndex]
        : ['key', 'string', 'keyword', 'number'][groupIndex];
    tokens.push({ text: match[0], kind });
    cursor = match.index + match[0].length;
  }
  if (cursor < line.length) tokens.push({ text: line.slice(cursor) });
  return tokens.length ? tokens : [{ text: line }];
}

export function DbtCodeViewer({ path, content }: { path: string; content: string }) {
  const syntax = syntaxForFile(path);
  const lines = content.split(/\r?\n/);

  return (
    <div
      className="dbt-code-scroll"
      role="region"
      aria-label={`${path} contents; scroll horizontally for long lines`}
      tabIndex={0}
      data-testid="dbt-code-viewer"
      data-syntax={syntax}
    >
      <pre className="dbt-code"><code>
        {lines.map((line, index) => (
          <span className="dbt-code-line" key={index}>
            <span className="dbt-code-line-number" aria-hidden="true">{index + 1}</span>
            <span className="dbt-code-line-text">
              {highlightLine(line, syntax).map((token, tokenIndex) => (
                <span
                  className={token.kind ? `dbt-token-${token.kind}` : undefined}
                  key={`${index}-${tokenIndex}`}
                >
                  {token.text}
                </span>
              ))}
            </span>
          </span>
        ))}
      </code></pre>
    </div>
  );
}
