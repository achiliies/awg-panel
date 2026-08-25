import * as React from "react";
import { useTranslation } from "react-i18next";

import { ApiTokensCard } from "@/components/settings/ApiTokensCard";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { CodeBlock } from "@/components/apidocs/CodeBlock";
import { EndpointReference } from "@/components/apidocs/EndpointReference";
import { ErrorState } from "@/components/ErrorState";
import { Notice } from "@/components/server/Notice";
import { PageHeader } from "@/components/PageHeader";
import { Prose } from "@/components/apidocs/Prose";
import { QuickStart } from "@/components/apidocs/QuickStart";
import { Recipes } from "@/components/apidocs/Recipes";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useToast } from "@/components/ui/toast";
import { BookOpenText, Code, Lightning, Prohibit, ShieldCheck, Signpost } from "@/lib/icons";
import { apiBase } from "@/api/client";
import { useApiSpec, useDownloadApiSpec, useSession } from "@/api/hooks";
import { useReveal } from "@/lib/reveal";

/*
 * The API, for whoever is writing against it.
 *
 * The panel is a client of its own API and nothing here is reserved for that
 * client, so anything on the other seven pages can be done by a script - which
 * was true before this page existed and was documented only in a Markdown file
 * in the repository, on a machine that is not the one being administered.
 *
 * Four tabs, in the order the work is done. Get a token; find the endpoint;
 * copy a shape that already works; and, first of all, make one request succeed,
 * because everything after that is easier than the first one.
 *
 * The reference is not written here. It is fetched from GET openapi.json, which
 * the panel builds from the catalog beside its own URL map, so a route cannot
 * change without this page changing with it - and so the same document a reader
 * sees rendered is the one their tooling imports. What this file adds is the
 * part a machine-readable document cannot carry: which credential to use, what
 * every answer looks like, and the half-dozen jobs people actually automate.
 *
 * The tokens card is the same component the Settings page shows under
 * Authentication, not a copy of it. A token is issued in the middle of writing
 * a script far more often than in the middle of administering a panel, so it
 * belongs on both pages; two implementations of it would be two dialogs that
 * drift apart, and one shared card cannot.
 */

