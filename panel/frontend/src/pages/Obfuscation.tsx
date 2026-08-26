import * as React from "react";
import { MaskHappy, SlidersHorizontal } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { PageHeader } from "@/components/PageHeader";
import { AdvancedCard } from "@/components/server/AdvancedCard";
import { HelpDrawer } from "@/components/server/HelpDrawer";
import { LoadingGroups } from "@/components/server/LoadingGroups";
import { ObfuscationCard } from "@/components/server/ObfuscationCard";
import { SaveBar } from "@/components/server/SaveBar";
import { SaveNotices } from "@/components/server/SaveNotices";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useServerForm } from "@/lib/serverForm";
import type { ParamPreviewResult } from "@/api/types";

/*
 * The Obfuscation page: everything that decides what the traffic looks like.
 *
 * These settings used to sit under Server, below the port and the address, and
 * that put two unlike jobs on one page. Where the tunnel listens is set once
 * and then left alone for a year. What it looks like on the wire is the part an
 * admin comes back to - drawn, compared against what a filter seems to be
 * catching, and drawn again - and it is also the part that costs every client a
 * new config each time. Splitting them means the expensive page is the one you
 * have to navigate to, and the save bar on it only ever talks about this.
 *
 * Two cards, in the order the cost rises. The obfuscation set works on every
 * client that exists. The advanced group below it works on almost none of
 * them yet, and breaks the rest silently, which is why it comes second, starts
 * closed and carries its warning on its face.
 *
 * The form is generated from GET server/params. That catalog is the single
 * source of truth for every label, every sentence of help, every bound and
 * every badge, and it is the same module the API validates against - so the
 * words on screen cannot drift from the rules being enforced.
 */

/** The groups this page owns, and therefore the only ones it can save. */
const OWNS: ReadonlySet<string> = new Set(["junk", "sizes", "headers", "imitation", "advanced"]);

/*
 * The mark beside the sentence about matching values.
 *
 * Not a Notice, and not Phosphor's warning triangle. Notice is a block for
 * something a save has left behind - clients to re-issue, a restart that did
 * not happen - and its border and tinted panel say that a thing is outstanding.
 * This sentence is not outstanding: it is a standing fact about the page, true
 * before anything on it has been touched, and framing it as an alert spends the
 * page's one loud element on something that will never stop being there. So it
 * is a line of text with a small mark in front of it.
 *
 * The mark is drawn here rather than named in lib/icons because that module's
 * vocabulary is Phosphor's, and Phosphor has no square warning - only the
 * triangle, the circle, the diamond and the octagon, every one of them a road
 * sign, which is the thing this is trying not to be.
 *
 * The exclamation is a hole punched through the square, not a second fill, so
 * what shows in it is the page behind: white out of near-black on the light
 * theme, near-black out of white on the dark one, from the one currentColor and
 * with no second value to keep in step.
 */
function MatchMark(): JSX.Element {
  return (
    <svg viewBox="0 0 16 16" className="mt-0.5 h-4 w-4 shrink-0 text-foreground" aria-hidden="true">
      <path
        fill="currentColor"
        fillRule="evenodd"
        clipRule="evenodd"
        d="M3.5 0h9A3.5 3.5 0 0 1 16 3.5v9a3.5 3.5 0 0 1-3.5 3.5h-9A3.5 3.5 0 0 1 0 12.5v-9A3.5 3.5 0 0 1 3.5 0Zm4.5 3.25a1 1 0 0 0-1 1v4.4a1 1 0 0 0 2 0v-4.4a1 1 0 0 0-1-1ZM8 10.6a1.15 1.15 0 1 0 0 2.3 1.15 1.15 0 0 0 0-2.3Z"
      />
    </svg>
  );
}

