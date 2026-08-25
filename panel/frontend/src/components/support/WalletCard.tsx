import { Check, Copy, QrCode } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { CoinMark } from "@/components/support/CoinMark";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useCopy } from "@/components/CopyButton";
import { cn } from "@/lib/utils";
import type { Wallet } from "@/lib/wallets";

/*
 * One wallet: the mark, what it is, and the address.
 *
 * The address is the button. Everywhere else in the panel a value sits beside
 * a copy button, which is right for a table row where the button has to be
 * small and the value has to stay selectable; here the value is the entire
 * point of the card and forty characters of base58 is a wide target that
 * nobody wants to aim past. So the whole block copies, and the icon on the end
 * of it says which block it was.
 *
 * The address is written out in full rather than shortened to its ends. A
 * truncated address is unverifiable - the reason anybody reads one at all is
 * to check it against what their wallet is about to send to - and this page
 * has the room.
 */

export interface WalletCardProps {
  wallet: Wallet;
  /** Opens the scan dialog. The page owns it, so there is one, not ten. */
  onScan: (wallet: Wallet) => void;
}

export function WalletCard({ wallet, onScan }: WalletCardProps): JSX.Element {
  const { t } = useTranslation();
  const { copied, copy } = useCopy();

  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex items-center gap-3">
        <CoinMark coin={wallet.coin} chain={wallet.chain} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold leading-tight">{wallet.name}</p>
          <p className="truncate text-xs leading-snug text-muted-foreground">{wallet.detail}</p>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 shrink-0 text-muted-foreground"
          aria-label={String(t("support.showCode", { coin: wallet.name }))}
          onClick={() => onScan(wallet)}
        >
          <QrCode aria-hidden="true" />
        </Button>
      </div>

      {/* mt-auto so a short address does not leave the card it is in floating
          above the taller ones beside it in the row. */}
      <button
        type="button"
        aria-label={String(t("support.copyAddress", { coin: wallet.name }))}
        onClick={() => void copy(wallet.code.address)}
        className={cn(
          "mt-auto flex w-full items-start gap-2 rounded-md border px-3 py-2 text-start transition-colors",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          "focus-visible:ring-offset-2 focus-visible:ring-offset-card",
          // A confirmed copy tints the block rather than writing a word into
          // it: the address wraps, so anything that changes width reflows the
          // very thing somebody has just been reading.
          copied
            ? "border-success/50 bg-success/10"
            : "border-border bg-muted/50 hover:border-border hover:bg-accent",
        )}
      >
        <span className="min-w-0 flex-1 break-all font-mono text-xs leading-relaxed">
          {wallet.code.address}
        </span>
        <span className="mt-0.5 shrink-0">
          {copied ? (
            <Check weight="bold" className="h-3.5 w-3.5 text-success" aria-hidden="true" />
          ) : (
            <Copy className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
          )}
        </span>
        <span aria-live="polite" className="sr-only">
          {copied ? t("common.copied") : ""}
        </span>
      </button>
    </Card>
  );
}
