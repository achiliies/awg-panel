import { useTranslation } from "react-i18next";

import { CoinMark } from "@/components/support/CoinMark";
import { CopyButton } from "@/components/CopyButton";
import { WalletCode } from "@/components/support/WalletCode";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { Wallet } from "@/lib/wallets";

/*
 * The code, at a size a phone locks onto from arm's length.
 *
 * Sized around the symbol the way the client's QR dialog is, and for the same
 * reason - the code is what the box is for, so nothing under it is tall enough
 * to push it off a laptop screen. The address is repeated below it because a
 * dialog somebody opened to scan is also the dialog they will hold up next to
 * their wallet to read the last six characters off.
 *
 * Nothing here is a secret, unlike the client dialog this borrows its shape
 * from: a receiving address is public by construction, so there is no reason
 * to keep it out of a cache and no warning to carry.
 */

export interface WalletDialogProps {
  /**
   * Held non-null by the page while the dialog animates out, so the code does
   * not vanish before the box around it does.
   */
  wallet: Wallet | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function WalletDialog({
  wallet,
  open,
  onOpenChange,
}: WalletDialogProps): JSX.Element | null {
  const { t } = useTranslation();

  if (!wallet) {
    return null;
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex flex-col gap-4 sm:max-w-sm">
        <DialogHeader className="shrink-0">
          <DialogTitle className="flex items-center gap-2.5">
            <CoinMark coin={wallet.coin} chain={wallet.chain} className="h-7 w-7" />
            <span className="min-w-0 truncate">{wallet.name}</span>
          </DialogTitle>
          <DialogDescription>{wallet.detail}</DialogDescription>
        </DialogHeader>

        <WalletCode
          code={wallet.code}
          label={String(t("support.codeAlt", { coin: wallet.name }))}
          className="mx-auto w-[min(13.5rem,56vw,40vh)] shrink-0"
        />

        <div className="flex shrink-0 items-start gap-2 rounded-md border border-border bg-muted/50 px-3 py-2">
          <span className="min-w-0 flex-1 break-all font-mono text-xs leading-relaxed">
            {wallet.code.address}
          </span>
          <CopyButton
            value={wallet.code.address}
            variant="ghost"
            className="-my-1 -me-1.5 h-7 w-7 shrink-0"
            aria-label={String(t("support.copyAddress", { coin: wallet.name }))}
          />
        </div>

        <DialogFooter className="shrink-0">
          <DialogClose asChild>
            <Button variant="outline">{t("common.close")}</Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