export default function Obfuscation(): JSX.Element {
  const { t } = useTranslation();
  const { toast } = useToast();
  const form = useServerForm(OWNS);

  /*
   * A drawn set arrives with whatever the API has to say about it. That is
   * normally nothing - no profile is allowed to hand back a set the save bar
   * would then complain about - but a warning that did arrive is about the set
   * on screen and there is no second moment to read it in, so it takes the
   * toast's line rather than the reassuring sentence.
   */
  const filled = React.useCallback(
    (preview: ParamPreviewResult) => {
      form.fill(preview.params);
      toast({
        title: String(t("server.reconfigured")),
        description: preview.warnings[0] ?? String(t("server.reconfiguredBody")),
      });
    },
    [form, t, toast],
  );

  const failed = React.useCallback(
    (detail: string) => {
      toast({
        title: String(t("server.reconfigureFailed")),
        description: detail,
        variant: "destructive",
      });
    },
    [t, toast],
  );

  /*
   * Same again, except here the warning is the rule rather than the exception:
   * every value the advanced generator draws needs AmneziaWG 3.0+ at the far
   * end, and anything the installed module cannot do comes back empty with a
   * sentence saying so. Afterwards the form looks like a set that simply works.
   */
  const generated = React.useCallback(
    (preview: ParamPreviewResult) => {
      form.fill(preview.params);
      toast({
        title: String(t("server.advancedGenerated")),
        description: preview.warnings[0] ?? String(t("server.advancedGeneratedBody")),
      });
    },
    [form, t, toast],
  );

  const clear = React.useCallback(
    (values: Record<string, string>) => {
      form.fill(values);
      toast({
        title: String(t("server.advancedCleared")),
        description: String(t("server.advancedClearedBody")),
      });
    },
    [form, t, toast],
  );

  const header = (
    <PageHeader
      title={String(t("obfuscation.title"))}
      description={String(t("obfuscation.subtitle"))}
      icon={MaskHappy}
      actions={<HelpDrawer specs={form.specs} />}
    />
  );

  if (form.server.isError || form.catalog.isError) {
    const broken = form.server.isError ? form.server : form.catalog;
    return (
      <>
        {header}
        <ErrorState
          error={broken.error}
          title={String(t("server.loadFailed"))}
          onRetry={() => {
            void form.server.refetch();
            void form.catalog.refetch();
          }}
        />
      </>
    );
  }

  if (form.server.isPending || form.catalog.isPending) {
    return (
      <>
        {header}
        <LoadingGroups cards={2} />
      </>
    );
  }

  if (form.specs.length === 0) {
    return (
      <>
        {header}
        <EmptyState
          icon={SlidersHorizontal}
          title={String(t("server.noParams"))}
          description={String(t("server.noParamsHint"))}
          action={
            <Button variant="outline" size="sm" onClick={() => void form.catalog.refetch()}>
              {t("common.retry")}
            </Button>
          }
        />
      </>
    );
  }

  return (
    <>
      {header}

      {/* Grows into the leftover height so the save bar below lands on the
          bottom edge of the window rather than wherever the last card ends. */}
      <div className="flex-1 space-y-4">
        {form.notes ? (
          <SaveNotices ref={form.notesRef} notes={form.notes} onDismiss={form.dismissNotes} />
        ) : null}

        <div role="note" className="flex gap-2.5 text-sm">
          <MatchMark />
          <div className="min-w-0 space-y-1">
            <p className="font-medium leading-tight">{t("obfuscation.matchTitle")}</p>
            <p className="leading-relaxed text-muted-foreground">{t("obfuscation.matchBody")}</p>
          </div>
        </div>

        <ObfuscationCard
          specs={form.specs}
          values={form.values}
          changed={form.changed}
          errorFor={form.errorFor}
          onChange={form.change}
          onRevert={form.revert}
          onReconfigure={filled}
          onReconfigureFailed={failed}
          icon={MaskHappy}
          disabled={form.saving}
        />

        <AdvancedCard
          specs={form.specs}
          values={form.values}
          changed={form.changed}
          errorFor={form.errorFor}
          onChange={form.change}
          onRevert={form.revert}
          onGenerate={generated}
          onGenerateFailed={failed}
          onClear={clear}
          disabled={form.saving}
        />
      </div>

      {/* Outlives `dirty` while a save is in flight, so the bar and its
          spinner do not disappear the moment the request lands. */}
      {form.dirty || form.saving ? (
        <SaveBar
          changedCount={form.changed.length}
          needsRestart={form.cost.needsRestart}
          mustReimport={form.cost.mustReimport}
          saving={form.saving}
          onDiscard={form.discard}
          onSave={form.save}
        />
      ) : null}
    </>
  );
}
