import * as React from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { CodeBlock } from "@/components/apidocs/CodeBlock";
import { CopyButton } from "@/components/CopyButton";
import { Notice } from "@/components/server/Notice";
import { Prose } from "@/components/apidocs/Prose";
import { BracketsCurly, DownloadSimple, Key, Lightning, Warning } from "@/lib/icons";
import { cn } from "@/lib/utils";

/*
 * The path from "I have a panel" to "I have a script that works", in the order
 * somebody actually walks it: get a token, set it in the shell, make one call,
 * and know what the answers look like.
 *
 * Everything on this card is built from the panel the page is running on rather
 * than written out as an example. The base URL is this installation's own,
 * secret path included, so a reader who copies the first command is not
 * assembling it from three places on this page and one in their address bar -
 * which is where the mistakes are, and which is the whole reason a documented
 * API still needs a page like this one.
 */

export interface QuickStartProps {
  /** This panel's API root, absolute and without a trailing slash. */
  baseUrl: string;
  /** Takes the reader to the tab where a token is issued. */
  onIssueToken: () => void;
  /** Downloads the OpenAPI document. */
  onDownloadSpec: () => void;
  downloadingSpec: boolean;
  text: (key: string, fallback: string, vars?: Record<string, string | number>) => string;
}

export function QuickStart({
  baseUrl,
  onIssueToken,
  onDownloadSpec,
  downloadingSpec,
  text,
}: QuickStartProps): JSX.Element {
  const firstCall = [
    `export AWG_API=${baseUrl}`,
    "export AWG_TOKEN=awgp_...        # the secret, copied once when it was issued",
    "",
    'curl -s -H "Authorization: Bearer $AWG_TOKEN" \\',
    "     \"$AWG_API/clients\" | jq '.clients[].name'",
  ].join("\n");

  const createOne = [
    'curl -s -H "Authorization: Bearer $AWG_TOKEN" \\',
    "     -H 'Content-Type: application/json' \\",
    '     -d \'{"name":"laptop","quotaBytes":53687091200}\' \\',
    '     "$AWG_API/clients"',
    "",
    "# and the configuration that client has to import",
    'curl -s -H "Authorization: Bearer $AWG_TOKEN" \\',
    '     -o laptop.conf "$AWG_API/clients/laptop/config"',
  ].join("\n");

  const cookieCall = [
    "J=$(mktemp)",
    'curl -sc "$J" "$AWG_API/auth/session" >/dev/null',
    'CSRF=$(awk \'$6=="awgcsrftoken"{print $7}\' "$J")',
    "",
    'curl -sb "$J" -c "$J" -H "X-CSRFToken: $CSRF" \\',
    "     -H 'Content-Type: application/json' \\",
    '     -d \'{"username":"admin","password":"..."}\' \\',
    '     "$AWG_API/auth/login"',
  ].join("\n");

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2.5">
            <Lightning
              weight="duotone"
              className="h-4 w-4 shrink-0 text-muted-foreground"
              aria-hidden="true"
            />
            <CardTitle>{text("api.quickStart", "Your first request")}</CardTitle>
          </div>
          <CardDescription>
            {text(
              "api.quickStartHint",
              "Three steps, and the addresses below are this panel's own. Anything the panel can do to this server, a script holding a token can do too.",
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <Step
            number={1}
            title={text("api.stepToken", "Issue a token")}
            body={text(
              "api.stepTokenHint",
              "A token is the credential for something that is not a browser. It needs no cookie, no CSRF header and no second factor, and it can be revoked on its own without changing the password. The secret is shown once.",
            )}
            action={
              <Button size="sm" onClick={onIssueToken}>
                <Key aria-hidden="true" />
                {text("api.goToTokens", "Go to tokens")}
              </Button>
            }
          />

          <Step
            number={2}
            title={text("api.stepBase", "Point it at this panel")}
            body={text(
              "api.stepBaseHint",
              "Every route hangs off this address, secret path and all. Anything outside it answers a bare 404, so a scanner that has not been given this URL learns nothing.",
            )}
          >
            <div className="flex items-center gap-2 rounded-md border border-border bg-muted/60 px-3 py-2">
              <code className="min-w-0 flex-1 overflow-x-auto whitespace-pre font-mono text-xs">
                {baseUrl}
              </code>
              <CopyButton value={baseUrl} />
            </div>
          </Step>

          <Step
            number={3}
            title={text("api.stepCall", "Make the call")}
            body={text(
              "api.stepCallHint",
              "One header on every request, and that is the whole of the authentication.",
            )}
          >
            <CodeBlock code={firstCall} />
          </Step>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{text("api.oneClient", "Adding a client end to end")}</CardTitle>
          <CardDescription>
            {text(
              "api.oneClientHint",
              "The operation almost every script is written for. The peer is applied to the running interface as it is created, so no existing session drops, and the configuration comes back as a file the device can import.",
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <CodeBlock code={createOne} />
          <Notice icon={Warning} title={text("api.keysWarning", "That file is a key")}>
            <p>
              {text(
                "api.keysWarningBody",
                "A client configuration and its QR code both carry the client's private key, and the export archive carries every one of them. They are the only responses in this API that contain a secret at all - nothing else, listing or status, ever returns key material.",
              )}
            </p>
          </Notice>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{text("api.conventions", "What every answer looks like")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <dl className="divide-y divide-border">
            <Convention
              term={text("api.conventionCase", "Case and time")}
              detail={text(
                "api.conventionCaseBody",
                "JSON is camelCase in both directions. Times are RFC 3339 in UTC, unless the field name ends in a unit - `lastHandshake` is Unix seconds, because that is what the kernel reports.",
              )}
            />
            <Convention
              term={text("api.conventionTraffic", "Which way traffic runs")}
              detail={text(
                "api.conventionTrafficBody",
                "From the server's point of view, matching `awg show dump`: `rxBytes` is what the client **uploaded** and `txBytes` what it **downloaded**. Anything that shows these to a person has to label them that way round.",
              )}
            />
            <Convention
              term={text("api.conventionErrors", "Errors")}
              detail={text(
                "api.conventionErrorsBody",
                'Always `{"detail": "...", "errors": {"field": "..."}}`. `detail` is a finished sentence; `errors` names the field that was refused. Some carry a `code` as well, for a caller that has to branch on the reason rather than the wording.',
              )}
            />
            <Convention
              term={text("api.conventionNotConfigured", "A server with no tunnel")}
              detail={text(
                "api.conventionNotConfiguredBody",
                'Every route that reads the configuration answers `503` with `code: "not_configured"` where there is none - the panel installed before the VPN, a wrong interface name, a fresh container. It is an expected state rather than a fault, and `GET server/status` keeps answering `200` throughout, because that is what somebody calls to find out what is wrong.',
              )}
            />
            <Convention
              term={text("api.conventionCsrf", "CSRF, and why a token needs none")}
              detail={text(
                "api.conventionCsrfBody",
                "A mutating request made with the session cookie must carry `X-CSRFToken` matching the `awgcsrftoken` cookie. A request authenticated by a bearer token carries no cookie and needs no such token: nothing can make a browser attach an `Authorization` header it was not given.",
              )}
            />
          </dl>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{text("api.cookieWay", "Signing in instead")}</CardTitle>
          <CardDescription>
            {text(
              "api.cookieWayHint",
              "Only worth doing for the handful of routes a token may not touch - the password, the second factor, the session list, the tokens themselves, restoring a backup and clearing the activity log. Everything else is one header.",
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <CodeBlock code={cookieCall} />
          <p className="text-xs leading-relaxed text-muted-foreground">
            {text(
              "api.cookieWayNote",
              "An account with an authenticator answers the first attempt with 401 and detail totp_required; send the same request again with a totp field. Five failures lock the source address out for fifteen minutes.",
            )}
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex items-center gap-2.5">
            <BracketsCurly
              weight="duotone"
              className="h-4 w-4 shrink-0 text-muted-foreground"
              aria-hidden="true"
            />
            <CardTitle>{text("api.machineReadable", "For your tooling")}</CardTitle>
          </div>
          <CardDescription>
            {text(
              "api.machineReadableHint",
              "The whole API as an OpenAPI 3.1 document - which is what this page is rendered from, so the two cannot disagree. Import it into Postman, Insomnia or Bruno, or point a client generator at it; the address of this panel is already in it, so nothing needs editing first.",
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-center gap-2 rounded-md border border-border bg-muted/60 px-3 py-2">
            <code className="min-w-0 flex-1 overflow-x-auto whitespace-pre font-mono text-xs">
              {`${baseUrl}/openapi.json`}
            </code>
            <CopyButton value={`${baseUrl}/openapi.json`} />
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              size="sm"
              loading={downloadingSpec}
              disabled={downloadingSpec}
              onClick={onDownloadSpec}
            >
              <DownloadSimple aria-hidden="true" />
              {text("api.downloadSpec", "Download openapi.json")}
            </Button>
          </div>
          <p className="text-xs leading-relaxed text-muted-foreground">
            {text(
              "api.machineReadableNote",
              "It needs a credential like everything else, and a token may fetch it - a script being written against this panel is exactly who wants it.",
            )}
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Parts                                                                       */
/* -------------------------------------------------------------------------- */

interface StepProps {
  number: number;
  title: string;
  body: string;
  action?: React.ReactNode;
  children?: React.ReactNode;
}

function Step({ number, title, body, action, children }: StepProps): JSX.Element {
  return (
    <div className="flex gap-3">
      <span
        aria-hidden="true"
        className={cn(
          "flex h-6 w-6 shrink-0 items-center justify-center rounded-full",
          "bg-primary/10 text-xs font-semibold tabular-nums text-primary",
        )}
      >
        {number}
      </span>
      <div className="min-w-0 flex-1 space-y-2">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <p className="text-sm font-medium leading-tight">{title}</p>
          {action}
        </div>
        <Prose text={body} />
        {children}
      </div>
    </div>
  );
}

interface ConventionProps {
  term: string;
  detail: string;
}

function Convention({ term, detail }: ConventionProps): JSX.Element {
  return (
    <div className="grid gap-1 py-3 first:pt-0 last:pb-0 sm:grid-cols-[12rem_1fr] sm:gap-6">
      <dt className="text-sm font-medium">{term}</dt>
      <dd className="min-w-0">
        <Prose text={detail} />
      </dd>
    </div>
  );
}
