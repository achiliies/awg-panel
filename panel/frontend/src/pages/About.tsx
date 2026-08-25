import * as React from "react";
import {
  ArrowRight,
  ArrowSquareOut,
  BatteryHigh,
  BookOpenText,
  Circuitry,
  Code,
  DownloadSimple,
  GithubLogo,
  Heart,
  Info,
  MaskHappy,
  SealCheck,
  ShieldCheck,
  Warning,
  type Icon,
} from "@/lib/icons";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { ColorBrandMark } from "@/components/BrandMark";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { bootstrap } from "@/api/client";
import { useSession } from "@/api/hooks";
import { MARK_TILE_MASK, MARK_TILE_SIZE, PANEL_NAME } from "@/lib/brand";

/*
 * About: what this is, and where to read more.
 *
 * The page used to be an inventory of the machine - a feature matrix for the
 * installed module, eight paths on disk, three shell tools with commands to
 * paste - and every one of those answers has a better home than a page nobody
 * opens twice. What a build supports is enforced on the Obfuscation page,
 * where the parameters it gates are locked and labelled in place. The version,
 * interface, module and TLS are on the dashboard. The port and the server's
 * public key are on the Server page, the base path is in Settings, and the
 * paths on disk are in docs/PANEL.md, where somebody holding an SSH session is
 * already looking.
 *
 * What was left over is the only thing an About page is actually for: saying
 * what the product is to somebody who inherited it, and pointing at the
 * writing that explains the rest. So it is three blocks - what this is, why
 * the protocol underneath is worth having, and four ways into the
 * documentation - and it fits on a screen.
 *
 * The documentation links leave the panel. That is deliberate: the docs are
 * prose that is revised between releases and would be stale the moment it was
 * frozen into a bundle, and shipping four markdown files into the UI to render
 * them here would be a second, worse copy of GitHub. The API reference is the
 * exception and stays inside, because that one is not prose - it is generated
 * from the routes this installation actually serves.
 */

/** Where the docs live. `master` is the default branch and carries the newest revision. */
const REPO_URL = "https://github.com/achiliies/awg-panel";
const DOC_FILE_URL = `${REPO_URL}/blob/master/docs`;

/** SPDX identifier, so it is the same string GitHub and the LICENSE file use. */
const LICENSE = "AGPL-3.0";

interface Pillar {
  icon: Icon;
  titleKey: string;
  titleFallback: string;
  bodyKey: string;
  bodyFallback: string;
}

/**
 * Why the protocol under the panel is worth the trouble of building a kernel
 * module. Four claims, each one sentence, each about something an operator can
 * feel: a tunnel that is not filtered, a server that is not saturated, a phone
 * that lasts the day, and cryptography nobody re-invented.
 */
const PILLARS: readonly Pillar[] = [
  {
    icon: MaskHappy,
    titleKey: "about.pillarDpi",
    titleFallback: "Invisible to DPI",
    bodyKey: "about.pillarDpiHint",
    bodyFallback:
      "Every fingerprint the filters match on - handshake size, header constants, packet cadence - is randomised for this server alone.",
  },
  {
    icon: Circuitry,
    titleKey: "about.pillarKernel",
    titleFallback: "Kernel-fast",
    bodyKey: "about.pillarKernelHint",
    bodyFallback:
      "Packets are encrypted where they already live. Nothing is copied out to a process and back, and nothing waits on a context switch.",
  },
  {
    icon: BatteryHigh,
    titleKey: "about.pillarBattery",
    titleFallback: "Kind to batteries",
    bodyKey: "about.pillarBatteryHint",
    bodyFallback:
      "Silent when idle, and each packet is sealed once. No keep-alive chatter, and no proxy chain unwrapping and re-wrapping on the phone.",
  },
  {
    icon: SealCheck,
    titleKey: "about.pillarWireGuard",
    titleFallback: "Still WireGuard",
    bodyKey: "about.pillarWireGuardHint",
    bodyFallback:
      "The Noise handshake and ChaCha20-Poly1305 are untouched - stealth on top of audited cryptography, not instead of it.",
  },
];

interface DocLink {
  /** An absolute URL leaves the panel; a leading slash is a route inside it. */
  href: string;
  external: boolean;
  icon: Icon;
  titleKey: string;
  titleFallback: string;
  hintKey: string;
  hintFallback: string;
}

