/* eslint-disable react-refresh/only-export-components -- the two endpoint
   builders and the download hook are the config-delivery half of this dialog;
   splitting them out would give every caller two imports for one feature. */
import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { CaretRight, DownloadSimple, QrCode } from "@/lib/icons";

import { CopyButton } from "@/components/CopyButton";
import { ErrorState } from "@/components/ErrorState";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { useToast } from "@/components/ui/toast";
import { api, isApiError, type ApiError } from "@/api/client";
import { cn } from "@/lib/utils";
import type { Client } from "@/api/types";

/*
 * Everything needed to get a config onto a device: the QR the app scans, the
 * text for anything that cannot scan, and the file.
 *
 * The one job here is that the code is in front of a camera in one look, so the
 * dialog is sized around it: a narrow box, the QR at the top at a size a phone
 * locks onto from arm's length, and nothing below it tall enough to push the
 * code off a laptop screen. The config text is a disclosure rather than a
 * panel for the same reason - it is the fallback for a device that cannot
 * scan, not the thing most people came for. The description carries the private
 * key warning, so there is no second box repeating it.
 *
 * All of it carries the client's private key. Nothing is cached beyond the open
 * dialog and the panel sends no-store, so the key does not sit in the browser
 * cache after the operator closes it.
 */

/** text/plain attachment, private key included. */
export function clientConfigPath(name: string): string {
  return `clients/${encodeURIComponent(name)}/config`;
}

/** Server-rendered PNG of the same config. */
export function clientQrPath(name: string): string {
  return `clients/${encodeURIComponent(name)}/qr`;
}

/** One dialog is open at a time, so a constant is enough to tie button to panel. */
const CONFIG_PANEL_ID = "client-config-text";

export interface ConfigDownload {
  /** Save <name>.conf. Failures arrive as a notification, not an exception. */
  start: (name: string) => void;
  /** Name being fetched right now, or null. */
  pending: string | null;
}

/**
 * Downloading through the API layer rather than an <a download> keeps the auth,
 * the base path and the error handling identical to every other request: a
 * plain link would drop the operator on a raw error page.
 */
export function useConfigDownload(): ConfigDownload {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [pending, setPending] = React.useState<string | null>(null);

  const start = React.useCallback(
    (name: string) => {
      setPending(name);
      void api
        .download(clientConfigPath(name), `${name}.conf`)
        .catch((error: unknown) => {
          toast({
            title: String(t("clients.downloadFailed", { name })),
            description: isApiError(error) ? error.detail : String(t("errors.generic")),
            variant: "destructive",
          });
        })
        .finally(() => setPending(null));
    },
    [t, toast],
  );

  return { start, pending };
}

interface QrImageProps {
  name: string;
  alt: string;
}

function QrImage({ name, alt }: QrImageProps): JSX.Element {
  const { t } = useTranslation();
  const [state, setState] = React.useState<"loading" | "ready" | "error">("loading");

  if (state === "error") {
    return (
      <div className="flex shrink-0 flex-col items-center justify-center rounded-lg border border-dashed border-border bg-muted/40 px-4 py-6 text-center">
        <QrCode weight="duotone" className="h-6 w-6 text-muted-foreground" aria-hidden="true" />
        <p className="mt-3 text-sm font-medium">{t("clients.qrUnavailable")}</p>
        <p className="mt-1 max-w-xs text-sm text-muted-foreground">
          {t("clients.qrUnavailableHint")}
        </p>
      </div>
    );
  }

  return (
    // Square and capped in both directions: wide enough to scan, never taller
    // than a short laptop window, and it shrinks with the dialog on a phone
    // instead of forcing the box to scroll. shrink-0 keeps the code its full
    // size when the config text below it opens - that panel takes the space
    // that is left, and the code is the thing nobody wants squashed.
    <div className="mx-auto w-[min(13.5rem,56vw,40vh)] shrink-0">
      {/* Deliberately white in both themes: a QR code needs the quiet zone and
          the contrast the PNG was drawn with, and a dark card behind it is the
          usual reason a camera refuses to lock on. */}
      <div className="relative rounded-lg border border-border bg-white p-2.5">
        {state === "loading" ? (
          <div className="absolute inset-0 flex items-center justify-center">
            <Spinner className="text-zinc-500" />
          </div>
        ) : null}
        <img
          src={api.blobUrl(clientQrPath(name))}
          alt={alt}
          width={256}
          height={256}
          className={cn("aspect-square w-full", state === "loading" ? "opacity-0" : "opacity-100")}
          onLoad={() => setState("ready")}
          onError={() => setState("error")}
        />
      </div>
    </div>
  );
}

