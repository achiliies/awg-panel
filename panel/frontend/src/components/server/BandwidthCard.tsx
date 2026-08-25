import * as React from "react";
import { Speedometer } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { GroupHeader } from "@/components/server/GroupHeader";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useBulkLimitClients, useStatsSummary } from "@/api/hooks";
import { MBIT, cn } from "@/lib/utils";
import type { BandwidthForm } from "@/lib/bandwidthForm";

/*
 * Speed limits, on the page about the server rather than the one about the panel.
 *
 * They were on the settings page, among the panel's own listen address and
 * session length, and they never belonged there: a shaping queue is attached to
 * the tunnel, survives nothing but the kernel, and is the same kind of decision
 * as the port the tunnel listens on. Somebody looking for "why is this client
 * capped" looks at the server, so this is where the switch is.
 *
 * The draft and the save are the page's, through useBandwidthForm, so the save
 * bar along the bottom is the only thing on the page that saves anything. The
 * card draws the fields and counts what has been changed, the same as the
 * parameter groups above it.
 *
 * Everything below the master switch is folded away while limits are off. Not
 * greyed out: a disabled row of settings still reads as settings that are doing
 * something, and none of these do anything at all until the switch is on.
 *
 * Each switch is answered on its own line by the number it turns on. Throwing
 * "Speed limits" puts the download default beside it, throwing "Limit upload
 * too" puts the upload default beside that, so the field a switch produces is
 * where the eye already is rather than in a block further down that has to be
 * matched back up to the switch that caused it.
 *
 * The outgoing interface follows them both, inside the upload fold, because it
 * is upload's alone: a download queue sits on the tunnel and never asks. It is
 * the one field here that is right left empty on almost every server, so it
 * says as much in a line rather than presenting itself as something to fill in,
 * and the multi-homed cases that do need it are in docs/PANEL.md rather than in
 * a hint nobody with one network card should have to read.
 */

export interface BandwidthCardProps {
  form: BandwidthForm;
}

