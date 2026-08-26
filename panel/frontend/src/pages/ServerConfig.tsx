import {
  HardDrives,
  Network,
  ShieldCheck,
  SlidersHorizontal,
  Warning,
  type Icon,
} from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { CopyButton } from "@/components/CopyButton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { PageHeader } from "@/components/PageHeader";
import { BandwidthCard } from "@/components/server/BandwidthCard";
import { LoadingGroups } from "@/components/server/LoadingGroups";
import { Notice } from "@/components/server/Notice";
import { ParamField } from "@/components/server/ParamField";
import { ParamGroup } from "@/components/server/ParamGroup";
import { SaveBar } from "@/components/server/SaveBar";
import { SaveNotices } from "@/components/server/SaveNotices";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useServerStatus } from "@/api/hooks";
import { useBandwidthForm } from "@/lib/bandwidthForm";
import { useServerForm } from "@/lib/serverForm";
import type { ParamGroup as ParamGroupId, ParamSpec } from "@/api/types";

/*
 * The Server Config page: where the tunnel listens, and how fast it lets anyone
 * through it.
 *
 * The protection card below the network one is not obfuscation and did not go
 * with it: it turns off the cookie challenge that answers a handshake flood,
 * which changes what this server does under attack rather than what its traffic
 * looks like, and no client ever sees it.
 *
 * Everything that disguises the traffic used to be here too, and moved to the
 * Obfuscation page. The two are one config behind one PUT, but they are not one
 * job: the settings below are decided at install time and revisited when
 * something about the network changes, while the obfuscation is redrawn, handed
 * out and redrawn again. Keeping them apart is what lets each save bar say
 * something specific about what it is about to cost.
 *
 * The firewall hooks were shown here as well, as a card nobody could edit -
 * PostUp, PostDown and PreDown, read back from awg0.conf with a note saying to
 * go and edit them on the server. They are the installer's, they change when the
 * installer changes them, and what each of them is for is written down in
 * docs/PANEL.md and README.md at a length no card has room for. A page of
 * settings is a page of things that can be set.
 *
 * The form is generated from GET server/params, which is the single source of
 * truth for every label, every sentence of help, every bound and every badge,
 * and is the same module the API validates against. Nothing in this file names
 * a parameter for the purpose of describing it. Bandwidth is the exception below
 * it: those are panel settings behind their own endpoint rather than catalog
 * parameters, so they keep a draft of their own - but not a Save of their own.
 * One page, one save bar; it counts both drafts and sends each to its endpoint.
 */

/** Card order. The catalog decides which groups exist; this decides the story. */
const GROUP_ORDER: readonly ParamGroupId[] = ["network", "protection"];

/** The groups this page owns, and therefore the only ones it can save. */
const OWNS: ReadonlySet<string> = new Set(["network", "protection"]);

const GROUP_ICONS: Readonly<Record<string, Icon>> = {
  network: Network,
  protection: ShieldCheck,
};

export default function ServerConfig(): JSX.Element {
  const { t } = useTranslation();
  const status = useServerStatus();
  const form = useServerForm(OWNS);
  const bandwidth = useBandwidthForm();

  const ifaceUp = status.data?.ifaceUp ?? true;
  const statusWarnings = status.data?.warnings ?? [];

  /*
   * The two drafts, counted and saved as one. They go to two endpoints and only
   * the changed one is sent, but the operator edited one page and presses one
   * button, so the bar states the total and both discards run together.
   */
  const dirty = form.dirty || bandwidth.dirty;
  const saving = form.saving || bandwidth.saving;

  const saveAll = (): void => {
    if (form.dirty) {
      form.save();
    }
    if (bandwidth.dirty) {
      bandwidth.save();
    }
  };

  const discardAll = (): void => {
    form.discard();
    bandwidth.discard();
  };

  const groups = GROUP_ORDER.map((group) => ({
    id: group,
    specs: form.specs.filter((spec) => spec.group === group),
  })).filter((group) => group.specs.length > 0);

  /** One card for one catalog group. */
  const renderGroup = (group: { id: ParamGroupId; specs: ParamSpec[] }): JSX.Element => {
    return (
      <ParamGroup
        key={group.id}
        group={group.id}
        icon={GROUP_ICONS[group.id]}
        changedCount={group.specs.filter((spec) => form.changed.includes(spec.key)).length}
        errorCount={group.specs.filter((spec) => form.errorFor(spec.key)).length}
      >
        {group.specs.map((spec) => (
          <ParamField
            key={spec.key}
            spec={spec}
            value={form.values[spec.key] ?? ""}
            onChange={form.change}
            onRevert={form.revert}
            changed={form.changed.includes(spec.key)}
            error={form.errorFor(spec.key)}
            disabled={form.saving}
          />
        ))}

        {group.id === "network" && form.server.data ? (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-border pt-4 sm:col-span-2">
            <span className="text-sm font-medium">{t("server.publicKey")}</span>
            <code className="min-w-0 break-all font-mono text-xs text-muted-foreground">
              {form.server.data.publicKey || t("common.notAvailable")}
            </code>
            {form.server.data.publicKey ? (
              <CopyButton
                value={form.server.data.publicKey}
                aria-label={String(t("server.copyPublicKey"))}
              />
            ) : null}
          </div>
        ) : null}
      </ParamGroup>
    );
  };

  const header = (
    <PageHeader
      title={String(t("server.title"))}
      description={String(t("server.subtitle"))}
      icon={HardDrives}
      badge={
        status.data ? (
          <Badge variant={ifaceUp ? "success" : "warning"}>
            {t(ifaceUp ? "server.statusUp" : "server.statusDown")}
          </Badge>
        ) : undefined
      }
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
          bottom edge of the window rather than wherever the last group ends. */}
      <div className="flex-1 space-y-4">
        {statusWarnings.length > 0 ? (
          <Notice icon={Warning} title={String(t("server.warningsTitle"))}>
            <ul className="space-y-1.5">
              {statusWarnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </Notice>
        ) : null}

        {form.notes ? (
          <SaveNotices ref={form.notesRef} notes={form.notes} onDismiss={form.dismissNotes} />
        ) : null}

        {groups.map((group) => renderGroup(group))}

        {/* Last, because it is stored as a panel setting rather than in
            awg0.conf and is therefore the one card the API cannot restart the
            interface for. The bar below saves it with the rest. */}
        <BandwidthCard form={bandwidth} />
      </div>

      {/* Outlives `dirty` while a save is in flight, so the bar and its
          spinner do not disappear the moment the request lands. */}
      {dirty || saving ? (
        <SaveBar
          changedCount={form.changed.length + bandwidth.changed.length}
          needsRestart={form.cost.needsRestart}
          mustReimport={form.cost.mustReimport}
          saving={saving}
          onDiscard={discardAll}
          onSave={saveAll}
        />
      ) : null}
    </>
  );
}
