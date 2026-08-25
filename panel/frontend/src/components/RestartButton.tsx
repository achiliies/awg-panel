import { ArrowClockwise } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useToast } from "@/components/ui/toast";
import { useRestartServer } from "@/api/hooks";

export interface RestartButtonProps {
  /** Run once the tunnel is back up. For a caller with a banner to clear. */
  onRestarted?: () => void;
}

/*
 * Restart the tunnel: awg-quick down, awg-quick up.
 *
 * The whole act is here - the confirmation, the request, and what is said
 * afterwards - because it is offered from two places with nothing else in
 * common. The dashboard is where somebody watching a tunnel misbehave already
 * is, and this is the one thing they can do to it from the panel; the other is
 * the banner a save leaves behind when it wrote the config but could not put it
 * in force. Passing a callback and a pending flag down from each of them meant
 * the same success-and-failure toast written twice, and a page holding a
 * mutation it had no other use for.
 *
 * Restarting drops every session for a second or two, so it always goes through
 * the confirmation - there is no variant of this button that fires on one click.
 *
 * The arrows rather than the power glyph, which this used to wear: the power
 * button now sits beside it, and two controls showing the same symbol next to
 * each other invite the reader to press whichever they reach first.
 */
export function RestartButton({ onRestarted }: RestartButtonProps): JSX.Element {
  const { t } = useTranslation();
  const { toast } = useToast();
  const restart = useRestartServer();
  const pending = restart.isPending;

  const confirm = (): void => {
    restart.mutate(undefined, {
      onSuccess: () => {
        toast({ title: String(t("server.restarted")), variant: "success" });
        onRestarted?.();
      },
      onError: (error) => {
        toast({
          title: String(t("server.restartFailed")),
          description: error.detail,
          variant: "destructive",
        });
      },
    });
  };

  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button type="button" variant="outline" size="sm" loading={pending}>
          {pending ? <Spinner aria-hidden="true" /> : <ArrowClockwise aria-hidden="true" />}
          {t(pending ? "server.restarting" : "server.restart")}
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t("server.restartConfirm")}</AlertDialogTitle>
          <AlertDialogDescription>{t("server.restartConfirmBody")}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
          <AlertDialogAction onClick={confirm}>{t("server.restart")}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
