import * as React from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";

import { useToast } from "@/components/ui/toast";
import { useSaveServer, useServer, useServerParams } from "@/api/hooks";
import { usePendingIndicator } from "@/lib/pending";
import { useReveal } from "@/lib/reveal";
import type {
  FieldErrors,
  ParamPreview,
  ParamSpec,
  ServerConfig as ServerConfigPayload,
  ServerSaveResult,
  ServerUpdateInput,
} from "@/api/types";

/*
 * The editing half of the server config, shared by the two pages that do it.
 *
 * Server and Obfuscation are one config behind one PUT, split across two pages
 * because they are two jobs: where the tunnel listens is set once and left
 * alone, while what the traffic looks like is drawn, compared and redrawn. What
 * they cannot be is two implementations of the same form - a draft that reverts
 * differently, or a save bar that states a different cost for the same change,
 * is a bug that only shows up on one of the two pages.
 *
 * So the draft, the validation, the cost model and the save live here, and a
 * page says only which groups it owns. Owning a group is what decides what a
 * page saves: `changed` is computed against that page's groups alone, so an
 * unsaved edit left on the other one is neither sent nor silently discarded.
 *
 * Saving applies. Contract 11a says an obfuscation change - and the port, the
 * address and the MTU - needs a full interface restart, so the API performs one
 * as part of the save; a setting that is written but not in force is the worst
 * of the three possible states, because everything says success and nothing
 * changed. What the save bar owes the operator is therefore the cost up front:
 * whether the tunnel is about to drop for a second, and whether every client
 * will need a new config afterwards.
 */

/**
 * How one setting travels over the wire.
 *
 * GET server returns the network settings as named fields rather than inside
 * `params`, and PUT server takes them back the same way, so the form has to
 * bind those eight by name. `restart` and `reimport` mirror contract 11a, which
 * is what lets the save bar state the cost of a change before it is made; every
 * other parameter is obfuscation and costs both.
 */
interface WireBinding {
  /** Field name the API reports a validation error against. */
  field: string;
  read: (server: ServerConfigPayload) => string;
  write: (payload: ServerUpdateInput, value: string) => void;
  restart: boolean;
  reimport: boolean;
}

/** Ports and the MTU are numbers on the wire; an unparsable one is simply not sent. */
function writeNumber(value: string): number | undefined {
  const parsed = Number.parseInt(value.trim(), 10);
  return Number.isFinite(parsed) ? parsed : undefined;
}

const WIRE: Readonly<Record<string, WireBinding>> = {
  ListenPort: {
    field: "listenPort",
    read: (server) => (server.listenPort > 0 ? String(server.listenPort) : ""),
    write: (payload, value) => {
      const port = writeNumber(value);
      if (port !== undefined) {
        payload.listenPort = port;
      }
    },
    restart: true,
    reimport: true,
  },
  Address: {
    field: "address",
    read: (server) => server.address,
    write: (payload, value) => {
      payload.address = value;
    },
    restart: true,
    reimport: true,
  },
  MTU: {
    field: "mtu",
    read: (server) => (server.mtu > 0 ? String(server.mtu) : ""),
    write: (payload, value) => {
      const mtu = writeNumber(value);
      if (mtu !== undefined) {
        payload.mtu = mtu;
      }
    },
    restart: true,
    reimport: true,
  },
  DNS: {
    field: "dns",
    read: (server) => server.dns,
    write: (payload, value) => {
      payload.dns = value;
    },
    restart: false,
    reimport: true,
  },
  EndpointHost: {
    field: "endpointHost",
    read: (server) => server.endpointHost,
    write: (payload, value) => {
      payload.endpointHost = value;
    },
    restart: false,
    reimport: true,
  },
  EndpointPort: {
    field: "endpointPort",
    read: (server) => server.endpointPort,
    write: (payload, value) => {
      payload.endpointPort = value;
    },
    restart: false,
    reimport: true,
  },
  AllowedIPs: {
    field: "allowedIpsDefault",
    read: (server) => server.allowedIpsDefault,
    write: (payload, value) => {
      payload.allowedIpsDefault = value;
    },
    restart: false,
    reimport: true,
  },
  PersistentKeepalive: {
    field: "keepalive",
    read: (server) => server.keepalive,
    write: (payload, value) => {
      payload.keepalive = value;
    },
    restart: false,
    reimport: true,
  },
};

const NO_VALUES: Readonly<Record<string, string>> = {};

/** Every catalog key with the value the server currently has for it. */
function readValues(
  server: ServerConfigPayload,
  specs: ParamSpec[],
): Readonly<Record<string, string>> {
  const values: Record<string, string> = {};
  for (const spec of specs) {
    const wire = WIRE[spec.key];
    values[spec.key] = wire ? wire.read(server) : (server.params[spec.key] ?? "");
  }
  return values;
}

