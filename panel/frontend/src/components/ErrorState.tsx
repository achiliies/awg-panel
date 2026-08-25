import * as React from "react";
import { ArrowsClockwise, Warning, WifiSlash, type Icon } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { isApiError } from "@/api/client";
import { cn } from "@/lib/utils";

/*
 * The API answers every failure with {detail, errors} where detail is already a
 * finished sentence written for an operator, so that is what gets shown. A
 * value that is not an ApiError came from somewhere the user cannot act on
 * (a render bug, a parse failure), and its message would be a stack trace or an
 * internal string, so it is replaced by the generic line.
 */

export interface ErrorStateProps {
  /** Whatever the query or mutation threw. Unknown on purpose. */
  error?: unknown;
  /** Overrides the derived heading. */
  title?: string;
  /** Overrides the derived explanation. */
  description?: string;
  onRetry?: () => void;
  retryLabel?: string;
  /** Extra actions beside Retry, e.g. a link to the logs. */
  action?: React.ReactNode;
  icon?: Icon;
  /** "page" fills a route body, "inline" fits inside a card. */
  variant?: "page" | "inline";
  className?: string;
}

export function ErrorState({
  error,
  title,
  description,
  onRetry,
  retryLabel,
  action,
  icon,
  variant = "page",
  className,
}: ErrorStateProps): JSX.Element {
  const { t } = useTranslation();

  const apiError = isApiError(error) ? error : null;
  const offline = apiError?.offline ?? false;
  const detail = apiError?.detail.trim() ?? "";
  const fields = Object.entries(apiError?.errors ?? {});

  const heading = title ?? String(t(offline ? "errors.offlineTitle" : "errors.title"));
  const message = description ?? (detail || String(t("errors.generic")));
  const Icon = icon ?? (offline ? WifiSlash : Warning);

  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-center justify-center rounded-lg border border-destructive/30",
        "bg-destructive/5 text-center",
        variant === "page" ? "px-6 py-12" : "px-4 py-8",
        className,
      )}
    >
      <span className="flex h-11 w-11 items-center justify-center rounded-full bg-destructive/10 text-destructive">
        <Icon weight="duotone" className="h-5 w-5" aria-hidden="true" />
      </span>
      <p className="mt-4 text-base font-medium text-foreground">{heading}</p>
      <p className="mt-1.5 max-w-md text-sm leading-relaxed text-muted-foreground">{message}</p>

      {fields.length > 0 ? (
        <ul className="mt-3 max-w-md space-y-1 text-start text-sm text-muted-foreground">
          {fields.map(([field, text]) => (
            <li key={field}>
              <span className="font-medium text-foreground">{field}</span>: {text}
            </li>
          ))}
        </ul>
      ) : null}

      {onRetry || action ? (
        <div className="mt-5 flex flex-wrap justify-center gap-2">
          {onRetry ? (
            <Button variant="outline" size="sm" onClick={onRetry}>
              <ArrowsClockwise aria-hidden="true" />
              {retryLabel ?? String(t("common.retry"))}
            </Button>
          ) : null}
          {action}
        </div>
      ) : null}
    </div>
  );
}
