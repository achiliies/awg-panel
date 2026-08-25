import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CodeBlock } from "@/components/apidocs/CodeBlock";
import { Prose } from "@/components/apidocs/Prose";
import type { Icon } from "@/lib/icons";
import { Archive, ArrowsClockwise, Broom, Plus, Speedometer, WifiHigh } from "@/lib/icons";

/*
 * The jobs people actually automate, written out in full.
 *
 * Not a second reference - the reference is the tab beside this one. These are
 * the shapes that are hard to arrive at from a list of endpoints, and each one
 * is here because getting it wrong has a cost worth avoiding: a loop of
 * deletions where one sweep would do, a poll of the whole client list every two
 * seconds when a stamp would have said nothing changed, a monitoring check that
 * reports a healthy panel while the tunnel is down.
 *
 * Every recipe is plain `curl` and `jq`, and every one assumes the two
 * variables the quick start sets. Nothing here needs a library, because the
 * point is that the thing being demonstrated is the API and not a client for it.
 */

export interface RecipesProps {
  text: (key: string, fallback: string, vars?: Record<string, string | number>) => string;
}

interface Recipe {
  id: string;
  icon: Icon;
  titleKey: string;
  title: string;
  bodyKey: string;
  body: string;
  code: string;
}

export function Recipes({ text }: RecipesProps): JSX.Element {
  const recipes: readonly Recipe[] = [
    {
      id: "provision",
      icon: Plus,
      titleKey: "api.recipeProvision",
      title: "Provision a batch of clients",
      bodyKey: "api.recipeProvisionBody",
      body:
        "One request per client, and that is the honest shape: each one is a config rewrite and an `awg syncconf`, so a hundred clients is a hundred of each. It is fast enough for the numbers anybody provisions by hand and it is the only way to give each client its own options.\n\n" +
        "A name already taken answers `409` rather than overwriting anybody, which is what makes the loop safe to re-run: it skips what exists and adds what does not.",
      code: [
        "while read -r name; do",
        "  code=$(curl -s -o \"$name.json\" -w '%{http_code}' \\",
        '    -H "Authorization: Bearer $AWG_TOKEN" \\',
        "    -H 'Content-Type: application/json' \\",
        '    -d "{\\"name\\":\\"$name\\",\\"quotaBytes\\":53687091200}" \\',
        '    "$AWG_API/clients")',
        '  case "$code" in',
        '    201) curl -s -H "Authorization: Bearer $AWG_TOKEN" \\',
        '           -o "$name.conf" "$AWG_API/clients/$name/config" ;;',
        '    409) echo "$name: already there, left alone" ;;',
        '    *)   echo "$name: $code $(jq -r .detail "$name.json")" ;;',
        "  esac",
        "done < names.txt",
      ].join("\n"),
    },
    {
      id: "sweep",
      icon: Broom,
      titleKey: "api.recipeSweep",
      title: "Sweep the clients nobody is using",
      bodyKey: "api.recipeSweepBody",
      body:
        "One request, not a loop. Every client in the sweep leaves in a single config rewrite and a single `awg syncconf`, where thirty deletions are thirty of each and thirty windows in which the configuration on disk is halfway through the job.\n\n" +
        "Size it first if the number matters: `GET clients` carries `expiredCount`, `disabledCount` and `expiredOrDisabledCount`, counted over the whole server by the same rules the sweep uses. A lapsed client an admin deliberately switched back on is left alone by both.",
      code: [
        "# what it would take, before taking it",
        'curl -s -H "Authorization: Bearer $AWG_TOKEN" "$AWG_API/clients?pageSize=1" \\',
        "  | jq '{expired: .expiredCount, disabled: .disabledCount,",
        "         both: .expiredOrDisabledCount}'",
        "",
        'curl -s -H "Authorization: Bearer $AWG_TOKEN" \\',
        "     -H 'Content-Type: application/json' \\",
        '     -d \'{"expired":true,"disabled":false}\' \\',
        "     \"$AWG_API/clients/bulk-remove\" | jq '.count, .removed[]'",
      ].join("\n"),
    },
    {
      id: "backup",
      icon: Archive,
      titleKey: "api.recipeBackup",
      title: "Copy a backup off the box every night",
      bodyKey: "api.recipeBackupBody",
      body:
        "The one privileged thing a token is deliberately allowed to do. Restoring an archive is closed to tokens, because it replaces the database the password hash and the tokens themselves live in - but copying one off is exactly what a nightly job is for.\n\n" +
        "**The archive contains the server's private key, every client's, and the panel's own database.** That database holds the session table, so whatever holds an archive can sign in as the account as well. Wherever it is written has to be as protected as the server is, and a token that fetches one is the password rather than a revocable stand-in for it.",
      code: [
        "#!/bin/sh",
        "set -eu",
        "stamp=$(date -u +%Y%m%d)",
        'out="/var/backups/awg-panel-$stamp.tar.gz"',
        "",
        'curl -sf -H "Authorization: Bearer $AWG_TOKEN" -o "$out" "$AWG_API/backup"',
        'chmod 600 "$out"',
        "",
        "# keep a fortnight",
        "find /var/backups -name 'awg-panel-*.tar.gz' -mtime +14 -delete",
      ].join("\n"),
    },
    {
      id: "watch",
      icon: ArrowsClockwise,
      titleKey: "api.recipeWatch",
      title: "Notice a change without polling the list",
      bodyKey: "api.recipeWatchBody",
      body:
        "`GET stats/live` reads a file the collector wrote, so polling it every couple of seconds costs nothing - and it carries `confStamp`, which changes whenever the server configuration is rewritten: a client added, removed, or switched off by the collector for its data limit.\n\n" +
        "So the expensive call is made only when something moved. `?peers=` narrows the peer table, and an empty one asks for none of it, which is what a caller reading only the totals should send: on a server with a few thousand clients the difference is most of a megabyte every two seconds.\n\n" +
        "The stamp is **opaque**. Compare it with the last one seen and do not parse it; the contract is that it differs after a change and does not otherwise.",
      code: [
        "last=",
        "while sleep 2; do",
        '  live=$(curl -s -H "Authorization: Bearer $AWG_TOKEN" "$AWG_API/stats/live?peers=")',
        '  stamp=$(printf %s "$live" | jq -r .confStamp)',
        '  [ "$stamp" = "$last" ] && continue',
        "  last=$stamp",
        "",
        '  curl -s -H "Authorization: Bearer $AWG_TOKEN" "$AWG_API/clients?pageSize=0" \\',
        "    | jq -r '.clients[] | select(.enabled | not)",
        '              | "\\(.name) off: \\(.disabledReason)"\'',
        "done",
      ].join("\n"),
    },
    {
      id: "monitor",
      icon: WifiHigh,
      titleKey: "api.recipeMonitor",
      title: "Check that the tunnel is actually up",
      bodyKey: "api.recipeMonitorBody",
      body:
        "`GET health` needs no credential and says only that the panel is answering, which is what a container healthcheck wants and not what a monitoring system does: the web service can be perfectly healthy over a tunnel that is down.\n\n" +
        "`GET server/status` is the one that knows, and it answers `200` even when everything it describes is broken - which is the point, because a route that failed when the tunnel failed would give a monitor nothing to read. Watch `ts` on the live blob as well: a blob that has stopped moving is a collector that has stopped, and every rate on it is then a stale number rather than a zero.",
      code: [
        'status=$(curl -s -H "Authorization: Bearer $AWG_TOKEN" "$AWG_API/server/status")',
        "printf %s \"$status\" | jq -e '.ifaceUp and .moduleLoaded and .listening' >/dev/null \\",
        "  || { printf %s \"$status\" | jq -r '.warnings[]'; exit 2; }",
        "",
        "# and that the figures on the dashboard are still being written",
        'age=$(( $(date +%s) - $(curl -s -H "Authorization: Bearer $AWG_TOKEN" \\',
        '        "$AWG_API/stats/live?peers=" | jq .ts) ))',
        '[ "$age" -lt 30 ] || { echo "collector is $age s behind"; exit 2; }',
      ].join("\n"),
    },
    {
      id: "limit",
      icon: Speedometer,
      titleKey: "api.recipeLimit",
      title: "Give everybody the same speed limit",
      bodyKey: "api.recipeLimitBody",
      body:
        'Bits per second, `0` for no limit. Both fields are required rather than defaulted, because a body that lost half of itself in transit must not read as "and clear everybody\'s upload limit while you are here".\n\n' +
        "It overwrites and there is no undo: whatever any client had is replaced. `changed` counts the rows that actually moved, so running it twice answers `0` the second time rather than the size of the server. `applied: false` with a `reason` means the numbers were stored but the kernel could not be brought into line, and the collector applies them when it can.",
      code: [
        'curl -s -H "Authorization: Bearer $AWG_TOKEN" \\',
        "     -H 'Content-Type: application/json' \\",
        '     -d \'{"downBps":20000000,"upBps":0}\' \\',
        '     "$AWG_API/clients/bulk-limit" | jq',
        "",
        "# one client instead of all of them",
        'curl -s -X PUT -H "Authorization: Bearer $AWG_TOKEN" \\',
        "     -H 'Content-Type: application/json' \\",
        "     -d '{\"downBps\":100000000}' \\",
        "     \"$AWG_API/clients/phone\" | jq '{name, downBps, upBps}'",
      ].join("\n"),
    },
  ];

  return (
    <div className="space-y-4">
      {recipes.map((recipe) => {
        const Glyph = recipe.icon;
        return (
          <Card key={recipe.id}>
            <CardHeader>
              <div className="flex items-center gap-2.5">
                <Glyph
                  weight="duotone"
                  className="h-4 w-4 shrink-0 text-muted-foreground"
                  aria-hidden="true"
                />
                <CardTitle>{text(recipe.titleKey, recipe.title)}</CardTitle>
              </div>
              {/* Prose rather than CardDescription: these run to two or three
                  paragraphs with code in them, and CardDescription is a <p>. */}
              <Prose text={text(recipe.bodyKey, recipe.body)} />
            </CardHeader>
            <CardContent>
              <CodeBlock code={recipe.code} />
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
