import * as React from "react";
import { useTranslation } from "react-i18next";

import { CaretDown, Globe, LockKey } from "@/lib/icons";
import { Badge } from "@/components/ui/badge";
import { CodeBlock } from "@/components/apidocs/CodeBlock";
import { Prose } from "@/components/apidocs/Prose";
import { cn } from "@/lib/utils";
import { operationText } from "@/lib/apiText";
import type { ApiOperation } from "@/api/types";

/*
 * One endpoint, closed until somebody wants it.
 *
 * Closed, a row is the three things that answer "is this the one I need":
 * the method, the path, and one line saying what it does. Open, it is
 * everything needed to make the call without leaving the page - the parameters,
 * a real request body, the answer that comes back, the failures worth handling,
 * and a curl command with this panel's own address already in it.
 *
 * The command is the point of the whole page, so it is built from the operation
 * rather than written out beside it: the URL is the one the browser is on, the
 * method and body come from the document, and the credential shown is the one
 * this route actually accepts. A reader can copy it, paste it, and have it work
 * - which is a different experience from being shown the shape of a request and
 * left to assemble it.
 */

export interface OperationRowProps {
  operation: ApiOperation;
  /** The API root, absolute and ending without a slash, for the curl example. */
  baseUrl: string;
  /** Opened by a search that matched inside it, rather than by a click. */
  forceOpen?: boolean;
  text: (key: string, fallback: string, vars?: Record<string, string | number>) => string;
}

/** What a method does, coloured by how much it does. */
const METHOD_TONE: Readonly<Record<string, string>> = {
  GET: "border-info/40 bg-info/10 text-info",
  POST: "border-success/40 bg-success/10 text-success",
  PUT: "border-warning/50 bg-warning/10 text-warning",
  DELETE: "border-destructive/40 bg-destructive/10 text-destructive",
};

