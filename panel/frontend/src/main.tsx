import { Component, StrictMode, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ArrowsClockwise, Warning } from "@/lib/icons";

import "@/i18n";
import App from "@/App";
import { isApiError } from "@/api/client";
import { installFavicon } from "@/lib/brand";
import { ThemeProvider } from "@/theme/ThemeProvider";
import { ToastProvider } from "@/components/ui/toast";
import { TooltipProvider } from "@/components/ui/tooltip";
import "./index.css";

/**
 * How long to wait before repeating a mutation the config lock turned away.
 *
 * The same five seconds the API names in the `Retry-After` it sends with that
 * refusal. Read from the constant rather than the header because the two would
 * only ever differ by somebody changing the server's mind about it, and a wait
 * of the wrong length costs a moment where reading the header costs a field on
 * every error the panel can raise.
 */
const LOCKED_RETRY_MS = 5000;

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // One retry covers a dropped connection while the tunnel restarts;
      // more than that only delays the error state the pages already render.
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5000,
    },
    mutations: {
      /**
       * Once, and only for the config lock having been busy.
       *
       * Every writer of the configuration queues on one flock - this panel's
       * two workers, its collector, and `awg-panel manage` from a root
       * shell - and a request
       * that waits out its budget there is answered 503 without having written
       * anything. Left unretried that is an admin who pressed a button, waited,
       * and was told to press it again, for a queue that clears in well under a
       * second; the operation is not lost, but nothing resumes it either.
       *
       * `retryable` is what decides, and what makes this safe to apply to every
       * mutation rather than to the idempotent ones: it is true only for the
       * failure that provably happened before the files were touched. A 4xx is
       * an answer, a 502 is a change that may be half applied, and both are
       * shown to the admin exactly as they were.
       */
      retry: (failureCount, error) => failureCount < 1 && isApiError(error) && error.retryable,
      retryDelay: LOCKED_RETRY_MS,
    },
  },
});

function CrashScreen({ error }: { error: Error }): JSX.Element {
  const { t } = useTranslation();
  // The boundary must stay readable even if i18n is the thing that failed, so
  // every string carries its English original.
  const text = (key: string, fallback: string): string => {
    const value = t(key, { defaultValue: fallback });
    return typeof value === "string" && value.length > 0 && value !== key ? value : fallback;
  };

  return (
    <div className="flex min-h-dvh items-center justify-center bg-background p-6">
      <div
        role="alert"
        className="w-full max-w-lg rounded-lg border border-border bg-card p-6 text-card-foreground shadow-sm"
      >
        <div className="flex items-start gap-3">
          <Warning
            weight="fill"
            className="mt-0.5 h-5 w-5 shrink-0 text-destructive"
            aria-hidden="true"
          />
          <div className="min-w-0">
            <h1 className="text-lg font-semibold tracking-tight">
              {text("error.boundary.title", "The panel hit an unexpected error")}
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {text(
                "error.boundary.body",
                "Your VPN keeps running. Reload the page; if this keeps happening, check the panel service logs.",
              )}
            </p>
          </div>
        </div>

        <pre className="mt-4 max-h-40 overflow-auto rounded-md bg-muted p-3 font-mono text-xs leading-relaxed text-muted-foreground">
          {error.message || String(error)}
        </pre>

        <div className="mt-4 flex justify-end">
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <ArrowsClockwise className="h-4 w-4" aria-hidden="true" />
            {text("error.boundary.reload", "Reload")}
          </button>
        </div>
      </div>
    </div>
  );
}

interface ErrorBoundaryState {
  error: Error | null;
}

class ErrorBoundary extends Component<{ children: ReactNode }, ErrorBoundaryState> {
  override state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // The panel is often the only remote view of the server; a swallowed render
    // error would look exactly like an outage, so leave a trace for support.
    console.error("AWG Panel render error", error, info.componentStack);
  }

  override render(): ReactNode {
    return this.state.error ? <CrashScreen error={this.state.error} /> : this.props.children;
  }
}

const container = document.getElementById("root");
if (!container) {
  throw new Error("index.html is missing its #root element");
}

// Before the first render, and outside the boundary: a panel that has crashed
// is still the panel, and the tab it left behind should still say so.
installFavicon();

createRoot(container).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <TooltipProvider>
            <ToastProvider>
              <App />
            </ToastProvider>
          </TooltipProvider>
        </ThemeProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);
