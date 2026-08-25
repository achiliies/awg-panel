import { WALLET_CODES, type WalletCode } from "@/lib/walletCodes";
import type { CoinId } from "@/lib/coins";

/*
 * What the Support page shows, in the order it shows it.
 *
 * Two halves that meet by id. The addresses and their QR codes are generated
 * together by panel/wallet-codes.py into lib/walletCodes.ts and never touched
 * by hand; this file is the presentation - which mark to draw, what to call
 * the chain, what order they come in - and it deliberately holds no address at
 * all. An entry whose id has no generated code is dropped rather than drawn,
 * so there is no path by which the page can show an address with the wrong
 * code beside it, or a code with no address.
 *
 * The order is roughly by how likely somebody is to already hold the coin,
 * with Monero third because a page in a VPN panel is a page read by people who
 * would rather the transfer not be public.
 *
 * Names are not translated. They are brands, and "Tether USD" is Tether USD in
 * every language the panel speaks - the same reason PANEL_NAME is a constant
 * rather than a catalog key.
 */

interface WalletPresentation {
  /** Join key into WALLET_CODES. */
  id: string;
  /** The mark on the disc. */
  coin: CoinId;
  /**
   * A second, smaller mark on the corner of the disc: the chain this lives on,
   * for a token that lives on more than one. Set for exactly the USDT pair,
   * where sending to the wrong one of two identical-looking rows loses the
   * money.
   */
  chain?: CoinId;
  name: string;
  /** The ticker, or for a token the ticker and the network it must arrive on. */
  detail: string;
}

const PRESENTATION: readonly WalletPresentation[] = [
  { id: "btc", coin: "btc", name: "Bitcoin", detail: "BTC" },
  { id: "eth", coin: "eth", name: "Ethereum", detail: "ETH" },
  { id: "xmr", coin: "xmr", name: "Monero", detail: "XMR" },
  {
    id: "usdt-trc20",
    coin: "usdt",
    chain: "trx",
    name: "Tether USD",
    detail: "USDT · TRC-20 (TRON)",
  },
  {
    id: "usdt-bep20",
    coin: "usdt",
    chain: "bnb",
    name: "Tether USD",
    detail: "USDT · BEP-20 (BNB Smart Chain)",
  },
  { id: "sol", coin: "sol", name: "Solana", detail: "SOL" },
  { id: "trx", coin: "trx", name: "TRON", detail: "TRX" },
  { id: "xrp", coin: "xrp", name: "XRP", detail: "XRP Ledger" },
  { id: "ltc", coin: "ltc", name: "Litecoin", detail: "LTC" },
  { id: "bch", coin: "bch", name: "Bitcoin Cash", detail: "BCH" },
];

export interface Wallet extends WalletPresentation {
  code: WalletCode;
}

const CODES = new Map(WALLET_CODES.map((code) => [code.id, code]));

export const WALLETS: readonly Wallet[] = PRESENTATION.flatMap((entry) => {
  const code = CODES.get(entry.id);
  return code ? [{ ...entry, code }] : [];
});