/**
 * Does this look like the server's address line at all?
 *
 * One address with a prefix, or two separated by a comma: a dual-stack server's
 * line reads `10.13.0.1/20, 2a0a:...:1::1/64`, and the field shows back exactly
 * what the config holds. Only the shape is checked - which family each half is,
 * how wide a prefix may be and where the server has to sit inside it are the
 * API's rules, and it words them far better than a regex here could.
 */
function isAddressLine(value: string): boolean {
  const entry = /^(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]+)\/\d{1,3}$/;
  return value.split(",").every((part) => entry.test(part.trim()));
}

/**
 * The checks worth doing in the browser: a value that cannot even be put on the
 * wire, and a bound the catalog already states. Everything else - overlapping
 * header ranges, malformed imitation tags, what this build of the module
 * supports - belongs to the API, which owns the rules and the wording.
 */
function checkLocally(spec: ParamSpec, raw: string, t: TFunction): string | undefined {
  const value = raw.trim();
  if (!value) {
    return spec.optional ? undefined : String(t("validation.required"));
  }

  if (spec.kind === "int" || spec.kind === "port") {
    if (!/^\d+$/.test(value)) {
      return String(t("validation.number"));
    }
    const number = Number.parseInt(value, 10);
    if (spec.min !== null && spec.max !== null && (number < spec.min || number > spec.max)) {
      return String(t("validation.range", { min: spec.min, max: spec.max }));
    }
    return undefined;
  }

  if (spec.kind === "range") {
    // `<` rather than `<=`: the API accepts a range whose ends are equal, and a
    // field that refuses what a save would take is a field that invents a rule.
    // It matters more now than it did - the five timers joined this path, and a
    // hand-written config full of single values reaches it as `120-120` the
    // moment somebody edits one end of it.
    const match = /^(\d+)(?:-(\d+))?$/.exec(value);
    if (!match || (match[2] !== undefined && Number(match[2]) < Number(match[1]))) {
      return String(
        t("server.rangeInvalid", {
          defaultValue:
            "Enter a whole number, or a range like 10-500 with the smaller number first.",
        }),
      );
    }
    return undefined;
  }

  if (spec.kind === "cidr" && !isAddressLine(value)) {
    return String(t("validation.cidr"));
  }

  return undefined;
}

function buildPayload(
  changed: readonly string[],
  values: Readonly<Record<string, string>>,
): ServerUpdateInput {
  const payload: ServerUpdateInput = {};
  const params: Record<string, string> = {};

  for (const key of changed) {
    const value = values[key] ?? "";
    const wire = WIRE[key];
    if (wire) {
      wire.write(payload, value);
    } else {
      params[key] = value;
    }
  }
  if (Object.keys(params).length > 0) {
    payload.params = params;
  }
  return payload;
}

export interface ServerForm {
  catalog: ReturnType<typeof useServerParams>;
  server: ReturnType<typeof useServer>;
  /** The whole catalog, so a card can pick its own groups out of it. */
  specs: ParamSpec[];
  /** Every catalog key, holding the draft where there is one and the server otherwise. */
  values: Readonly<Record<string, string>>;
  /** Keys this page owns that differ from the server. Nothing else is ever sent. */
  changed: readonly string[];
  dirty: boolean;
  saving: boolean;
  /** What applying the pending edits costs, by the rules in contract 11a. */
  cost: { needsRestart: boolean; mustReimport: boolean };
  /** The last save, but only when it has something to show for itself. */
  notes: ServerSaveResult | null;
  /** Attach to the notes block: it scrolls into view when one arrives. */
  notesRef: React.RefObject<HTMLDivElement>;
  errorFor: (key: string) => string | undefined;
  change: (key: string, value: string) => void;
  revert: (key: string) => void;
  /** Drop a whole generated set into the draft. Nothing is saved by doing it. */
  fill: (values: ParamPreview) => void;
  discard: () => void;
  dismissNotes: () => void;
  save: () => void;
}

/**
 * The draft, the rules and the save for one page's share of the server config.
 *
 * `owns` names the catalog groups the calling page renders. A group nobody owns
 * is still readable - the values map covers the whole catalog, because the
 * cross-field rules the API enforces need it - but it can never be saved from
 * here.
 */
