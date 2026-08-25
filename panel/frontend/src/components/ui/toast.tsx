/* eslint-disable react-refresh/only-export-components -- the hook and the
   provider are one feature; splitting the file would give every page two
   imports for one thing. */
import * as React from "react";
import { CheckCircle, Info, WarningCircle, X } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { useModalOpen } from "@/lib/toast-space";

export type ToastVariant = "default" | "destructive" | "success";

export interface ToastOptions {
  title: string;
  description?: string;
  variant?: ToastVariant;
  /** Milliseconds on screen. 0 keeps it until the operator dismisses it. */
  duration?: number;
}

export interface ToastRecord extends ToastOptions {
  id: string;
}

export interface ToastContextValue {
  /** Queue a notification. Returns its id so a caller can dismiss it early. */
  toast: (options: ToastOptions) => string;
  dismiss: (id: string) => void;
}

const DEFAULT_DURATION = 5000;
/* Beyond four the stack covers the content it is reporting on. */
const MAX_VISIBLE = 4;

interface Timer {
  handle: number | undefined;
  /** What is left to run; recomputed every time the stack is paused. */
  remaining: number;
  startedAt: number;
}

const ToastContext = React.createContext<ToastContextValue | null>(null);

let idCounter = 0;
let missingProviderWarned = false;

/*
 * A missing provider drops notifications instead of throwing: the panel is
 * often the only remote view of a server, and losing the whole page because a
 * success message had nowhere to go is the worse failure.
 */
const NOOP_TOAST: ToastContextValue = {
  toast: () => {
    warnMissingProvider();
    return "";
  },
  dismiss: () => warnMissingProvider(),
};

function warnMissingProvider(): void {
  if (missingProviderWarned) {
    return;
  }
  missingProviderWarned = true;
  console.warn(
    "useToast() was called outside <ToastProvider>, so this notification was dropped. " +
      "Mount <ToastProvider> above the router.",
  );
}

export function useToast(): ToastContextValue {
  const context = React.useContext(ToastContext);
  if (!context) {
    warnMissingProvider();
    return NOOP_TOAST;
  }
  return context;
}

export interface ToastProviderProps {
  children: React.ReactNode;
  /** Accessible name of the per-toast close button. Defaults to t('common.dismiss'). */
  dismissLabel?: string;
  /** Accessible name of the stack itself. Defaults to t('toast.region'). */
  regionLabel?: string;
}

