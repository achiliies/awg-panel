import * as React from "react";
import { Power, Warning } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { Notice } from "@/components/server/Notice";
import { RestartButton } from "@/components/RestartButton";
import { Button } from "@/components/ui/button";
import { api } from "@/api/client";
import type { ServerSaveResult } from "@/api/types";

/*
 * What the last save has to say for itself.
 *
 * This is the top of the page, and Save is on the bottom edge of the window
 * with a card per parameter group in between - so the block is scrolled to when
 * it arrives rather than left for the operator to find. `scroll-mt` clears the
 * sticky top bar.
 *
 * The restart banner is the exception rather than the rule: the save restarts
 * the interface itself, so seeing it means the tunnel was already down or the
 * restart failed, and the warnings underneath say which.
 */

export interface SaveNoticesProps {
  /** The outcome, already filtered to one that has a banner to draw. */
  notes: ServerSaveResult;
  onDismiss: () => void;
}

export const SaveNotices = React.forwardRef<HTMLDivElement, SaveNoticesProps>(function SaveNotices(
  { notes, onDismiss },
  ref,
): JSX.Element {
  const { t } = useTranslation();

  return (
    <div ref={ref} className="scroll-mt-20 space-y-4">
      {notes.mustReimport ? (
        <Notice
          icon={Warning}
          title={String(t("server.mustReimport"))}
          action={
            <>
              <Button asChild size="sm" variant="outline">
                <a href={api.blobUrl("clients/export.zip")} download>
                  {t("clients.exportAll")}
                </a>
              </Button>
              <Button size="sm" variant="ghost" onClick={onDismiss}>
                {t("common.dismiss")}
              </Button>
            </>
          }
        >
          <p>{t("server.mustReimportBody")}</p>
        </Notice>
      ) : null}

      {notes.needsRestart && !notes.applied ? (
        <Notice
          icon={Power}
          tone="info"
          title={String(t("server.restartPending"))}
          // The banner is about a restart that has not happened; once one has,
          // it is describing nothing. Dismissing is exactly that, and the same
          // thing the operator would otherwise have to do by hand.
          action={<RestartButton onRestarted={onDismiss} />}
        >
          <p>{t("server.restartPendingBody")}</p>
        </Notice>
      ) : null}

      {notes.warnings.length > 0 ? (
        <Notice icon={Warning} tone="info" title={String(t("server.warningsTitle"))}>
          <ul className="space-y-1.5">
            {notes.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </Notice>
      ) : null}
    </div>
  );
});
