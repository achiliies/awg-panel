import * as React from "react";
import { Heart } from "@/lib/icons";
import { useTranslation } from "react-i18next";

import { PageHeader } from "@/components/PageHeader";
import { WalletCard } from "@/components/support/WalletCard";
import { WalletDialog } from "@/components/support/WalletDialog";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { WALLETS, type Wallet } from "@/lib/wallets";

/*
 * Support: the one page here that asks for something rather than doing
 * something.
 *
 * It is written to be skippable. Whoever opened it already has the panel
 * working, owes nothing for it, and is one click from never seeing it again -
 * so the words say what the money is for and then get out of the way, and the
 * page's own weight is on the ten cards that answer "which chain, and where".
 * There is no goal bar, no suggested amount and no nagging: the link into here
 * is a line at the foot of the sidebar, and it is the only mention anywhere.
 *
 * Nothing on this page talks to the server. The addresses and their codes are
 * generated into the bundle by panel/wallet-codes.py, which is why the page
 * has no loading state, no error state and works on a panel whose tunnel is
 * down - and why "here is where to send money" never became an operation in an
 * API whose every route is a thing done to a VPN server.
 */

export default function Support(): JSX.Element {
  const { t } = useTranslation();
  // Held after the dialog closes so the code inside it does not disappear
  // before the box does, the same way the client QR dialog is driven.
  const [scanning, setScanning] = React.useState<Wallet | null>(null);
  const [open, setOpen] = React.useState(false);

  const scan = React.useCallback((wallet: Wallet) => {
    setScanning(wallet);
    setOpen(true);
  }, []);

  return (
    <>
      <PageHeader
        title={String(t("support.title"))}
        description={String(t("support.subtitle"))}
        icon={Heart}
      />

      <div className="space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>{t("support.leadTitle")}</CardTitle>
          </CardHeader>
          {/* No measure of its own. This started at max-w-prose, which is 65
              characters, and on anything wider than a laptop that left every
              paragraph stopping a third of the way across a card it had all of
              to itself - the line looked broken rather than measured. The rest
              of the panel caps nothing (see components/apidocs/Prose), so the
              page container's own width is the width, here as everywhere. */}
          <CardContent className="space-y-3 text-sm leading-relaxed text-muted-foreground">
            <p>{t("support.lead1")}</p>
            <p>{t("support.lead2")}</p>
            <p className="text-foreground">{t("support.lead3")}</p>
          </CardContent>
        </Card>

        <section aria-labelledby="wallets-heading" className="space-y-4">
          <div className="space-y-1.5">
            <h2 id="wallets-heading" className="text-base font-semibold tracking-tight">
              {t("support.walletsTitle")}
            </h2>
            <p className="text-sm leading-relaxed text-muted-foreground">
              {t("support.walletsHint")}
            </p>
          </div>

          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {WALLETS.map((wallet) => (
              <WalletCard key={wallet.id} wallet={wallet} onScan={scan} />
            ))}
          </div>

          {/* The one thing on this page that can cost somebody money, so it is
              below the cards it is about rather than above them, where it
              would be read before there was anything to apply it to. */}
          <p role="note" className="text-sm leading-relaxed text-muted-foreground">
            {t("support.networkWarning")}
          </p>
        </section>

        <p className="text-sm leading-relaxed">{t("support.thanks")}</p>
      </div>

      <WalletDialog wallet={scanning} open={open} onOpenChange={setOpen} />
    </>
  );
}