export default function ApiDocs(): JSX.Element {
  const { t } = useTranslation();
  const { toast } = useToast();

  /** t() with an English original, so a key the catalog lacks never shows raw. */
  const text = React.useCallback(
    (key: string, fallback: string, vars?: Record<string, string | number>): string =>
      String(t(key, { defaultValue: fallback, ...vars })),
    [t],
  );

  const spec = useApiSpec();
  const session = useSession();
  const [tab, setTab] = React.useState("start");

  /*
   * "Go to tokens" on the first tab has two jobs, and changing the tab is only
   * one of them: the card it was pressed for sits below two others inside that
   * tab, so switching alone hands the reader a card about what a token may not
   * do and no sign that the thing they asked for is a screen further down.
   *
   * A count rather than a flag, because pressing the button, scrolling off and
   * pressing it again is the same request made twice, and a boolean already set
   * to true moves nothing the second time. Choosing the tab from the strip
   * clears it, so somebody who came to read the page is not dragged past it.
   */
  const [tokensAsked, setTokensAsked] = React.useState(0);
  const selectTab = React.useCallback((value: string) => {
    setTab(value);
    setTokensAsked(0);
  }, []);
  const goToTokens = React.useCallback(() => {
    setTab("auth");
    setTokensAsked((asked) => asked + 1);
  }, []);

  /*
   * The document names the panel it came from, which is the address a reader
   * should be sending requests to. Until it arrives - and if it never does -
   * the same address is assembled from the origin this page was served from and
   * the base path it is mounted under, which is what every other request the
   * SPA makes already uses. Neither is a guess.
   */
  const baseUrl = React.useMemo(() => {
    const fetched = spec.data?.serverUrl.trim();
    if (fetched) {
      return fetched.replace(/\/$/, "");
    }
    return new URL(apiBase, window.location.origin).toString().replace(/\/$/, "");
  }, [spec.data]);

  const download = useDownloadApiSpec();
  const downloadSpec = React.useCallback(() => {
    download.mutate(undefined, {
      onError: (error) =>
        toast({
          title: text("api.downloadFailed", "Could not download the document"),
          description: error.detail,
          variant: "destructive",
        }),
    });
  }, [download, text, toast]);

  return (
    <>
      <PageHeader
        title={text("api.title", "API")}
        description={text(
          "api.subtitle",
          "Everything this panel does, it does through this API - so anything on the other pages can be done by a script instead.",
        )}
        icon={Code}
        badge={
          session.data?.mock ? <Badge variant="warning">{t("about.mockMode")}</Badge> : undefined
        }
      />

      <Tabs value={tab} onValueChange={selectTab} className="flex-1">
        <TabsList>
          <TabsTrigger value="start">
            <Lightning aria-hidden="true" />
            {text("api.tabStart", "Getting started")}
          </TabsTrigger>
          <TabsTrigger value="auth">
            <ShieldCheck aria-hidden="true" />
            {text("api.tabAuth", "Authentication")}
          </TabsTrigger>
          <TabsTrigger value="endpoints">
            <Signpost aria-hidden="true" />
            {text("api.tabEndpoints", "Endpoints")}
          </TabsTrigger>
          <TabsTrigger value="recipes">
            <BookOpenText aria-hidden="true" />
            {text("api.tabRecipes", "Recipes")}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="start">
          <QuickStart
            baseUrl={baseUrl}
            text={text}
            onIssueToken={goToTokens}
            onDownloadSpec={downloadSpec}
            downloadingSpec={download.isPending}
          />
        </TabsContent>

        <TabsContent value="auth">
          <AuthenticationTab baseUrl={baseUrl} text={text} revealTokens={tokensAsked} />
        </TabsContent>

        <TabsContent value="endpoints">
          {spec.isPending ? (
            <LoadingReference />
          ) : spec.isError ? (
            <ErrorState
              error={spec.error}
              title={text("errors.loadFailed", "Could not load {{what}}", {
                what: text("api.reference", "the API reference").toLowerCase(),
              })}
              onRetry={() => void spec.refetch()}
            />
          ) : spec.data ? (
            <EndpointReference spec={spec.data} text={text} />
          ) : null}
        </TabsContent>

        <TabsContent value="recipes">
          <Recipes text={text} />
        </TabsContent>
      </Tabs>
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* Authentication                                                              */
/* -------------------------------------------------------------------------- */

interface AuthenticationTabProps {
  baseUrl: string;
  text: (key: string, fallback: string, vars?: Record<string, string | number>) => string;
  /** Counts the times "Go to tokens" was pressed; each one scrolls to the card. */
  revealTokens: number;
}

/**
 * How a request proves who it is, and the tokens that do it.
 *
 * The card doing the work is the Settings page's, unchanged. What is around it
 * is what a script author needs and an admin editing their own password does
 * not: which of the two credentials to use, what a token may not do and why the
 * line is drawn there, and what the activity log will say about work the token
 * does once it is loose in a cron job.
 */
function AuthenticationTab({ baseUrl, text, revealTokens }: AuthenticationTabProps): JSX.Element {
  const tokensRef = useReveal<HTMLDivElement>(revealTokens);

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>{text("api.twoWays", "Two ways in, and which to use")}</CardTitle>
          <CardDescription>
            {text(
              "api.twoWaysHint",
              "The panel's own pages sign in with a cookie. Anything that is not a browser should send a token.",
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Method
              title={text("api.bearer", "A bearer token")}
              recommended
              body={text(
                "api.bearerBody",
                "One header on every request, and that is the whole of the authentication. No cookie jar, no CSRF token, no second factor to script around, and it can be revoked on its own without changing the password anybody signs in with.",
              )}
              code={`Authorization: Bearer awgp_...`}
            />
            <Method
              title={text("api.cookie", "A session cookie")}
              body={text(
                "api.cookieBody",
                "What `POST auth/login` hands back, and what this browser is using right now. Every mutating request made with it must also carry `X-CSRFToken` matching the `awgcsrftoken` cookie. Worth the trouble only for the routes a token may not touch.",
              )}
              code={`Cookie: awgsessionid=...\nX-CSRFToken: ...`}
            />
          </div>

          <Notice icon={Prohibit} title={text("api.tokenLimits", "What a token may not do")}>
            <p>
              {text(
                "api.tokenLimitsBody",
                "A token can do anything this panel can, except touch the credentials behind it. Issuing or revoking a token, reading the token list, changing the username or password, turning off the second factor, ending a session, and restoring a backup (the archive carries the password hash and the token table) all answer 403. Otherwise a leaked token could mint itself a permanent replacement, and revoking it would be theatre.",
              )}
            </p>
            {/* Prose rather than a bare <p> like the one above it: this one
                names a route, and a backtick that is not rendered as code is a
                backtick the reader has to read past. */}
            <Prose
              className="space-y-1.5"
              text={text(
                "api.tokenLimitsBackup",
                "`GET backup` is the one gap. The archive holds the session table, and a session key is the browser's cookie itself, so whoever downloads one can sign in as the account - past every 403 above. It stays open because a nightly copy off the box is worth having, and that file already carries every private key on the server. Revoking the token later does not take the archive back: treat a token that has fetched one like the password.",
              )}
            />
          </Notice>

          <div className="space-y-1.5">
            <p className="text-sm font-medium">
              {text("api.checkToken", "Checking a token from a script")}
            </p>
            <Prose
              text={text(
                "api.checkTokenBody",
                "The token list is closed to tokens, so a script cannot read the row belonging to the secret it holds. What it can do is ask who it is: `GET auth/session` names the calling token and says when it runs out, which is the check to make before a long job rather than after it fails.",
              )}
            />
            <CodeBlock
              code={[
                `curl -s -H "Authorization: Bearer $AWG_TOKEN" \\`,
                `     "${baseUrl}/auth/session" | jq .token`,
                "",
                "# {",
                '#   "name": "nightly backup",',
                '#   "expiresAt": "2026-09-04T09:14:02Z",',
                '#   "renewOnUse": true',
                "# }",
              ].join("\n")}
            />
          </div>
        </CardContent>
      </Card>

      {/* The same card the Settings page shows, so a token issued on either
          page is one token and one row, however it was reached. The wrapper is
          what the first tab's button scrolls to; scroll-mt keeps the card's
          heading clear of the sticky top bar, which "start" would put it under. */}
      <div ref={tokensRef} className="scroll-mt-20">
        <ApiTokensCard text={text} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{text("api.whatTheLogSays", "What the activity log records")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <Prose
            text={text(
              "api.whatTheLogSaysBody",
              'Everything a token does is recorded like anything else, with one difference worth knowing before you name one. The actor on those rows is `api:` followed by the token\'s name - every token on a panel authenticates as the same single account, so recording that account would make the log say `admin` for work nobody was present for.\n\nSo the name is load-bearing: "nightly backup" in the token list is "nightly backup" in the log a month later. A refused token records nothing at all, deliberately - that path can be driven as often as an outsider likes, and an audit trail a stranger can grow at will is not one.',
            )}
          />
          <CodeBlock
            label={text("api.exampleRow", "One row")}
            code={[
              "{",
              '  "kind": "client.updated",',
              '  "actor": "api:nightly backup",',
              '  "actorIp": "203.0.113.10",',
              '  "target": "phone",',
              '  "detail": { "fields": ["quotaBytes"] }',
              "}",
            ].join("\n")}
          />
        </CardContent>
      </Card>
    </div>
  );
}

interface MethodProps {
  title: string;
  body: string;
  code: string;
  recommended?: boolean;
}

function Method({ title, body, code, recommended = false }: MethodProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <div className="space-y-2 rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-sm font-medium leading-tight">{title}</p>
        {recommended ? (
          <Badge variant="success" size="sm">
            {t("api.forScripts", { defaultValue: "For scripts" })}
          </Badge>
        ) : null}
      </div>
      <Prose text={body} />
      <CodeBlock code={code} />
    </div>
  );
}

/** The reference loads as a list of cards, so the tab does not jump when it lands. */
function LoadingReference(): JSX.Element {
  return (
    <div className="space-y-4">
      <Skeleton className="h-9 w-full" />
      {[0, 1].map((card) => (
        <div key={card} className="space-y-3 rounded-lg border border-border p-5">
          <Skeleton className="h-4 w-40" />
          <Skeleton className="h-3 w-full max-w-xl" />
          <div className="space-y-2 pt-2">
            {[0, 1, 2].map((row) => (
              <Skeleton key={row} className="h-12 w-full" />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
