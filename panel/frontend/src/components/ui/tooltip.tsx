import * as React from "react";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";

import { cn } from "@/lib/utils";

/*
 * Radix throws when a Tooltip renders outside a Provider. The app mounts one at
 * the root, but a tooltip in a portal-rendered subtree (or a page rendered on
 * its own in a test) would otherwise take the whole panel down, so Tooltip
 * falls back to providing its own. The marker context tells it which case it is
 * in, which keeps the shared open/close grouping working in the normal path.
 */
const HasTooltipProvider = React.createContext(false);

const TooltipProvider = ({
  delayDuration = 250,
  skipDelayDuration = 300,
  children,
  ...props
}: React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Provider>) => (
  <TooltipPrimitive.Provider
    delayDuration={delayDuration}
    skipDelayDuration={skipDelayDuration}
    {...props}
  >
    <HasTooltipProvider.Provider value={true}>{children}</HasTooltipProvider.Provider>
  </TooltipPrimitive.Provider>
);
TooltipProvider.displayName = "TooltipProvider";

const Tooltip = (props: React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Root>) => {
  const hasProvider = React.useContext(HasTooltipProvider);
  if (hasProvider) {
    return <TooltipPrimitive.Root {...props} />;
  }
  return (
    <TooltipProvider>
      <TooltipPrimitive.Root {...props} />
    </TooltipProvider>
  );
};
Tooltip.displayName = "Tooltip";

const TooltipTrigger = TooltipPrimitive.Trigger;

const TooltipContent = React.forwardRef<
  React.ElementRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(({ className, sideOffset = 6, collisionPadding = 12, ...props }, ref) => (
  <TooltipPrimitive.Portal>
    <TooltipPrimitive.Content
      ref={ref}
      sideOffset={sideOffset}
      collisionPadding={collisionPadding}
      className={cn(
        "z-50 max-w-xs rounded-md border border-border bg-popover px-2.5 py-1.5",
        "text-xs leading-snug text-popover-foreground shadow-md",
        // The width cap only holds where the text can wrap. A run with nothing
        // to break on - a key, an address, a language that does not write
        // spaces - carries on past the right edge of the box it is meant to be
        // inside, so let it break mid-run rather than let it out.
        "[overflow-wrap:anywhere]",
        // And never taller than the room Radix measured on the side it landed.
        "max-h-[var(--radix-tooltip-content-available-height)] overflow-y-auto overscroll-contain",
        "ease-out data-[state=delayed-open]:animate-in data-[state=delayed-open]:fade-in-0 data-[state=delayed-open]:zoom-in-95",
        "data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95",
        // A tooltip that lingers follows the pointer around; get it out fast.
        "data-[state=closed]:duration-100",
        "data-[side=bottom]:slide-in-from-top-1 data-[side=top]:slide-in-from-bottom-1",
        "data-[side=left]:slide-in-from-right-1 data-[side=right]:slide-in-from-left-1",
        className,
      )}
      {...props}
    />
  </TooltipPrimitive.Portal>
));
TooltipContent.displayName = TooltipPrimitive.Content.displayName;

export { TooltipProvider, Tooltip, TooltipTrigger, TooltipContent };