const DOCS: readonly DocLink[] = [
  {
    href: `${DOC_FILE_URL}/PANEL.md`,
    external: true,
    icon: BookOpenText,
    titleKey: "about.panelGuide",
    titleFallback: "Panel guide",
    hintKey: "about.panelGuideHint",
    hintFallback:
      "Installing, upgrading, HTTPS, reverse proxies, and what to do when something is wrong.",
  },
  {
    href: `${DOC_FILE_URL}/UPDATES.md`,
    external: true,
    icon: DownloadSimple,
    titleKey: "about.updates",
    titleFallback: "Updates",
    hintKey: "about.updatesHint",
    hintFallback: "What an update replaces, what it keeps, and how to go back.",
  },
  {
    href: `${DOC_FILE_URL}/SECURITY.md`,
    external: true,
    icon: ShieldCheck,
    titleKey: "about.security",
    titleFallback: "Security model",
    hintKey: "about.securityHint",
    hintFallback: "What runs as root and why, where the keys live, what a backup contains.",
  },
  {
    href: "/api-docs",
    external: false,
    icon: Code,
    titleKey: "about.apiReference",
    titleFallback: "API reference",
    hintKey: "about.apiReferenceHint",
    hintFallback: "Every endpoint this panel serves, searchable, built from this installation.",
  },
];

/* -------------------------------------------------------------------------- */
/* Building blocks                                                             */
/* -------------------------------------------------------------------------- */

/**
 * The texture behind the hero, borrowed from the login page: the mark repeated
 * as a mask rather than as a background image, so it takes its colour from the
 * card it sits on and needs no second copy for the other theme. It is faded
 * out towards the text with a radial mask, because a pattern that runs under a
 * paragraph is noise rather than texture.
 */
function HeroBackdrop(): JSX.Element {
  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
      <div className="absolute inset-0 [mask-image:radial-gradient(ellipse_75%_100%_at_8%_0%,black,transparent_75%)]">
        <div
          className="absolute inset-0 bg-foreground/[0.07]"
          style={{
            maskImage: MARK_TILE_MASK,
            WebkitMaskImage: MARK_TILE_MASK,
            maskSize: `${MARK_TILE_SIZE}px ${MARK_TILE_SIZE}px`,
            WebkitMaskSize: `${MARK_TILE_SIZE}px ${MARK_TILE_SIZE}px`,
          }}
        />
      </div>
      <div className="absolute -left-24 -top-28 h-64 w-64 rounded-full bg-primary/15 blur-3xl" />
    </div>
  );
}

interface SectionProps {
  id: string;
  title: string;
  description: string;
  children: React.ReactNode;
}

function Section({ id, title, description, children }: SectionProps): JSX.Element {
  return (
    <section aria-labelledby={id} className="space-y-4">
      <div className="space-y-1.5">
        <h2 id={id} className="text-base font-semibold tracking-tight">
          {title}
        </h2>
        <p className="text-sm leading-relaxed text-muted-foreground">{description}</p>
      </div>
      {children}
    </section>
  );
}

interface PillarCardProps {
  pillar: Pillar;
  title: string;
  body: string;
}

function PillarCard({ pillar, title, body }: PillarCardProps): JSX.Element {
  const { icon: Icon } = pillar;
  return (
    <Card className="p-5">
      <span className="mb-3 flex h-9 w-9 items-center justify-center rounded-md bg-primary/10 text-primary">
        <Icon weight="duotone" className="h-5 w-5" aria-hidden="true" />
      </span>
      <h3 className="text-sm font-semibold leading-tight">{title}</h3>
      <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{body}</p>
    </Card>
  );
}

interface DocRowProps {
  doc: DocLink;
  title: string;
  hint: string;
}

/*
 * One row is one link, and the whole row is the target: a title that is the
 * only clickable part of a three-line row is a small target on a phone and an
 * invitation to miss on a desktop. The trailing glyph says where it goes - the
 * box-with-an-arrow for the three that leave the panel, a plain arrow for the
 * one that does not.
 */
function DocRow({ doc, title, hint }: DocRowProps): JSX.Element {
  const { icon: Icon, external } = doc;
  const Away = external ? ArrowSquareOut : ArrowRight;

  const body = (
    <>
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground transition-colors group-hover:bg-primary/10 group-hover:text-primary">
        <Icon weight="duotone" className="h-[1.125rem] w-[1.125rem]" aria-hidden="true" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-medium">{title}</span>
        <span className="mt-0.5 block text-sm leading-relaxed text-muted-foreground">{hint}</span>
      </span>
      <Away
        className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground transition-colors group-hover:text-foreground"
        aria-hidden="true"
      />
    </>
  );

  const className =
    "group flex items-start gap-3 p-4 transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring";

  return external ? (
    <a href={doc.href} target="_blank" rel="noreferrer noopener" className={className}>
      {body}
    </a>
  ) : (
    <Link to={doc.href} className={className}>
      {body}
    </Link>
  );
}