export function useServerForm(owns: ReadonlySet<string>): ServerForm {
  const { t } = useTranslation();
  const { toast } = useToast();

  const catalog = useServerParams();
  const server = useServer();
  const saveServer = useSaveServer();
  /* Held open past the response: onSuccess drops the draft, and the bar the
     spinner lives in goes with it. See usePendingIndicator. */
  const saving = usePendingIndicator(saveServer.isPending);

  /** null means "no local edits": the form is showing exactly what the server has. */
  const [draft, setDraft] = React.useState<Record<string, string> | null>(null);
  const [formErrors, setFormErrors] = React.useState<Record<string, string>>({});
  const [apiErrors, setApiErrors] = React.useState<FieldErrors>({});
  /** What the last save cost, which is what the banners are about. */
  const [outcome, setOutcome] = React.useState<ServerSaveResult | null>(null);

  const specs = React.useMemo(() => catalog.data ?? [], [catalog.data]);
  const editable = React.useMemo(() => specs.filter((spec) => owns.has(spec.group)), [specs, owns]);

  const baseline = React.useMemo(
    () => (server.data ? readValues(server.data, specs) : null),
    [server.data, specs],
  );
  const values = draft ?? baseline ?? NO_VALUES;

  const changed = React.useMemo(() => {
    if (!draft || !baseline) {
      return [];
    }
    return editable
      .filter((spec) => (draft[spec.key] ?? "") !== (baseline[spec.key] ?? ""))
      .map((spec) => spec.key);
  }, [draft, baseline, editable]);
  const dirty = changed.length > 0;

  // Leaving with unsaved obfuscation edits is the one mistake on these pages
  // that is genuinely annoying to redo, so the browser asks.
  React.useEffect(() => {
    if (!dirty) {
      return;
    }
    const warn = (event: BeforeUnloadEvent): void => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const cost = React.useMemo(() => {
    let needsRestart = false;
    let mustReimport = false;
    for (const key of changed) {
      const wire = WIRE[key];
      // An obfuscation parameter, or anything the wire table does not know:
      // both ends have to agree on it, and the interface has to be restarted.
      needsRestart = needsRestart || (wire ? wire.restart : true);
      mustReimport = mustReimport || (wire ? wire.reimport : true);
    }
    return { needsRestart, mustReimport };
  }, [changed]);

  const errorFor = React.useCallback(
    (key: string): string | undefined =>
      formErrors[key] ?? apiErrors[key] ?? apiErrors[WIRE[key]?.field ?? key],
    [formErrors, apiErrors],
  );

  const change = React.useCallback(
    (key: string, value: string) => {
      setDraft((current) => ({ ...(current ?? baseline ?? {}), [key]: value }));
      // The old message described the old value; keeping it on screen while the
      // field changes underneath is worse than saying nothing.
      setFormErrors((current) => {
        if (!(key in current)) {
          return current;
        }
        const next = { ...current };
        delete next[key];
        return next;
      });
    },
    [baseline],
  );

  const revert = React.useCallback(
    (key: string) => {
      if (baseline) {
        change(key, baseline[key] ?? "");
      }
    },
    [baseline, change],
  );

  const fill = React.useCallback(
    (next: ParamPreview) => {
      setDraft((current) => ({ ...(current ?? baseline ?? {}), ...next }));
      setFormErrors({});
    },
    [baseline],
  );

  const discard = React.useCallback(() => {
    setDraft(null);
    setFormErrors({});
    setApiErrors({});
  }, []);

  const dismissNotes = React.useCallback(() => setOutcome(null), []);

  const save = React.useCallback(() => {
    const problems: Record<string, string> = {};
    for (const key of changed) {
      const spec = editable.find((candidate) => candidate.key === key);
      const message = spec ? checkLocally(spec, values[key] ?? "", t) : undefined;
      if (message) {
        problems[key] = message;
      }
    }
    setFormErrors(problems);
    if (Object.keys(problems).length > 0) {
      toast({
        title: String(t("errors.validation")),
        description: String(t("errors.validationBody")),
        variant: "destructive",
      });
      return;
    }

    saveServer.mutate(buildPayload(changed, values), {
      onSuccess: (result) => {
        setDraft(null);
        setApiErrors({});
        setFormErrors({});
        setOutcome(result);
        /*
         * A restart is the loudest thing these pages do, so when one has just
         * happened the toast says so rather than talking about backups - and
         * when one was due and did not happen, it says that instead. A green
         * "saved" on a tunnel still running the previous settings is how an
         * admin walks away from a change that is only on disk; the notice that
         * explains it, with the Restart button in it, is at the top of the page
         * and they pressed Save at the bottom.
         */
        toast(
          result.needsRestart && !result.applied
            ? {
                title: String(t("server.restartPending")),
                description: String(t("server.restartPendingBody")),
              }
            : {
                title: String(t("server.saved")),
                description: String(
                  result.needsRestart ? t("server.savedRestarted") : t("server.savedBody"),
                ),
                variant: "success",
              },
        );
      },
      onError: (error) => {
        setOutcome(null);
        setApiErrors(error.errors);
        toast({
          title: String(t("errors.saveFailed")),
          description: error.detail,
          variant: "destructive",
        });
      },
    });
  }, [changed, editable, values, saveServer, t, toast]);

  /** The same outcome, but only when it has a banner to draw. */
  const notes =
    outcome &&
    (outcome.mustReimport ||
      (outcome.needsRestart && !outcome.applied) ||
      outcome.warnings.length > 0)
      ? outcome
      : null;
  const notesRef = useReveal<HTMLDivElement>(notes);

  return {
    catalog,
    server,
    specs,
    values,
    changed,
    dirty,
    saving,
    cost,
    notes,
    notesRef,
    errorFor,
    change,
    revert,
    fill,
    discard,
    dismissNotes,
    save,
  };
}
