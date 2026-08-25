import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { useModalPresence } from "@/lib/toast-space";
import { cn } from "@/lib/utils";

const Dialog = DialogPrimitive.Root;
const DialogTrigger = DialogPrimitive.Trigger;
const DialogClose = DialogPrimitive.Close;

const DialogOverlay = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      "fixed inset-0 z-50 bg-overlay/60 backdrop-blur-md ease-out",
      "data-[state=open]:animate-in data-[state=open]:fade-in-0",
      "data-[state=closed]:animate-out data-[state=closed]:fade-out-0",
      className,
    )}
    {...props}
  />
));
DialogOverlay.displayName = DialogPrimitive.Overlay.displayName;

export interface DialogContentProps extends React.ComponentPropsWithoutRef<
  typeof DialogPrimitive.Content
> {
  /** Accessible name of the corner close button. Defaults to the t('common.close') text. */
  closeLabel?: string;
  /** Hide the corner close button for flows that must end in an explicit choice. */
  hideClose?: boolean;
  /**
   * Open with the dialog itself focused, rather than the first field in it.
   *
   * What a dialog does otherwise is put the caret in its first text box and
   * select whatever that box already holds. For a box somebody opened in order
   * to type in, that is exactly right. For a form whose first field arrives
   * carrying a value the panel chose - a suggested name - it is not: the caret
   * blinks in a word nobody wrote, the whole of it comes up highlighted, and a
   * form that has only just appeared reads as one already complaining about
   * something.
   *
   * The dialog takes the focus instead, so Escape still closes it, Tab still
   * walks into the form, and a screen reader still announces what opened.
   */
  focusSelf?: boolean;
}

/*
 * The content box is capped at the small viewport height and scrolls inside
 * itself: the client form is taller than a phone in landscape, and a dialog
 * that scrolls the page behind it loses its own footer.
 */
const DialogContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  DialogContentProps
>(({ className, children, closeLabel, hideClose = false, focusSelf = false, ...props }, ref) => {
  const { t } = useTranslation();
  const label = closeLabel ?? String(t("common.close", { defaultValue: "Close" }));
  // Capped at the viewport height above, which on a phone leaves the footer
  // close enough to the bottom edge for the toast stack to cover it.
  const contentRef = useModalPresence(ref);

  return (
    <DialogPrimitive.Portal>
      <DialogOverlay />
      <DialogPrimitive.Content
        ref={contentRef}
        className={cn(
          "fixed left-1/2 top-1/2 z-50 grid w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 gap-4",
          "max-h-[calc(100dvh-2rem)] overflow-y-auto",
          "rounded-lg border border-border bg-card p-5 text-card-foreground shadow-lg sm:p-6",
          "duration-200 ease-out",
          "data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95",
          "data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95",
          className,
        )}
        {...props}
        // After the spread, because this is the panel's answer rather than the
        // caller's: the box below is focusable at tabindex -1 for exactly this,
        // and taking the focus here is what stops it going to the first field.
        onOpenAutoFocus={
          focusSelf
            ? (event) => {
                event.preventDefault();
                (event.currentTarget as HTMLElement | null)?.focus({ preventScroll: true });
              }
            : props.onOpenAutoFocus
        }
      >
        {children}
        {hideClose ? null : (
          <DialogPrimitive.Close
            aria-label={label}
            className={cn(
              "absolute end-3 top-3 rounded-md p-1 text-muted-foreground transition-colors",
              "hover:bg-accent hover:text-accent-foreground",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              "disabled:pointer-events-none",
            )}
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
});
DialogContent.displayName = DialogPrimitive.Content.displayName;

const DialogHeader = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("flex flex-col gap-1.5 pe-8 text-start", className)} {...props} />
);
DialogHeader.displayName = "DialogHeader";

const DialogFooter = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div
    className={cn(
      "flex flex-col-reverse gap-2 sm:flex-row sm:justify-end sm:gap-2 [&>button]:w-full sm:[&>button]:w-auto",
      className,
    )}
    {...props}
  />
);
DialogFooter.displayName = "DialogFooter";

const DialogTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title
    ref={ref}
    className={cn("text-base font-semibold leading-tight tracking-tight", className)}
    {...props}
  />
));
DialogTitle.displayName = DialogPrimitive.Title.displayName;

const DialogDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description
    ref={ref}
    className={cn("text-sm leading-snug text-muted-foreground", className)}
    {...props}
  />
));
DialogDescription.displayName = DialogPrimitive.Description.displayName;

export {
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
  DialogClose,
  DialogOverlay,
};