/* -------------------------------------------------------------------------- */
/* Page                                                                        */
/* -------------------------------------------------------------------------- */

export default function About(): JSX.Element {
  const { t } = useTranslation();

  /** t() with the English original inline, so a missing key never shows raw. */
  const text = React.useCallback(
    (key: string, fallback: string): string => String(t(key, { defaultValue: fallback })),
    [t],
  );

  const session = useSession();
  // bootstrap carries the version index.html was built with, which is the right
  // answer while the session request is in flight and after one that failed.
  const version = session.data?.version ?? bootstrap.version;

  return (
    <>
      <PageHeader
        title={String(t("about.title"))}
        description={String(t("about.subtitle"))}
        icon={Info}
        badge={
          session.data?.mock ? <Badge variant="warning">{t("about.mockMode")}</Badge> : undefined
        }
      />

      <div className="space-y-6">
        {session.data?.mock ? (
          <div className="rounded-lg border border-warning/40 bg-warning/5 p-4">
            <div className="flex gap-3">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-warning/15 text-warning">
                <Warning weight="duotone" className="h-4 w-4" aria-hidden="true" />
              </span>
              <div className="min-w-0 space-y-1">
                <p className="text-sm font-medium leading-tight">{t("about.mockMode")}</p>
                <p className="text-sm leading-relaxed text-muted-foreground">
                  {t("about.mockModeHint")}
                </p>
              </div>
            </div>
          </div>
        ) : null}

        <Card className="relative overflow-hidden">
          <HeroBackdrop />
          {/* Even padding: CardContent's default has no top, for the header it
              normally follows, and this card has no header. */}
          <CardContent className="relative p-6 sm:p-8">
            <div className="flex flex-col gap-5 sm:flex-row sm:gap-6">
              <ColorBrandMark className="h-14 w-14 sm:h-16 sm:w-16" />
              <div className="min-w-0 space-y-4">
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                    <h2 className="text-2xl font-semibold tracking-tight">{PANEL_NAME}</h2>
                    <Badge variant="secondary" className="font-mono">
                      {version}
                    </Badge>
                    <Badge variant="outline">{LICENSE}</Badge>
                  </div>
                  <p className="text-balance text-base font-medium">
                    {text("about.tagline", "A stealth VPN server that sets itself up.")}
                  </p>
                </div>

                <p className="max-w-2xl text-sm leading-relaxed text-muted-foreground">
                  {text(
                    "about.lead",
                    "AmneziaWG is WireGuard's own cryptography wrapped in per-connection camouflage, and it is at its fastest as a kernel module - the part that has to be compiled against the kernel you run and rebuilt every time that kernel changes. This panel does that for you, and manages everything around it: clients and their QR codes, quotas and expiry, traffic history, backups.",
                  )}
                </p>

                {/* Two, not three: a Documentation button here would have sat a
                    screen-inch above a section of the same name, and the four
                    rows down there are the better door - they say which
                    document answers what. */}
                <div className="flex flex-wrap gap-2">
                  <Button asChild variant="outline">
                    <a href={REPO_URL} target="_blank" rel="noreferrer noopener">
                      <GithubLogo aria-hidden="true" />
                      {text("about.repo", "Project on GitHub")}
                    </a>
                  </Button>
                  <Button asChild variant="ghost">
                    <Link to="/support">
                      <Heart weight="fill" aria-hidden="true" />
                      {t("nav.support")}
                    </Link>
                  </Button>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>

        <Section
          id="about-why"
          title={text("about.why", "Why AmneziaWG")}
          description={text(
            "about.whyHint",
            "WireGuard is the fastest thing a VPN can be, and its handshake is the easiest thing on the network to recognise. AmneziaWG keeps the first and takes away the second.",
          )}
        >
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {PILLARS.map((pillar) => (
              <PillarCard
                key={pillar.titleKey}
                pillar={pillar}
                title={text(pillar.titleKey, pillar.titleFallback)}
                body={text(pillar.bodyKey, pillar.bodyFallback)}
              />
            ))}
          </div>
        </Section>

        <Section
          id="about-docs"
          title={String(t("about.docs"))}
          description={text(
            "about.docsHint",
            "The project's own writing, kept with the code it describes. The first three open on GitHub; the last is built from the routes this panel serves.",
          )}
        >
          <Card className="divide-y divide-border overflow-hidden">
            {DOCS.map((doc) => (
              <DocRow
                key={doc.href}
                doc={doc}
                title={text(doc.titleKey, doc.titleFallback)}
                hint={text(doc.hintKey, doc.hintFallback)}
              />
            ))}
          </Card>
        </Section>
      </div>
    </>
  );
}