export function BandwidthCard({ form }: BandwidthCardProps): JSX.Element {
  const { t } = useTranslation();

  /** t() with an English original, so a key the catalog lacks never shows raw. */
  const text = React.useCallback(
    (key: string, fallback: string, vars?: Record<string, string | number>): string =>
      String(t(key, { defaultValue: fallback, ...vars })),
    [t],
  );

  const { values, errors, change } = form;

  // Read off the form rather than off the saved settings, so the card opens and
  // closes as the switch is thrown - before the save, which is when somebody is
  // looking at it.
  const shapingOn = values.shaperOn === "1";
  const uploadOn = shapingOn && values.shaperUpload === "1";
  const disabled = form.loading || form.saving;

  const changedCount = form.changed.length;
  const errorCount = Object.keys(errors).length;

  return (
    <Card>
      <GroupHeader
        icon={Speedometer}
        title={text("settings.bandwidth", "Bandwidth limits")}
        description={text(
          "settings.bandwidthHint",
          "Turn this on to give clients a speed limit. While it is off nothing is limited and the tunnel keeps the queueing the kernel gives it - the limits themselves are kept, so turning it back on puts every one of them back.",
        )}
        actions={
          errorCount > 0 || changedCount > 0 ? (
            <>
              {errorCount > 0 ? (
                <Badge variant="destructive">
                  {t("server.groupErrors", { count: errorCount })}
                </Badge>
              ) : null}
              {changedCount > 0 ? (
                <Badge>{t("server.groupChanged", { count: changedCount })}</Badge>
              ) : null}
            </>
          ) : null
        }
      />

      <CardContent>
        <div className="grid gap-6 sm:grid-cols-2">
          <Field
            id="shaperOn"
            label={text("settings.shapingOn", "Speed limits")}
            hint={text(
              "settings.shapingOnHint",
              "Clients with no limit of their own are unaffected in what they may transfer.",
            )}
            error={errors.shaperOn}
          >
            <div className="flex items-center gap-3">
              <Switch
                id="shaperOn"
                checked={shapingOn}
                disabled={disabled}
                onCheckedChange={(on) => change("shaperOn", on ? "1" : "0")}
              />
              <span className="text-sm text-muted-foreground">
                {shapingOn ? t("common.on") : t("common.off")}
              </span>
            </div>
          </Field>

          {shapingOn ? (
            <Field
              id="shaperDefaultDownMbps"
              label={text("settings.defaultDown", "Default download limit")}
              hint={text(
                "settings.defaultDownHint",
                "What a newly added client is given. 0 means no limit. Existing clients keep what they have.",
              )}
              error={errors.shaperDefaultDownMbps}
            >
              <MbpsInput
                id="shaperDefaultDownMbps"
                value={values.shaperDefaultDownMbps}
                invalid={Boolean(errors.shaperDefaultDownMbps)}
                disabled={disabled}
                onChange={(value) => change("shaperDefaultDownMbps", value)}
              />
            </Field>
          ) : null}
        </div>

        {/*
          A grid row that goes from 0fr to 1fr, which is the one way to animate
          to a height the content decides for itself. `invisible` is what keeps
          the folded fields out of the tab order, and it is in the transition
          list so it waits for the fold to finish rather than blanking the card
          the instant the switch is thrown. The padding is inside the fold, so a
          closed card ends at the switch rather than at a band of empty space.
        */}
        <div
          className={cn(
            "grid transition-[grid-template-rows,opacity,visibility]",
            shapingOn
              ? "visible grid-rows-[1fr] opacity-100"
              : "invisible grid-rows-[0fr] opacity-0",
          )}
        >
          {/*
            The clip the fold needs is a clip like any other, and a focus ring
            is painted outside the box it belongs to - so the switch and the
            interface box, which both sit flush against the start of this
            column, had the leading edge of their ring shaved off the moment
            they were tabbed to. The clipping box is widened by the ring's own
            width on each side and the padding puts the fields back where they
            were, which buys the room without moving anything. Horizontally
            only: padding here is padding a closed fold cannot collapse away,
            and a band of it would sit under the switch with the card shut.
          */}
          <div className="-mx-1 overflow-hidden px-1">
            <div className="grid gap-6 pt-6 sm:grid-cols-2">
              <Field
                id="shaperUpload"
                label={text("settings.uploadShaping", "Limit upload too")}
                error={errors.shaperUpload}
              >
                <div className="flex items-center gap-3">
                  <Switch
                    id="shaperUpload"
                    checked={values.shaperUpload === "1"}
                    disabled={disabled}
                    onCheckedChange={(on) => change("shaperUpload", on ? "1" : "0")}
                  />
                  <span className="text-sm text-muted-foreground">
                    {values.shaperUpload === "1" ? t("common.on") : t("common.off")}
                  </span>
                </div>
              </Field>

              {uploadOn ? (
                <Field
                  id="shaperDefaultUpMbps"
                  label={text("settings.defaultUp", "Default upload limit")}
                  hint={text("settings.defaultUpHint", "The same, for what a client may send.")}
                  error={errors.shaperDefaultUpMbps}
                >
                  <MbpsInput
                    id="shaperDefaultUpMbps"
                    value={values.shaperDefaultUpMbps}
                    invalid={Boolean(errors.shaperDefaultUpMbps)}
                    disabled={disabled}
                    onChange={(value) => change("shaperDefaultUpMbps", value)}
                  />
                </Field>
              ) : null}

              {/* Upload's own, so it lives and dies with upload's switch: the
                  download queue is on the tunnel and this would mean nothing
                  beside it. */}
              {uploadOn ? (
                <Field
                  id="shaperWanIface"
                  label={text("settings.wanIface", "Outgoing interface")}
                  hint={text(
                    "settings.wanIfaceHint",
                    "Upload only - download is shaped on the tunnel itself. Empty means whichever interface holds the default route, which is right unless this server has several network cards; the manual covers those.",
                  )}
                  error={errors.shaperWanIface}
                >
                  <Input
                    id="shaperWanIface"
                    value={values.shaperWanIface}
                    placeholder={text("settings.wanIfaceAuto", "auto")}
                    disabled={disabled}
                    autoComplete="off"
                    className="w-32 font-mono text-xs"
                    aria-invalid={Boolean(errors.shaperWanIface)}
                    onChange={(event) => change("shaperWanIface", event.target.value)}
                  />
                </Field>
              ) : null}

              <ApplyToAll
                disabled={disabled}
                down={values.shaperDefaultDownMbps}
                up={uploadOn ? values.shaperDefaultUpMbps : "0"}
                uploadOn={uploadOn}
                text={text}
              />
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

interface MbpsInputProps {
  id: string;
  value: string;
  invalid: boolean;
  disabled: boolean;
  onChange: (value: string) => void;
}

/** A number with its unit spelled out beside it, never just a bare box. */
function MbpsInput({ id, value, invalid, disabled, onChange }: MbpsInputProps): JSX.Element {
  const { t } = useTranslation();

  return (
    <div className="flex items-center gap-2">
      <Input
        id={id}
        inputMode="numeric"
        className="w-32"
        value={value}
        disabled={disabled}
        aria-invalid={invalid}
        onChange={(event) => onChange(event.target.value)}
      />
      <span className="text-sm text-muted-foreground">{t("units.megabitsPerSecond")}</span>
    </div>
  );
}

interface ApplyToAllProps {
  disabled: boolean;
  /** The two default fields, in megabits, exactly as they read on the form. */
  down: string;
  up: string;
  uploadOn: boolean;
  text: (key: string, fallback: string, vars?: Record<string, string | number>) => string;
}

/**
 * Write the limit above over every client that already exists.
 *
 * Separate from the default beside it because they answer different questions.
 * The default is what the next client gets and changing it is harmless; this
 * reaches backwards over everybody, destroys every limit anyone set by hand, and
 * has no undo - so it is a button that asks, and the asking names the number of
 * clients it is about to change rather than saying "are you sure".
 *
 * It sends the numbers off the form rather than the saved settings, so it
 * applies what the operator is looking at. An unsaved default and a button that
 * quietly used the previous one would be the worst of both.
 */
function ApplyToAll({ disabled, down, up, uploadOn, text }: ApplyToAllProps): JSX.Element {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [open, setOpen] = React.useState(false);
  const summary = useStatsSummary();
  const apply = useBulkLimitClients();

  const clients = summary.data?.totalClients ?? 0;
  const downMbps = Number(down) || 0;
  const upMbps = uploadOn ? Number(up) || 0 : 0;
  const spoken = (mbps: number): string =>
    mbps > 0
      ? `${mbps} ${String(t("units.megabitsPerSecond"))}`
      : String(text("settings.applyAllNoLimit", "no limit"));

  const run = (): void => {
    apply.mutate(
      { downBps: downMbps * MBIT, upBps: upMbps * MBIT },
      {
        onSuccess: ({ changed, applied, reason }) => {
          setOpen(false);
          toast({
            title: text("settings.applyAllDone", "{{count}} client(s) updated", {
              count: changed,
            }),
            description: applied ? undefined : reason,
            variant: applied ? undefined : "destructive",
          });
        },
        onError: (error) => {
          toast({ title: String(error.message), variant: "destructive" });
        },
      },
    );
  };

  return (
    <Field
      id="applyToAll"
      label={text("settings.applyAll", "Apply to existing clients")}
      hint={text(
        "settings.applyAllHint",
        "Gives every client that already exists the limit above, replacing whatever each of them has now. This cannot be undone.",
      )}
      className="sm:col-span-2"
    >
      <AlertDialog open={open} onOpenChange={setOpen}>
        <Button
          type="button"
          variant="outline"
          disabled={disabled || apply.isPending}
          onClick={() => setOpen(true)}
        >
          {text("settings.applyAllAction", "Apply to all clients")}
        </Button>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {text("settings.applyAllConfirm", "Overwrite every client's limit?")}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {uploadOn
                ? text(
                    "settings.applyAllConfirmBoth",
                    "All {{clients}} client(s) on this server will be set to {{down}} down and {{up}} up. Every limit set by hand is replaced, and there is nothing that puts the old numbers back.",
                    { clients, down: spoken(downMbps), up: spoken(upMbps) },
                  )
                : text(
                    "settings.applyAllConfirmDown",
                    "All {{clients}} client(s) on this server will be set to {{down}}. Every limit set by hand is replaced, and there is nothing that puts the old numbers back.",
                    { clients, down: spoken(downMbps) },
                  )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={run} disabled={apply.isPending}>
              {text("settings.applyAllAction", "Apply to all clients")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Field>
  );
}