export interface QrDialogProps {
  /**
   * The client whose config is on show. Kept non-null by the page while the
   * dialog animates out, so the content does not vanish before the box does.
   */
  client: Client | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function QrDialog({ client, open, onOpenChange }: QrDialogProps): JSX.Element | null {
  const { t } = useTranslation();
  const download = useConfigDownload();
  const name = client?.name ?? "";
  // Closed on every opening: the QR is what the dialog is for, and a text block
  // that stayed open from last time would push it off a short screen.
  const [showConfig, setShowConfig] = React.useState(false);

  React.useEffect(() => {
    if (!open) {
      setShowConfig(false);
    }
  }, [open]);

  const config = useQuery<string, ApiError>({
    // Under the clients prefix on purpose: resetting the keys invalidates the
    // list, and this text is exactly as stale as the list is.
    queryKey: ["clients", name, "config"],
    queryFn: () => api.get<string>(clientConfigPath(name)),
    enabled: open && name.length > 0,
    retry: false,
    // Long enough to survive closing and reopening the dialog, short enough
    // that a change made over SSH shows up on the next visit.
    staleTime: 30_000,
    gcTime: 60_000,
  });

  if (!client) {
    return null;
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/*
        A column, not the default grid, and every child but the config panel is
        shrink-0. That is what makes opening the config text cost nothing: the
        dialog is already at most the height of the window, so a panel that grew
        would push the box past it and turn the whole thing into a scrolling
        window with the QR somewhere above the fold. Instead the panel is the
        one flexible child and takes whatever room is left over.
      */}
      <DialogContent className="flex flex-col gap-3 sm:max-w-sm">
        <DialogHeader className="shrink-0">
          <DialogTitle>{t("clients.qrTitle")}</DialogTitle>
          {/* Says what to do with the code and that it is key material, which is
              why there is no separate warning box below it. */}
          <DialogDescription>{t("clients.scanHint")}</DialogDescription>
        </DialogHeader>

        <QrImage
          // Resetting the keys produces a different code for the same name, so
          // the element is rebuilt rather than left showing the old one.
          key={client.publicKey}
          name={client.name}
          alt={String(t("clients.qrAlt", { name: client.name }))}
        />

        <p className="flex shrink-0 flex-wrap items-baseline justify-center gap-x-2 text-sm">
          <span className="font-medium">{client.name}</span>
          <span className="font-mono text-xs text-muted-foreground">{client.ip}</span>
        </p>

        <div
          className={cn(
            "rounded-lg border border-border",
            // Only while it is open. A collapsed row given flex-1 would stretch
            // to fill the dialog and leave a bordered void under the QR.
            showConfig && "flex min-h-0 flex-1 flex-col",
          )}
        >
          <div className="flex shrink-0 items-center gap-1 px-1.5 py-1.5">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="min-w-0 flex-1 justify-start gap-2 font-normal"
              aria-expanded={showConfig}
              // Only while the panel exists: aria-controls pointing at nothing
              // is worse for a screen reader than not being there at all.
              aria-controls={showConfig ? CONFIG_PANEL_ID : undefined}
              onClick={() => setShowConfig((current) => !current)}
            >
              <CaretRight
                aria-hidden="true"
                className={cn("transition-transform", showConfig && "rotate-90")}
              />
              <span className="truncate">{t("clients.configTitle")}</span>
            </Button>
            <CopyButton
              value={config.data ?? ""}
              label={String(t("common.copy"))}
              disabled={!config.data}
            />
          </div>

          {showConfig ? (
            // min-h-0 is what lets this shrink below the height of the text
            // inside it; without it a flex child refuses to go under its
            // content and the dialog grows again. max-h caps the other
            // direction, so a tall window gets a readable block rather than a
            // wall of monospace.
            <div
              id={CONFIG_PANEL_ID}
              className="max-h-64 min-h-0 flex-1 overflow-y-auto border-t border-border"
            >
              {config.isPending ? (
                <div className="space-y-2 p-3" aria-busy="true">
                  <Skeleton className="h-3 w-40" />
                  <Skeleton className="h-3 w-56" />
                  <Skeleton className="h-3 w-32" />
                </div>
              ) : config.isError ? (
                <ErrorState
                  variant="inline"
                  className="m-3"
                  error={config.error}
                  onRetry={() => void config.refetch()}
                />
              ) : (
                // Wrapped rather than scrolled sideways: a 44-character key is
                // wider than this box, and panning a text block left and right
                // to read a config is the worst way to read one.
                <pre className="whitespace-pre-wrap break-all p-3 font-mono text-xs leading-relaxed">
                  {config.data}
                </pre>
              )}
            </div>
          ) : null}
        </div>

        <DialogFooter className="shrink-0">
          <DialogClose asChild>
            <Button variant="outline">{t("common.close")}</Button>
          </DialogClose>
          <Button
            onClick={() => download.start(client.name)}
            loading={download.pending === client.name}
          >
            {download.pending === client.name ? (
              <Spinner size="sm" decorative />
            ) : (
              <DownloadSimple aria-hidden="true" />
            )}
            {t("clients.downloadConfig")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
