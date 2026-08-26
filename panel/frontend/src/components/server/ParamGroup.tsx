import * as React from "react";
import { type Icon } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { GroupHeader } from "@/components/server/GroupHeader";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import type { ParamGroup as ParamGroupId } from "@/api/types";

/*
 * One card per group of parameters.
 *
 * The group is the unit an admin thinks in - "the junk packet settings", not
 * "Jc, Jmin and Jmax" - so the card leads with a sentence about what the whole
 * group does before showing a single field. Title and sentence come from the UI
 * catalog by group id, so a group added to the API arrives here with its own
 * words and no change to this file.
 *
 * The head of the card is GroupHeader, which the Obfuscation card beside it
 * uses as well: two cards on one page that lay their titles and buttons out
 * differently is a difference an admin reads as a mistake, and it was one.
 *
 * It could also collapse, carry a chip on its heading, hold a note above the
 * fields and take buttons of its own, all for the advanced group: a card that
 * started closed because opening it was a trap, said "Beta feature" beside its
 * title, and had a generator and a Clear in its header. That group is a section
 * of the Obfuscation card now that the clients have caught up, and the machinery
 * went with it rather than staying as an unreachable second way to draw a card.
 */

export interface ParamGroupProps {
  group: ParamGroupId;
  icon?: Icon;
  /** Unsaved edits in this group, badged beside the heading. */
  changedCount?: number;
  /** Fields the server or the form rejected. */
  errorCount?: number;
  children: React.ReactNode;
}

export function ParamGroup({
  group,
  icon: Icon,
  changedCount = 0,
  errorCount = 0,
  children,
}: ParamGroupProps): JSX.Element {
  const { t } = useTranslation();

  return (
    <Card>
      <GroupHeader
        icon={Icon}
        title={t(`server.${group}`)}
        description={t(`server.${group}Hint`)}
        actions={
          errorCount > 0 || changedCount > 0 ? (
            <>
              {errorCount > 0 ? (
                <Badge variant="destructive">
                  {t("server.groupErrors", { count: errorCount, defaultValue: "{{count}} to fix" })}
                </Badge>
              ) : null}
              {changedCount > 0 ? (
                <Badge>
                  {t("server.groupChanged", {
                    count: changedCount,
                    defaultValue: "{{count}} changed",
                  })}
                </Badge>
              ) : null}
            </>
          ) : null
        }
      />

      <CardContent>
        <div className="grid gap-x-6 gap-y-6 sm:grid-cols-2">{children}</div>
      </CardContent>
    </Card>
  );
}