export function ToastProvider({
  children,
  dismissLabel,
  regionLabel,
}: ToastProviderProps): JSX.Element {
  const { t } = useTranslation();
  const [toasts, setToasts] = React.useState<ToastRecord[]>([]);
  const timers = React.useRef(new Map<string, Timer>());
  const paused = React.useRef(false);
  // A dialog owns the bottom edge on a short window, and its buttons are the
  // only ones on screen while it is up, so the stack gets out of the way
  // wholesale rather than trying to squeeze in beside it.
  const modalOpen = useModalOpen();

  const dismiss = React.useCallback((id: string) => {
    const timer = timers.current.get(id);
    if (timer?.handle !== undefined) {
      window.clearTimeout(timer.handle);
    }
    timers.current.delete(id);
    setToasts((current) => current.filter((item) => item.id !== id));
  }, []);

  const startTimer = React.useCallback(
    (id: string) => {
      const timer = timers.current.get(id);
      if (!timer || timer.handle !== undefined || timer.remaining <= 0) {
        return;
      }
      timer.startedAt = Date.now();
      timer.handle = window.setTimeout(() => dismiss(id), timer.remaining);
    },
    [dismiss],
  );

  const toast = React.useCallback(
    (options: ToastOptions) => {
      idCounter += 1;
      const id = `toast-${idCounter}`;
      const duration = options.duration ?? DEFAULT_DURATION;

      if (duration > 0) {
        timers.current.set(id, { handle: undefined, remaining: duration, startedAt: 0 });
      }
      setToasts((current) => {
        const next = [...current, { ...options, id }];
        return next.length > MAX_VISIBLE ? next.slice(next.length - MAX_VISIBLE) : next;
      });
      // A toast queued while the pointer rests on the stack must not start its
      // clock, or it disappears the moment the pointer leaves.
      if (!paused.current) {
        startTimer(id);
      }
      return id;
    },
    [startTimer],
  );

  const pause = React.useCallback(() => {
    paused.current = true;
    const now = Date.now();
    for (const timer of timers.current.values()) {
      if (timer.handle === undefined) {
        continue;
      }
      window.clearTimeout(timer.handle);
      timer.handle = undefined;
      timer.remaining = Math.max(0, timer.remaining - (now - timer.startedAt));
    }
  }, []);

  const resume = React.useCallback(() => {
    paused.current = false;
    for (const id of Array.from(timers.current.keys())) {
      startTimer(id);
    }
  }, [startTimer]);

  React.useEffect(() => {
    // Drop timers whose toast was pushed off the top of a full stack.
    const live = new Set(toasts.map((item) => item.id));
    for (const [id, timer] of Array.from(timers.current.entries())) {
      if (!live.has(id)) {
        if (timer.handle !== undefined) {
          window.clearTimeout(timer.handle);
        }
        timers.current.delete(id);
      }
    }
  }, [toasts]);

  React.useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const timer of pending.values()) {
        if (timer.handle !== undefined) {
          window.clearTimeout(timer.handle);
        }
      }
      pending.clear();
    };
  }, []);

  const value = React.useMemo<ToastContextValue>(() => ({ toast, dismiss }), [toast, dismiss]);
  const closeText = dismissLabel ?? String(t("common.dismiss", { defaultValue: "Dismiss" }));
  const regionText = regionLabel ?? String(t("toast.region", { defaultValue: "Notifications" }));

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        role="region"
        aria-label={regionText}
        // The wrapper stays click-through so it cannot swallow taps on the page
        // underneath; each toast turns pointer events back on for itself.
        className={cn(
          "pointer-events-none fixed inset-x-0 z-[100] flex flex-col gap-2 p-3",
          "sm:inset-x-auto sm:end-4 sm:w-full sm:max-w-sm sm:p-0",
          // --toast-inset-bottom is whatever the page has pinned to the bottom
          // edge, so the stack rides above a save bar instead of over it. Most
          // pages reserve nothing, which is what the 0px fallback is for.
          modalOpen
            ? "top-0 sm:top-4"
            : "bottom-[var(--toast-inset-bottom,0px)] sm:bottom-[calc(var(--toast-inset-bottom,0px)_+_1rem)]",
        )}
        onMouseEnter={pause}
        onMouseLeave={resume}
        onFocusCapture={pause}
        onBlurCapture={resume}
      >
        {toasts.map((item) => (
          <ToastItem
            key={item.id}
            toast={item}
            closeLabel={closeText}
            onDismiss={dismiss}
            fromTop={modalOpen}
          />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

const VARIANT_CLASSES: Record<ToastVariant, string> = {
  default: "border-s-info",
  success: "border-s-success",
  destructive: "border-s-destructive",
};

const VARIANT_ICON_CLASSES: Record<ToastVariant, string> = {
  default: "text-info",
  success: "text-success",
  destructive: "text-destructive",
};

const VARIANT_ICONS: Record<ToastVariant, typeof Info> = {
  default: Info,
  success: CheckCircle,
  destructive: WarningCircle,
};

interface ToastItemProps {
  toast: ToastRecord;
  closeLabel: string;
  onDismiss: (id: string) => void;
  /** The stack has moved to the top edge, so it should arrive from there. */
  fromTop: boolean;
}

function ToastItem({ toast, closeLabel, onDismiss, fromTop }: ToastItemProps): JSX.Element {
  const variant = toast.variant ?? "default";
  const Icon = VARIANT_ICONS[variant];
  const isError = variant === "destructive";

  return (
    <div
      // Errors interrupt; confirmations wait for a pause in the screen reader.
      role={isError ? "alert" : "status"}
      aria-live={isError ? "assertive" : "polite"}
      className={cn(
        "pointer-events-auto flex items-start gap-3 rounded-lg border border-border bg-card p-3",
        "text-card-foreground shadow-lg border-s-4",
        "animate-in fade-in-0 duration-200",
        fromTop ? "slide-in-from-top-2" : "slide-in-from-bottom-2",
        VARIANT_CLASSES[variant],
      )}
    >
      <Icon
        // Filled: a toast arrives unasked and leaves on its own, so its one
        // glyph has to land the outcome before the reader has focused on it.
        weight="fill"
        className={cn("mt-0.5 h-5 w-5 shrink-0", VARIANT_ICON_CLASSES[variant])}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium leading-tight">{toast.title}</p>
        {toast.description ? (
          <p className="mt-1 break-words text-sm leading-snug text-muted-foreground">
            {toast.description}
          </p>
        ) : null}
      </div>
      <button
        type="button"
        onClick={() => onDismiss(toast.id)}
        aria-label={closeLabel}
        className={cn(
          "-me-1 -mt-1 shrink-0 rounded-md p-1 text-muted-foreground transition-colors",
          "hover:bg-accent hover:text-accent-foreground",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        )}
      >
        <X className="h-4 w-4" aria-hidden="true" />
      </button>
    </div>
  );
}
