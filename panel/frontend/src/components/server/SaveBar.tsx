import { FloppyDisk } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { StickyActionBar } from "@/components/StickyActionBar";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";

/*
 * The bar that appears once something has been edited, and states what applying
 * it costs before it is applied.
 *
 * Two sentences, always in the same order: whether the tunnel is about to drop,
 * and whether every client will need a new config afterwards. An operator who
 * finds out the second one after pressing Save has been told too late.
 */

export interface SaveBarProps {
  /** How many settings differ from the server. */
  changedCount: number;
  needsRestart: boolean;
  mustReimport: boolean;
  saving: boolean;
  onDiscard: () => void;
  onSave: () => void;
}

export function SaveBar({
  changedCount,
  needsRestart,
  mustReimport,
  saving,
  onDiscard,
  onSave,
}: SaveBarProps): JSX.Element {
  const { t } = useTranslation();

  return (
    <StickyActionBar
      className="mt-4"
      actions={
        <>
          <Button type="button" variant="ghost" onClick={onDiscard} disabled={saving}>
            {t("server.discardChanges")}
          </Button>
          <Button type="button" onClick={onSave} loading={saving}>
            {saving ? <Spinner aria-hidden="true" /> : <FloppyDisk aria-hidden="true" />}
            {t(saving ? "common.saving" : "server.save")}
          </Button>
        </>
      }
    >
      <p className="text-sm font-medium">
        {saving ? (
          t("common.saving")
        ) : (
          <>
            {t("server.unsavedChanges")}
            <span className="ms-2 font-normal text-muted-foreground">
              {t("server.changedFields", { count: changedCount })}
            </span>
          </>
        )}
      </p>
      <p className="text-xs leading-relaxed text-muted-foreground">
        {t(needsRestart ? "server.applyRestart" : "server.applyHot")}
      </p>
      {mustReimport ? (
        <p className="text-xs font-medium leading-relaxed text-warning">
          {t("server.mustReimport")}
        </p>
      ) : null}
    </StickyActionBar>
  );
}
