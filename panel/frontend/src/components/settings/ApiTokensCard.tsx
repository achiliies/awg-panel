import * as React from "react";
import { useTranslation } from "react-i18next";

import { CopyButton } from "@/components/CopyButton";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { Notice } from "@/components/server/Notice";
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
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useApiTokens, useCreateApiToken, useRevokeApiToken, useUpdateApiToken } from "@/api/hooks";
import { Key, PencilSimple, Plus, Shuffle, Trash, Warning } from "@/lib/icons";
import { randomName, replaceSuggestion, typedCharacter } from "@/lib/names";
import { cn, formatDateTime, relativeParts } from "@/lib/utils";
import type { ApiError, ApiToken } from "@/api/types";

/*
 * The credentials that are not a person: what can reach this panel with a
 * header instead of a password, and the controls for issuing and ending one.
 *
 * The card is built around the one thing about a token that cannot be undone.
 * Its secret exists in exactly one response - the one that creates it - because
 * the server keeps only a hash, so the panel shows it once, says plainly that
 * this is the only time, and offers a copy button rather than a line of text to
 * select by hand. Everything else here is recoverable; that moment is not.
 *
 * Each row says the four things that decide whether a token should still exist:
 * what it is called, when it was last used, when it runs out, and whether using
 * it pushes that out again. The name is the load-bearing one - it is what the
 * activity log writes beside everything the token does, so "nightly backup" in
 * this list is "nightly backup" in the log a month later, and a token nobody
 * named would leave that log saying "admin" for work nobody was present for.
 *
 * Which is why the create form opens with a name already drawn instead of an
 * empty box. A required field that has to be invented before anything else can
 * happen is answered with "test", and "test" in the log a month later is worth
 * no more than nothing. Nine random characters are a poorer label than "nightly
 * backup" and a far better one than a word somebody typed to get past a form -
 * they tell two rows apart, which is the whole job, and the box is there to be
 * typed over by anyone who has a better answer.
 *
 * Renewal is off by default, and that default is the honest one: a token that
 * quietly renews itself for ever is a token with an expiry only on paper. Turned
 * on, it means what an operator usually wants from a long-lived credential - the
 * one still in daily use keeps working, and the one whose script was
 * decommissioned dies on schedule.
 */

interface ApiTokensCardProps {
  /** t() with an English original, so a key the catalog lacks never shows raw. */
  text: (key: string, fallback: string, vars?: Record<string, string | number>) => string;
}

/** The expiries the form offers, in seconds. 0 is "never", which is a choice. */
const LIFETIMES = [
  { value: 7 * 86400, key: "settings.tokenExpiry7d", fallback: "7 days" },
  { value: 30 * 86400, key: "settings.tokenExpiry30d", fallback: "30 days" },
  { value: 90 * 86400, key: "settings.tokenExpiry90d", fallback: "90 days" },
  { value: 365 * 86400, key: "settings.tokenExpiry1y", fallback: "1 year" },
  { value: 0, key: "settings.tokenExpiryNever", fallback: "Never" },
] as const;

const DEFAULT_LIFETIME = 30 * 86400;

