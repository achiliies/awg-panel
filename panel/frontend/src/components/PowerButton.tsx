import { Power } from "@/lib/icons";
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
import { useStartServer, useStopServer } from "@/api/hooks";

export interface PowerButtonProps {
  /** Whether the interface is up right now. Decides which of the two this is. */
  running: boolean;
}

/*
 * The strings for each direction, gathered so the two sets can be read against
 * each other. Spelling them out beats deriving them from `running`: the six
 * keys are not a stem plus a suffix in English and would not be in any other
 * language either.
 */
const COPY = {
  stop: {
    label: "server.stop",
    busy: "server.stopping",
    done: "server.stopped",
    failed: "server.stopFailed",
    confirm: "server.stopConfirm",
    confirmBody: "server.stopConfirmBody",
  },
  start: {
    label: "server.start",
    busy: "server.starting",
    done: "server.started",
    failed: "server.startFailed",
    confirm: "server.startConfirm",
    confirmBody: "server.startConfirmBody",
  },
} as const;

/*
 * Switch the tunnel off, or back on.
 *
 * One control rather than two, because there is only ever one of them worth
 * pressing: a stopped tunnel cannot be stopped and a running one does not need
 * starting, and a pair of buttons with one of them always dead would make the
 * reader work out which. What it says is what it will do.
 *
 * Deliberately not the same act as the restart button beside it, which is why
 * that one stays and this was added rather than replacing it. A restart is
 * something done *to* a tunnel that is meant to be running: it ends with every
 * client back on and nothing changed. This ends with a server that serves
 * nobody, for as long as the admin leaves it that way. Both go behind a
 * confirmation; only this one has to say what does not come back by itself.
 *
 * Stopping is the quiet outline and starting is the filled button, which is the
 * way round it looks wrong until you read it as a state: on a tunnel that is
 * down, starting it is the thing the operator came to do, and there is nothing
 * on the page for the filled button to compete with because the numbers are all
 * zero. On a running one, this is the button that costs something.
 */
export function PowerButton({ running }: PowerButtonProps): JSX.Element {
  const { t } = useTranslation();
  const { toast } = useToast();
  const stop = useStopServer();
  const start = useStartServer();

  const copy = running ? COPY.stop : COPY.start;
  const action = running ? stop : start;
  const pending = action.isPending;

  const confirm = (): void => {
    action.mutate(undefined, {
      onSuccess: () => {
        toast({ title: String(t(copy.done)), variant: "success" });
      },
      onError: (error) => {
        toast({
          title: String(t(copy.failed)),
          description: error.detail,
          variant: "destructive",
        });
      },
    });
  };

  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button type="button" variant={running ? "outline" : "default"} size="sm" loading={pending}>
          {pending ? <Spinner aria-hidden="true" /> : <Power aria-hidden="true" />}
          {t(pending ? copy.busy : copy.label)}
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t(copy.confirm)}</AlertDialogTitle>
          <AlertDialogDescription>{t(copy.confirmBody)}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
          <AlertDialogAction onClick={confirm}>{t(copy.label)}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
