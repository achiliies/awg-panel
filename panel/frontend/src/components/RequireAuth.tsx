import * as React from "react";
import { Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ErrorState } from "@/components/ErrorState";
import { Spinner } from "@/components/ui/spinner";
import { UNAUTHORIZED_EVENT } from "@/api/client";
import { useSession } from "@/api/hooks";

/*
 * The gate in front of every route except /login.
 *
 * Two ways a session ends, and both have to land on the login page without
 * losing the user's place:
 *
 * - This component asks who the visitor is on mount. The endpoint answers 200
 *   with authenticated:false when there is no session, so "logged out" is a
 *   result, not an error.
 * - A background poll (live stats every two seconds) gets a 401 after the
 *   session expires. api/client dispatches UNAUTHORIZED_EVENT for exactly that
 *   case, because a query deep in the tree has no router access.
 */

export interface RequireAuthProps {
  /** Omit to gate a nested route tree through <Outlet />. */
  children?: React.ReactNode;
}

export function RequireAuth({ children }: RequireAuthProps): JSX.Element {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const session = useSession();

  // Read in the event handler, which must not re-subscribe on every navigation.
  const locationRef = React.useRef(location);
  locationRef.current = location;

  React.useEffect(() => {
    const handleUnauthorized = (): void => {
      const from = locationRef.current;
      if (from.pathname === "/login") {
        return;
      }
      // Everything, not just the session key. An expiry ends the session
      // exactly as a sign-out does, and useLogout drops the whole cache on the
      // grounds that peer names, addresses and traffic are not for the next
      // visitor to read out of one - which is a policy about who is at the
      // browser, and says nothing about how the session came to be over. This
      // path used to invalidate the session alone and leave the rest, so the
      // session that ended by being walked away from was the one that kept its
      // data.
      //
      // Dropping the session query rather than refetching it still answers
      // what that did: a Back navigation remounts this component with nothing
      // cached, so it asks the server who the visitor is instead of rendering
      // pages off a cached "authenticated" that will only 401 again.
      queryClient.clear();
      navigate("/login", { replace: true, state: redirectState(from) });
    };

    window.addEventListener(UNAUTHORIZED_EVENT, handleUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, handleUnauthorized);
  }, [navigate, queryClient]);

  if (session.isPending) {
    return (
      // Picks up exactly where the boot ring in index.html left off: same ring,
      // same size, same point on the screen. The label hangs off the ring
      // rather than sharing a column with it, because centring the pair would
      // shift the ring upwards at the moment React takes over and turn one
      // continuous wait into two.
      <div className="flex min-h-dvh items-center justify-center bg-background p-6">
        <div className="relative">
          <Spinner size="lg" className="text-primary" label={String(t("auth.checking"))} />
          <p className="absolute left-1/2 top-full mt-3 w-max max-w-[80vw] -translate-x-1/2 text-center text-sm text-muted-foreground duration-500 animate-in fade-in">
            {t("auth.checking")}
          </p>
        </div>
      </div>
    );
  }

  if (session.isError) {
    if (session.error.unauthorized) {
      return <Navigate to="/login" replace state={redirectState(location)} />;
    }
    // Anything else (panel restarting, proxy in the way) is worth showing:
    // bouncing to a login form that also cannot load would hide the cause.
    return (
      <div className="flex min-h-dvh items-center justify-center bg-background p-6">
        <ErrorState
          error={session.error}
          title={String(t("auth.checkFailed"))}
          onRetry={() => void session.refetch()}
          className="w-full max-w-md"
        />
      </div>
    );
  }

  if (!session.data.authenticated) {
    return <Navigate to="/login" replace state={redirectState(location)} />;
  }

  return <>{children ?? <Outlet />}</>;
}

/** Enough of a router Location for the login page to send the user back. */
export interface RedirectOrigin {
  pathname: string;
  search: string;
  hash: string;
}

/**
 * What /login receives as history state. `from` matches the router's own
 * convention (`location.state.from.pathname`); `next` is the same target as a
 * plain path for a login page that would rather have a string.
 */
export interface RedirectState {
  from: RedirectOrigin;
  next: string;
}

function redirectState(location: RedirectOrigin): RedirectState {
  return {
    from: { pathname: location.pathname, search: location.search, hash: location.hash },
    next: `${location.pathname}${location.search}`,
  };
}