export function ApiTokensCard({ text }: ApiTokensCardProps): JSX.Element {
  const { t } = useTranslation();
  const { toast } = useToast();
  const tokens = useApiTokens();
  const create = useCreateApiToken();
  const update = useUpdateApiToken();
  const revoke = useRevokeApiToken();

  const [createOpen, setCreateOpen] = React.useState(false);
  /** The token whose name and renewal are being changed, or null. */
  const [editing, setEditing] = React.useState<ApiToken | null>(null);
  /** The row whose revoke confirmation is open. */
  const [revoking, setRevoking] = React.useState<ApiToken | null>(null);
  /**
   * The secret, for as long as the dialog showing it is open. Held here rather
   * than in the query cache on purpose: it is not part of any token's state, it
   * is a thing that happened once, and it must not survive a refetch or a
   * re-render into somewhere it can be read again.
   */
  const [issued, setIssued] = React.useState<{ token: ApiToken; secret: string } | null>(null);

  const rows = tokens.data ?? [];
  const busy = create.isPending || update.isPending || revoke.isPending;

  const endOne = (token: ApiToken): void => {
    revoke.mutate(token.id, {
      onSuccess: () => {
        setRevoking(null);
        toast({
          title: text("settings.tokenRevoked", "Token revoked"),
          description: text(
            "settings.tokenRevokedBody",
            "Anything still using {{name}} is refused from its very next request.",
            { name: token.name },
          ),
          variant: "success",
        });
      },
      onError: (error) => {
        setRevoking(null);
        toast({
          title: text("settings.tokenRevokeFailed", "Could not revoke that token"),
          description: error.detail,
          variant: "destructive",
        });
      },
    });
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle>{text("settings.tokens", "API tokens")}</CardTitle>
            <CardDescription className="mt-1.5">
              {text(
                "settings.tokensHint",
                "For scripts and monitoring, which have no browser to sign in with. A token can do everything this panel can except change the credentials that authorise it - it cannot issue another token, change the password, turn two-factor off, or restore a backup over them. It can download one, though, and that archive carries the panel's database: treat a token that has fetched a backup exactly like the password.",
              )}
            </CardDescription>
          </div>
          <Button size="sm" disabled={busy} onClick={() => setCreateOpen(true)}>
            <Plus aria-hidden="true" />
            {text("settings.tokenNew", "New token")}
          </Button>
        </div>
      </CardHeader>

      <CardContent>
        {tokens.isPending ? (
          <LoadingRows />
        ) : tokens.isError ? (
          <ErrorState
            variant="inline"
            error={tokens.error}
            title={text("errors.loadFailed", "Could not load {{what}}", {
              what: text("settings.tokens", "API tokens").toLowerCase(),
            })}
            onRetry={() => void tokens.refetch()}
          />
        ) : rows.length === 0 ? (
          <EmptyState
            variant="inline"
            icon={Key}
            title={String(text("settings.tokensEmpty", "No API tokens"))}
            description={String(
              text(
                "settings.tokensEmptyHint",
                "Nothing can reach this panel without signing in at the login page.",
              ),
            )}
          />
        ) : (
          <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border">
            {rows.map((token) => (
              <TokenRow
                key={token.id}
                token={token}
                busy={busy}
                text={text}
                onEdit={() => setEditing(token)}
                onRevoke={() => setRevoking(token)}
              />
            ))}
          </ul>
        )}
      </CardContent>

      <CreateDialog
        open={createOpen}
        busy={create.isPending}
        text={text}
        onOpenChange={(open) => {
          if (!open && !create.isPending) {
            setCreateOpen(false);
            create.reset();
          }
        }}
        onSubmit={(input) =>
          create.mutate(input, {
            onSuccess: (result) => {
              setCreateOpen(false);
              setIssued(result);
              // The mutation keeps its last result until it is reset, and that
              // result is the secret. Closing the dialog from here never goes
              // through onOpenChange, so without this the one copy the comment
              // on `issued` is about would sit beside a second one in the query
              // client for as long as this page stays open.
              create.reset();
            },
          })
        }
        error={create.error}
      />

      <SecretDialog issued={issued} text={text} onClose={() => setIssued(null)} />

      <EditDialog
        token={editing}
        busy={update.isPending}
        text={text}
        error={update.error}
        onClose={() => {
          setEditing(null);
          update.reset();
        }}
        onSubmit={(changes) =>
          update.mutate(changes, {
            onSuccess: () => {
              setEditing(null);
              toast({
                title: text("settings.tokenSaved", "Token updated"),
                description: text(
                  "settings.tokenSavedBody",
                  "The secret is unchanged: whatever holds it goes on working.",
                ),
                variant: "success",
              });
            },
          })
        }
      />

      <AlertDialog
        open={revoking !== null}
        onOpenChange={(open) => {
          if (!open && !revoke.isPending) {
            setRevoking(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {text("settings.tokenRevokeConfirm", "Revoke {{name}}?", {
                name: revoking?.name ?? "",
              })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {text(
                "settings.tokenRevokeConfirmBody",
                "Whatever is using this token stops working at once, and there is no way to bring it back - a replacement is a new token with a new secret. Nothing on the server changes and no VPN client is disconnected.",
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={revoke.isPending}>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className={cn(
                buttonVariants({ variant: "destructive" }),
                // Disabled but at full strength while the request runs: dimming
                // it hides the spinner that is the only sign of progress.
                revoke.isPending && "cursor-progress disabled:opacity-100",
              )}
              disabled={revoke.isPending}
              aria-busy={revoke.isPending || undefined}
              onClick={(event) => {
                // The dialog closes itself on click; it is held open so a
                // failure can be reported into a toast instead of vanishing.
                event.preventDefault();
                if (revoking) {
                  endOne(revoking);
                }
              }}
            >
              {revoke.isPending ? <Spinner aria-hidden="true" /> : null}
              {text("settings.tokenRevoke", "Revoke")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* One row                                                                     */
/* -------------------------------------------------------------------------- */

interface TokenRowProps {
  token: ApiToken;
  busy: boolean;
  text: ApiTokensCardProps["text"];
  onEdit: () => void;
  onRevoke: () => void;
}

function TokenRow({ token, busy, text, onEdit, onRevoke }: TokenRowProps): JSX.Element {
  const { t, i18n } = useTranslation();

  return (
    <li className={cn("p-4", token.expired && "bg-muted/40")}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 gap-3">
          <span
            className={cn(
              "flex h-9 w-9 shrink-0 items-center justify-center rounded-md",
              token.expired ? "bg-muted text-muted-foreground" : "bg-primary/10 text-primary",
            )}
          >
            <Key weight="duotone" className="h-5 w-5" aria-hidden="true" />
          </span>

          <div className="min-w-0 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <p className="text-sm font-medium leading-tight">{token.name}</p>
              {token.hint ? (
                <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
                  {`awgp_…${token.hint}`}
                </code>
              ) : null}
              {token.expired ? (
                <Badge variant="secondary" size="sm">
                  {text("settings.tokenExpired", "Expired")}
                </Badge>
              ) : null}
              {token.renewOnUse ? (
                <Badge variant="success" size="sm">
                  {text("settings.tokenRenews", "Renews on use")}
                </Badge>
              ) : null}
            </div>

            <dl className="grid gap-x-6 gap-y-1 text-xs text-muted-foreground sm:grid-cols-[auto_1fr]">
              <Detail label={text("settings.tokenCreated", "Created")}>
                {formatDateTime(token.createdAt, i18n.language) || String(t("common.notSet"))}
              </Detail>
              <Detail label={text("settings.tokenLastUsed", "Last used")}>
                {token.lastUsedAt ? (
                  <>
                    <Ago iso={token.lastUsedAt} />
                    {token.lastUsedIp ? (
                      <span className="ms-2 font-mono">{token.lastUsedIp}</span>
                    ) : null}
                  </>
                ) : (
                  <span className="italic">{text("settings.tokenNeverUsed", "never")}</span>
                )}
              </Detail>
              <Detail label={text("settings.tokenExpires", "Expires")}>
                {token.expiresAt ? (
                  <Until iso={token.expiresAt} expired={token.expired} />
                ) : (
                  <span className="italic">{text("settings.tokenExpiryNever", "Never")}</span>
                )}
              </Detail>
            </dl>

            {token.renewOnUse && token.expiresIn ? (
              <p className="text-xs leading-relaxed text-muted-foreground">
                {text(
                  "settings.tokenRenewsHint",
                  "Each use puts the expiry back to {{days}} days from that moment.",
                  { days: Math.round(token.expiresIn / 86400) },
                )}
              </p>
            ) : null}
          </div>
        </div>

        <div className="flex shrink-0 gap-1">
          <Button variant="ghost" size="sm" disabled={busy} onClick={onEdit}>
            <PencilSimple aria-hidden="true" />
            {text("settings.tokenEdit", "Edit")}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={busy}
            className="text-destructive hover:bg-destructive/10 hover:text-destructive"
            onClick={onRevoke}
          >
            <Trash aria-hidden="true" />
            {text("settings.tokenRevoke", "Revoke")}
          </Button>
        </div>
      </div>
    </li>
  );
}

/* -------------------------------------------------------------------------- */
/* Issuing                                                                     */
/* -------------------------------------------------------------------------- */

interface CreateDialogProps {
  open: boolean;
  busy: boolean;
  error: ApiError | null;
  text: ApiTokensCardProps["text"];
  onOpenChange: (open: boolean) => void;
  onSubmit: (input: { name: string; expiresIn: number; renewOnUse: boolean }) => void;
}

function CreateDialog({
  open,
  busy,
  error,
  text,
  onOpenChange,
  onSubmit,
}: CreateDialogProps): JSX.Element {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/*
        Nothing inside is focused on opening: the first field is the name, and
        it already holds a suggestion. A caret parked in it, with the nine
        characters selected, asks for an answer to a question that has one.
      */}
      <DialogContent focusSelf>
        <CreateForm busy={busy} error={error} text={text} onSubmit={onSubmit} />
      </DialogContent>
    </Dialog>
  );
}

type CreateFormProps = Omit<CreateDialogProps, "open" | "onOpenChange">;

/*
 * The form the dialog above is a shell for, and it is a component of its own so
 * that the dialog mounts it once per opening.
 *
 * Not tidying. Every field has to start fresh each time - a name left over from
 * the last token is how two of them end up called the same thing - and doing
 * that by resetting each one from an effect is a form that renders once with
 * the previous token's name in the box before the effect replaces it. Mounted
 * per opening, the suggestion is drawn in the state initialiser and there is no
 * such frame: the box has only ever held the name this dialog is offering.
 */
function CreateForm({ busy, error, text, onSubmit }: CreateFormProps): JSX.Element {
  const { t } = useTranslation();
  /**
   * A name is what the activity log calls this token for the rest of its life,
   * and an empty box asks for that decision at the moment somebody least wants
   * to make one. So one is drawn here: typing over it is the same single action
   * as accepting it, and clearing it hands the choice to the server.
   */
  const [name, setName] = React.useState(() => randomName());
  const [lifetime, setLifetime] = React.useState(DEFAULT_LIFETIME);
  const [renew, setRenew] = React.useState(false);
  /**
   * The suggestion currently in the box, or "" once it has been typed over.
   * What it buys is the one thing the panel may do on its own when the server
   * refuses a name as taken: replace a word nobody chose. See the effect below.
   */
  const suggested = React.useRef(name);
  /** The failure already answered, so answering it cannot loop. */
  const answered = React.useRef<ApiError | null>(null);
  /** Whether the box is holding a replacement drawn after a refusal. */
  const [replaced, setReplaced] = React.useState(false);

  // A name this dialog invented and the server would not take is not something
  // to hand back to the operator: they never chose that word. Another is drawn
  // and the message under the box says so; the button is still theirs to press.
  // Nothing happens to a name that was typed, which is a real decision and the
  // operator's to change, and nothing happens to an empty box either - that is
  // a choice as well, and filling it would undo it.
  React.useEffect(() => {
    if (!error?.nameInUse || error === answered.current) {
      return;
    }
    answered.current = error;
    if (!suggested.current || name !== suggested.current) {
      return;
    }
    const drawn = randomName();
    suggested.current = drawn;
    setName(drawn);
    setReplaced(true);
  }, [error, name]);

  // Nothing to renew without an expiry, and the server refuses the combination
  // outright - so the switch goes off with the choice rather than sitting there
  // claiming something that is about to be rejected.
  const canRenew = lifetime > 0;
  React.useEffect(() => {
    if (!canRenew) {
      setRenew(false);
    }
  }, [canRenew]);

  const fieldErrors = error?.errors ?? {};

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        // The notice under the name box is about the attempt just made, and
        // while it stands the sentence at the foot of the dialog is held back.
        // Left up across a second attempt it would hide whatever the server
        // says about that one - a lock it could not take, a panel that has
        // stopped answering - behind a message about a name already replaced.
        setReplaced(false);
        onSubmit({ name: name.trim(), expiresIn: lifetime, renewOnUse: renew });
      }}
    >
      <DialogHeader>
        <DialogTitle>{text("settings.tokenNew", "New token")}</DialogTitle>
        <DialogDescription>
          {text(
            "settings.tokenNewHint",
            "The secret is shown once, on the next screen. Nothing here can show it again.",
          )}
        </DialogDescription>
      </DialogHeader>

      <Field
        id="tokenName"
        label={text("settings.tokenName", "Name")}
        hint={text(
          "settings.tokenNameHint",
          "What the activity log will call it. The suggestion is only a suggestion - name it after the thing that will hold it, or clear the box and the server names it.",
        )}
        error={
          fieldErrors.name ??
          (replaced
            ? text(
                "settings.tokenNameTaken",
                "That name was already taken. Here is another - save it, or type your own.",
              )
            : undefined)
        }
      >
        {/* A value rather than a placeholder, and that is the difference
                this field turns on: grey text inside an empty box is read as a
                value often enough that somebody submits it, while a real one
                can be saved as it stands or typed straight over. */}
        <div className="flex gap-2">
          <Input
            id="tokenName"
            value={name}
            maxLength={64}
            // The first character typed or pasted at an untouched suggestion
            // takes the place of all nine, so accepting one and typing another
            // stay the same single action - without the box having to sit there
            // highlighted from the moment the dialog opens.
            onKeyDown={(event) => {
              if (typedCharacter(event)) {
                replaceSuggestion(event.currentTarget, suggested.current);
              }
            }}
            onPaste={(event) => replaceSuggestion(event.currentTarget, suggested.current)}
            aria-invalid={Boolean(fieldErrors.name)}
            onChange={(event) => {
              // Typed over, so it is a name somebody chose from here on -
              // including a keystroke that leaves it looking the same.
              suggested.current = "";
              setReplaced(false);
              setName(event.target.value);
            }}
          />
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="shrink-0"
            title={text("settings.tokenNameShuffle", "Suggest another name")}
            aria-label={text("settings.tokenNameShuffle", "Suggest another name")}
            onClick={() => {
              const drawn = randomName();
              suggested.current = drawn;
              setReplaced(false);
              setName(drawn);
            }}
          >
            <Shuffle aria-hidden="true" />
          </Button>
        </div>
      </Field>

      <Field
        id="tokenExpiry"
        label={text("settings.tokenExpires", "Expires")}
        hint={text(
          "settings.tokenExpiryHint",
          "A token that runs out is one you do not have to remember to clean up.",
        )}
        error={fieldErrors.expiresIn}
      >
        <Select value={String(lifetime)} onValueChange={(value) => setLifetime(Number(value))}>
          <SelectTrigger id="tokenExpiry" className="w-48">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {LIFETIMES.map((choice) => (
              <SelectItem key={choice.value} value={String(choice.value)}>
                {text(choice.key, choice.fallback)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>

      <div className="flex items-start justify-between gap-4 rounded-lg border border-border p-3">
        <div className="min-w-0 space-y-1">
          <p className="text-sm font-medium leading-tight">
            {text("settings.tokenRenewOnUse", "Reset the expiry each time it is used")}
          </p>
          <p className="text-xs leading-relaxed text-muted-foreground">
            {canRenew
              ? text(
                  "settings.tokenRenewOnUseHint",
                  "A token still in daily use keeps working; one whose script was retired runs out on schedule.",
                )
              : text(
                  "settings.tokenRenewNeedsExpiry",
                  "A token that never expires has nothing to renew.",
                )}
          </p>
        </div>
        <Switch
          checked={renew}
          disabled={!canRenew}
          aria-label={text("settings.tokenRenewOnUse", "Reset the expiry each time it is used")}
          onCheckedChange={setRenew}
        />
      </div>

      {/* Not while a refused suggestion has been replaced: the sentence
              under the name box is about that, and the server's "there is
              already a token called that" is about a name no longer on screen. */}
      {error && !replaced && !Object.keys(fieldErrors).length ? (
        <p role="alert" className="text-sm font-medium text-destructive">
          {error.detail}
        </p>
      ) : null}

      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="ghost" disabled={busy}>
            {t("common.cancel")}
          </Button>
        </DialogClose>
        {/* An empty box is allowed through: it is the request to be named,
                and the server answers it with the same nine characters this
                form would have suggested. */}
        <Button type="submit" loading={busy} disabled={busy}>
          {busy ? <Spinner aria-hidden="true" /> : <Key aria-hidden="true" />}
          {text("settings.tokenCreate", "Create token")}
        </Button>
      </DialogFooter>
    </form>
  );
}

interface SecretDialogProps {
  issued: { token: ApiToken; secret: string } | null;
  text: ApiTokensCardProps["text"];
  onClose: () => void;
}

/** The one sight of the secret, and the sentence saying so. */
function SecretDialog({ issued, text, onClose }: SecretDialogProps): JSX.Element {
  return (
    <Dialog
      open={issued !== null}
      onOpenChange={(open) => {
        if (!open) {
          onClose();
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {text("settings.tokenIssued", "{{name}} is ready", { name: issued?.token.name ?? "" })}
          </DialogTitle>
          <DialogDescription>
            {text(
              "settings.tokenIssuedBody",
              "Copy it now. The panel keeps a hash and not the token itself, so this is the only time it can be shown - a token that was not copied has to be replaced rather than looked up.",
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="flex items-center gap-2 rounded-md border border-border bg-muted/60 px-3 py-2">
            <code className="min-w-0 flex-1 break-all font-mono text-xs">
              {issued?.secret ?? ""}
            </code>
            <CopyButton value={issued?.secret ?? ""} />
          </div>

          <Notice icon={Warning} title={text("settings.tokenUsing", "How to send it")}>
            <p>
              {text(
                "settings.tokenUsingBody",
                "Send it as an Authorization header on any API request. No cookie and no CSRF token are needed - the header is the whole of the authentication, so treat it exactly like the password.",
              )}
            </p>
            <code className="mt-1 block break-all rounded bg-background/60 px-2 py-1 font-mono text-xs">
              {`Authorization: Bearer ${issued?.secret ?? ""}`}
            </code>
          </Notice>
        </div>

        <DialogFooter>
          <DialogClose asChild>
            <Button type="button">{text("settings.tokenCopied", "I have copied it")}</Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* -------------------------------------------------------------------------- */
/* Editing                                                                     */
/* -------------------------------------------------------------------------- */

interface EditDialogProps {
  token: ApiToken | null;
  busy: boolean;
  error: ApiError | null;
  text: ApiTokensCardProps["text"];
  onClose: () => void;
  onSubmit: (changes: { id: string; name?: string; renewOnUse?: boolean }) => void;
}

/**
 * What may be changed about a token that already exists: its name, and whether
 * using it renews it. Not the secret - that would be a new token wearing an old
 * one's name - and not the expiry, because an end date that can be pushed back
 * whenever it approaches is not really an end date.
 */
function EditDialog({ token, busy, error, text, onClose, onSubmit }: EditDialogProps): JSX.Element {
  const { t } = useTranslation();
  const [name, setName] = React.useState("");
  const [renew, setRenew] = React.useState(false);

  React.useEffect(() => {
    if (token) {
      setName(token.name);
      setRenew(token.renewOnUse);
    }
  }, [token]);

  const canRenew = Boolean(token?.expiresIn);
  const fieldErrors = error?.errors ?? {};
  const changed = token ? name.trim() !== token.name || renew !== token.renewOnUse : false;

  return (
    <Dialog
      open={token !== null}
      onOpenChange={(open) => {
        if (!open && !busy) {
          onClose();
        }
      }}
    >
      <DialogContent>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!token) {
              return;
            }
            onSubmit({
              id: token.id,
              // Only what actually moved: the endpoint takes either half, and
              // sending an unchanged name would make a rename event out of a
              // change to the switch.
              ...(name.trim() !== token.name ? { name: name.trim() } : {}),
              ...(renew !== token.renewOnUse ? { renewOnUse: renew } : {}),
            });
          }}
        >
          <DialogHeader>
            <DialogTitle>
              {text("settings.tokenEditTitle", "Edit {{name}}", { name: token?.name ?? "" })}
            </DialogTitle>
            <DialogDescription>
              {text(
                "settings.tokenEditHint",
                "The secret is not changed by anything here, so whatever holds it goes on working.",
              )}
            </DialogDescription>
          </DialogHeader>

          <Field
            id="tokenEditName"
            label={text("settings.tokenName", "Name")}
            error={fieldErrors.name}
          >
            <Input
              id="tokenEditName"
              value={name}
              maxLength={64}
              aria-invalid={Boolean(fieldErrors.name)}
              onChange={(event) => setName(event.target.value)}
            />
          </Field>

          <div className="flex items-start justify-between gap-4 rounded-lg border border-border p-3">
            <div className="min-w-0 space-y-1">
              <p className="text-sm font-medium leading-tight">
                {text("settings.tokenRenewOnUse", "Reset the expiry each time it is used")}
              </p>
              <p className="text-xs leading-relaxed text-muted-foreground">
                {canRenew
                  ? text(
                      "settings.tokenRenewOnUseHint",
                      "A token still in daily use keeps working; one whose script was retired runs out on schedule.",
                    )
                  : text(
                      "settings.tokenRenewNeedsExpiry",
                      "A token that never expires has nothing to renew.",
                    )}
              </p>
            </div>
            <Switch
              checked={renew}
              disabled={!canRenew}
              aria-label={text("settings.tokenRenewOnUse", "Reset the expiry each time it is used")}
              onCheckedChange={setRenew}
            />
          </div>

          {error && !Object.keys(fieldErrors).length ? (
            <p role="alert" className="text-sm font-medium text-destructive">
              {error.detail}
            </p>
          ) : null}

          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="ghost" disabled={busy}>
                {t("common.cancel")}
              </Button>
            </DialogClose>
            <Button type="submit" loading={busy} disabled={busy || !changed || name.trim() === ""}>
              {busy ? <Spinner aria-hidden="true" /> : null}
              {t("common.save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/* -------------------------------------------------------------------------- */
/* Small parts                                                                 */
/* -------------------------------------------------------------------------- */

interface DetailProps {
  label: string;
  children: React.ReactNode;
}

function Detail({ label, children }: DetailProps): JSX.Element {
  return (
    <>
      <dt className="sm:text-end">{label}</dt>
      <dd className="text-foreground/80 tabular-nums">{children}</dd>
    </>
  );
}

/** "2m ago", with the exact moment on hover. */
function Ago({ iso }: { iso: string }): JSX.Element {
  const { t, i18n } = useTranslation();
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) {
    return <span>{t("common.notAvailable")}</span>;
  }

  const { unit, value } = relativeParts(Math.floor(at / 1000));
  const label =
    unit === "now" || unit === "never"
      ? String(t("time.justNow"))
      : unit === "second"
        ? String(t("time.secondsAgo", { count: value }))
        : unit === "minute"
          ? String(t("time.minutesAgo", { count: value }))
          : unit === "hour"
            ? String(t("time.hoursAgo", { count: value }))
            : String(t("time.daysAgo", { count: value }));

  return <span title={formatDateTime(iso, i18n.language)}>{label}</span>;
}

/** "in 23d", or the date it ran out on for one that already has. */
function Until({ iso, expired }: { iso: string; expired: boolean }): JSX.Element {
  const { t, i18n } = useTranslation();
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) {
    return <span>{t("common.notAvailable")}</span>;
  }

  if (expired) {
    return (
      <span title={formatDateTime(iso, i18n.language)}>{formatDateTime(iso, i18n.language)}</span>
    );
  }

  const seconds = Math.max(0, Math.floor((at - Date.now()) / 1000));
  const label =
    seconds < 60
      ? String(t("time.inSeconds", { count: seconds }))
      : seconds < 3600
        ? String(t("time.inMinutes", { count: Math.floor(seconds / 60) }))
        : seconds < 86400
          ? String(t("time.inHours", { count: Math.floor(seconds / 3600) }))
          : String(t("time.inDays", { count: Math.floor(seconds / 86400) }));

  return <span title={formatDateTime(iso, i18n.language)}>{label}</span>;
}

/** The card loads as a list, so the page does not jump when the rows arrive. */
function LoadingRows(): JSX.Element {
  return (
    <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border">
      {[0, 1].map((row) => (
        <li key={row} className="flex gap-3 p-4">
          <Skeleton className="h-9 w-9 shrink-0 rounded-md" />
          <div className="min-w-0 flex-1 space-y-2">
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-3 w-full max-w-sm" />
            <Skeleton className="h-3 w-32" />
          </div>
        </li>
      ))}
    </ul>
  );
}
