/* eslint-disable react-refresh/only-export-components -- useCopy is the button
   without the button: the Support page copies from the address itself, which is
   a control this file has no business drawing. */
import * as React from "react";
import { Check, Copy } from "@/lib/icons";
import { useTranslation } from "react-i18next";
import type { VariantProps } from "class-variance-authority";

import { Button, type buttonVariants } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";

/*
 * navigator.clipboard only exists in a secure context, and the panel is very
 * often reached over plain HTTP on a LAN address. Falling back to a detached
 * textarea plus execCommand is the only way copying a public key or a config
 * works there at all, so it is the normal path rather than a legacy branch.
 */

/** How long the button stays in its confirmed state. */
const COPIED_MS = 2000;

function copyViaTextarea(text: string): boolean {
  if (typeof document === "undefined") {
    return false;
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  // Off-screen rather than display:none, which browsers refuse to select from.
  area.style.position = "fixed";
  area.style.top = "-9999px";
  area.style.opacity = "0";
  area.style.pointerEvents = "none";
  document.body.appendChild(area);

  const previous = document.activeElement;
  let copied = false;
  try {
    area.select();
    area.setSelectionRange(0, area.value.length);
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  } finally {
    area.remove();
    if (previous instanceof HTMLElement) {
      previous.focus();
    }
  }
  return copied;
}

async function writeClipboard(text: string): Promise<boolean> {
  if (typeof navigator !== "undefined" && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Permission denied or a non-secure origin: the textarea still works.
    }
  }
  return copyViaTextarea(text);
}

export interface Copier {
  /** True for COPIED_MS after a copy that worked. */
  copied: boolean;
  /** Copies, and says whether it managed to. A failure has already been shown. */
  copy: (value: string) => Promise<boolean>;
}

/**
 * Copying, with the two seconds of confirmation and the failure notification,
 * for a control that is not this file's button - the Support page's address
 * blocks, where the address itself is what gets clicked.
 *
 * One `copied` flag per hook, so a page that copies from several places wants
 * one call per place. That is what keeps the tick on the row that was clicked
 * rather than on all of them.
 */
export function useCopy(): Copier {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [copied, setCopied] = React.useState(false);
  const timer = React.useRef<number | undefined>(undefined);

  React.useEffect(
    () => () => {
      if (timer.current !== undefined) {
        window.clearTimeout(timer.current);
      }
    },
    [],
  );

  const copy = React.useCallback(
    async (value: string): Promise<boolean> => {
      const ok = await writeClipboard(value);
      if (!ok) {
        toast({
          title: String(t("common.copyFailed")),
          description: String(t("common.copyFailedBody")),
          variant: "destructive",
        });
        return false;
      }
      setCopied(true);
      if (timer.current !== undefined) {
        window.clearTimeout(timer.current);
      }
      timer.current = window.setTimeout(() => setCopied(false), COPIED_MS);
      return true;
    },
    [t, toast],
  );

  return { copied, copy };
}

export interface CopyButtonProps extends Omit<
  React.ButtonHTMLAttributes<HTMLButtonElement>,
  "value" | "children"
> {
  /** Exactly what lands on the clipboard. */
  value: string;
  /** Visible text. Omit for an icon-only button, which is the table variant. */
  label?: string;
  /** Replaces the label for two seconds after a successful copy. */
  copiedLabel?: string;
  variant?: VariantProps<typeof buttonVariants>["variant"];
  size?: VariantProps<typeof buttonVariants>["size"];
  onCopied?: (value: string) => void;
}

export function CopyButton({
  value,
  label,
  copiedLabel,
  variant = "outline",
  size,
  className,
  onCopied,
  onClick,
  ...props
}: CopyButtonProps): JSX.Element {
  const { t } = useTranslation();
  const { copied, copy } = useCopy();

  const copiedText = copiedLabel ?? String(t("common.copied"));
  const copyText = label ?? String(t("common.copy"));

  const handleCopy = React.useCallback(async () => {
    if (await copy(value)) {
      onCopied?.(value);
    }
  }, [copy, onCopied, value]);

  return (
    <Button
      type="button"
      variant={variant}
      size={size ?? (label ? "sm" : "icon")}
      // The icon-only default sits in table rows, where the 36px icon size is
      // taller than the row it lives in.
      className={cn(!label && size === undefined && "h-8 w-8", className)}
      // Naming the button explicitly keeps the live region below out of its
      // accessible name, and gives the icon-only form a name at all.
      aria-label={copied ? copiedText : copyText}
      onClick={(event) => {
        onClick?.(event);
        void handleCopy();
      }}
      {...props}
    >
      {copied ? (
        <Check weight="bold" className="text-success" aria-hidden="true" />
      ) : (
        <Copy aria-hidden="true" />
      )}
      {label ? <span>{copied ? copiedText : label}</span> : null}
      <span aria-live="polite" className="sr-only">
        {copied ? copiedText : ""}
      </span>
    </Button>
  );
}