export function OperationRow({
  operation,
  baseUrl,
  forceOpen = false,
  text,
}: OperationRowProps): JSX.Element {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const words = operationText(t, operation);
  const bodyId = `api-op-${operation.id}`;
  const expanded = open || forceOpen;

  return (
    <li className="scroll-mt-20" id={operation.id}>
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        aria-expanded={expanded}
        aria-controls={bodyId}
        className={cn(
          "flex w-full items-start gap-3 p-4 text-start transition-colors",
          "hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2",
          "focus-visible:ring-inset focus-visible:ring-ring",
        )}
      >
        <span
          className={cn(
            "mt-0.5 w-16 shrink-0 rounded border px-1.5 py-0.5 text-center",
            "font-mono text-[11px] font-semibold leading-tight",
            METHOD_TONE[operation.method] ?? "border-border bg-muted text-muted-foreground",
          )}
        >
          {operation.method}
        </span>

        <span className="min-w-0 flex-1 space-y-1">
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <code className="break-all font-mono text-sm font-medium">{operation.path}</code>
            <AuthBadge auth={operation.auth} text={text} />
          </span>
          <span className="block text-sm leading-relaxed text-muted-foreground">
            {words.summary}
          </span>
        </span>

        <CaretDown
          aria-hidden="true"
          className={cn(
            "mt-1 h-4 w-4 shrink-0 text-muted-foreground transition-transform",
            expanded && "rotate-180",
          )}
        />
      </button>

      {expanded ? (
        <div id={bodyId} className="space-y-4 border-t border-border bg-muted/20 p-4">
          <Prose text={words.description} />

          {operation.params.length > 0 ? <Parameters operation={operation} text={text} /> : null}

          {operation.request ? (
            <CodeBlock
              label={text("api.request", "Request")}
              note={
                operation.requestRequired ? null : (
                  <Badge variant="muted" size="sm">
                    {text("api.optional", "optional")}
                  </Badge>
                )
              }
              code={operation.request}
            />
          ) : null}
          {operation.requestNote ? (
            <p className="text-xs leading-relaxed text-muted-foreground">{operation.requestNote}</p>
          ) : null}

          <Answer operation={operation} text={text} />

          {operation.statuses.length > 0 ? (
            <div className="space-y-1.5">
              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                {text("api.alsoAnswers", "Also answers")}
              </p>
              <dl className="space-y-1">
                {operation.statuses.map((status) => (
                  <div key={status.code} className="flex gap-2.5 text-sm">
                    <dt className="w-9 shrink-0 font-mono text-xs leading-relaxed text-muted-foreground">
                      {status.code}
                    </dt>
                    <dd className="min-w-0 flex-1">
                      <Prose text={status.description} className="space-y-1" />
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
          ) : null}

          <CodeBlock label="curl" code={curlFor(operation, baseUrl)} />
        </div>
      ) : null}
    </li>
  );
}

/* -------------------------------------------------------------------------- */
/* Parts                                                                       */
/* -------------------------------------------------------------------------- */

interface AuthBadgeProps {
  auth: ApiOperation["auth"];
  text: OperationRowProps["text"];
}

/**
 * Which credential opens this route, which is the one thing about an endpoint a
 * script author has to know before writing anything.
 *
 * Only two of the three states get a chip. "A token works here" is true of
 * nearly every route, and a chip on nearly every row is a chip nobody reads -
 * so the common case is silent and the two exceptions are marked: the routes a
 * token is refused on, and the two that need no credential at all.
 */
function AuthBadge({ auth, text }: AuthBadgeProps): JSX.Element | null {
  if (auth === "session") {
    return (
      <Badge variant="caution" size="sm">
        <LockKey weight="bold" aria-hidden="true" />
        {text("api.sessionOnly", "Sign-in only")}
      </Badge>
    );
  }
  if (auth === "none") {
    return (
      <Badge variant="muted" size="sm">
        <Globe weight="bold" aria-hidden="true" />
        {text("api.noAuth", "No credential")}
      </Badge>
    );
  }
  return null;
}

interface ParametersProps {
  operation: ApiOperation;
  text: OperationRowProps["text"];
}

function Parameters({ operation, text }: ParametersProps): JSX.Element {
  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {text("api.parameters", "Parameters")}
      </p>
      <ul className="divide-y divide-border rounded-md border border-border bg-background">
        {operation.params.map((param) => (
          <li key={`${param.where}-${param.name}`} className="space-y-1 p-2.5">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <code className="font-mono text-xs font-medium">{param.name}</code>
              <Badge variant="muted" size="sm">
                {param.where === "path"
                  ? text("api.inPath", "in the path")
                  : text("api.inQuery", "query")}
              </Badge>
              {param.required ? (
                <span className="text-[11px] font-medium text-destructive">
                  {text("api.required", "required")}
                </span>
              ) : param.fallback !== "" ? (
                <span className="text-[11px] text-muted-foreground">
                  {text("api.defaultsTo", "defaults to {{value}}", { value: param.fallback })}
                </span>
              ) : null}
            </div>
            <p className="text-xs leading-relaxed text-muted-foreground">{param.description}</p>
            {param.values.length > 0 ? (
              <div className="flex flex-wrap gap-1">
                {param.values.map((value) => (
                  <code
                    key={value}
                    className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground"
                  >
                    {value}
                  </code>
                ))}
              </div>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

interface AnswerProps {
  operation: ApiOperation;
  text: OperationRowProps["text"];
}

/** What comes back: the status, what it means, and the body when there is one. */
function Answer({ operation, text }: AnswerProps): JSX.Element {
  const status = (
    <Badge variant="muted" size="sm">
      <code className="font-mono">{operation.successCode}</code>
      {operation.responseType && operation.responseType !== "application/json"
        ? ` · ${operation.responseType}`
        : null}
    </Badge>
  );

  if (!operation.response) {
    return (
      <div className="space-y-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {text("api.answers", "Answers")}
          </span>
          {status}
        </div>
        <p className="text-sm leading-relaxed text-muted-foreground">
          {operation.successNote || text("api.noBody", "No body.")}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      <CodeBlock label={text("api.answers", "Answers")} note={status} code={operation.response} />
      {operation.successNote && operation.successNote !== DEFAULT_SUCCESS ? (
        <p className="text-xs leading-relaxed text-muted-foreground">{operation.successNote}</p>
      ) : null}
    </div>
  );
}

/** The catalog's wording for an ordinary 200, which the status chip already says. */
const DEFAULT_SUCCESS = "The current state.";

/* -------------------------------------------------------------------------- */
/* The command                                                                 */
/* -------------------------------------------------------------------------- */

/** A plausible value for a path parameter, so the command reads as a real one. */
const SAMPLE: Readonly<Record<string, string>> = {
  name: "phone",
  id: "9f1c6b2a-2d4e-4d7f-8a1b-3c5e7f9a0b2d",
};

/** What a downloaded body should be saved as, by media type. */
const SAVE_AS: Readonly<Record<string, string>> = {
  "text/plain": "phone.conf",
  "image/png": "phone.png",
  "application/zip": "clients.zip",
  "application/gzip": "awg-panel-backup.tar.gz",
};

/**
 * The whole request as one shell command, against this panel.
 *
 * Two credentials, because the routes need two. Nearly everything takes the
 * bearer header, which is one line and no state; the credential routes take a
 * cookie, and there the command shows the jar and the CSRF header, because a
 * copied command that quietly omits them fails with a 403 that says nothing
 * about what is missing.
 *
 * Line continuations rather than one long line: a command that is four screens
 * wide cannot be read before it is run, and this is a page for reading.
 */
function curlFor(operation: ApiOperation, baseUrl: string): string {
  const path = operation.path.replace(/\{(\w+)\}/g, (whole, key: string) => SAMPLE[key] ?? whole);
  const url = `${baseUrl}/${path}`;
  const parts = ["curl -s"];

  if (operation.auth === "session") {
    parts.push('-b cookies.txt -H "X-CSRFToken: $CSRF"');
  } else if (operation.auth !== "none") {
    parts.push('-H "Authorization: Bearer $AWG_TOKEN"');
  }

  if (operation.method !== "GET") {
    parts.push(`-X ${operation.method}`);
  }
  if (operation.request && operation.requestType === "multipart/form-data") {
    // A file, not JSON. `-d` here would post the word "file=@..." as a body and
    // the panel would refuse it, which is a worse thing to hand somebody than
    // no command at all. curl sets the multipart header itself.
    parts.push(`-F "${operation.request}"`);
  } else if (operation.request) {
    // Compact, because the point of the block above is the shape of the body
    // and the point of this one is a line that can be pasted.
    parts.push("-H 'Content-Type: application/json'");
    parts.push(`-d '${compactJson(operation.request)}'`);
  }

  const saveAs = SAVE_AS[operation.responseType];
  if (saveAs && operation.method === "GET") {
    parts.push(`-o ${saveAs}`);
  }

  parts.push(`"${url}"`);
  return wrap(parts);
}

function compactJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text));
  } catch {
    // A body that is not JSON - the multipart upload's placeholder - is quoted
    // as it stands rather than dropped.
    return text;
  }
}

/** Join the pieces onto lines of about eighty columns, backslash-continued. */
function wrap(parts: readonly string[]): string {
  const lines: string[] = [];
  let line = "";
  for (const part of parts) {
    if (line === "") {
      line = part;
    } else if (line.length + part.length + 1 <= 78) {
      line = `${line} ${part}`;
    } else {
      lines.push(line);
      line = `  ${part}`;
    }
  }
  lines.push(line);
  return lines.join(" \\\n");
}
