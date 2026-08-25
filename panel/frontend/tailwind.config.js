import animate from "tailwindcss-animate";

/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    container: {
      center: true,
      padding: { DEFAULT: "1rem", sm: "1.5rem", lg: "2rem" },
      screens: { "2xl": "1400px" },
    },
    extend: {
      // Every colour resolves to a bare HSL triplet in index.css so Tailwind can
      // append an alpha channel (bg-primary/10) without a second variable.
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        overlay: "hsl(var(--overlay))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        success: {
          DEFAULT: "hsl(var(--success))",
          foreground: "hsl(var(--success-foreground))",
        },
        warning: {
          DEFAULT: "hsl(var(--warning))",
          foreground: "hsl(var(--warning-foreground))",
        },
        info: {
          DEFAULT: "hsl(var(--info))",
          foreground: "hsl(var(--info-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        chart: {
          1: "hsl(var(--chart-1))",
          2: "hsl(var(--chart-2))",
          3: "hsl(var(--chart-3))",
          4: "hsl(var(--chart-4))",
          5: "hsl(var(--chart-5))",
        },
        sidebar: {
          DEFAULT: "hsl(var(--sidebar-background))",
          foreground: "hsl(var(--sidebar-foreground))",
          primary: "hsl(var(--sidebar-primary))",
          "primary-foreground": "hsl(var(--sidebar-primary-foreground))",
          accent: "hsl(var(--sidebar-accent))",
          "accent-foreground": "hsl(var(--sidebar-accent-foreground))",
          border: "hsl(var(--sidebar-border))",
          ring: "hsl(var(--sidebar-ring))",
        },
      },
      // Motion defaults for the whole panel. Tailwind ships 150ms and a
      // symmetric ease-in-out, which on a hover or a colour swap is short
      // enough to read as a jump cut rather than a change. 200ms on a curve
      // that starts fast and settles is still quick, but the eye can follow it.
      //
      // tailwindcss-animate derives animationDuration from transitionDuration,
      // so every overlay that opens with animate-in inherits the same 200ms as
      // the button that opened it.
      transitionDuration: {
        DEFAULT: "200ms",
        // The sidebar's own, and the only duration in the panel that is a
        // variable rather than a number: a rail being dragged has to follow the
        // pointer exactly, so lib/sidebarWidth.ts sets --sidebar-motion to 0ms
        // for the length of the drag and back afterwards. `duration-sidebar` is
        // what the rail, the page beside it and the handle between them all
        // ride, so one write turns the easing off for all three at once.
        sidebar: "var(--sidebar-motion)",
      },
      transitionTimingFunction: { DEFAULT: "cubic-bezier(0.22, 0.61, 0.36, 1)" },
      borderRadius: {
        xl: "calc(var(--radius) + 4px)",
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      fontFamily: {
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "SF Mono",
          "Menlo",
          "Consolas",
          "Liberation Mono",
          "monospace",
        ],
      },
      boxShadow: {
        focus: "0 0 0 2px hsl(var(--background)), 0 0 0 4px hsl(var(--ring))",
      },
      keyframes: {
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
        // Login uses this on a rejected password: a short nudge reads as "wrong"
        // faster than any message can.
        shake: {
          "0%, 100%": { transform: "translateX(0)" },
          "15%, 55%": { transform: "translateX(-6px)" },
          "35%, 75%": { transform: "translateX(6px)" },
          "90%": { transform: "translateX(-2px)" },
        },
        // The loading ring. Identical to Tailwind's built-in `spin`, but under
        // its own name so the reduced-motion rule in index.css can exempt
        // progress indicators by naming one selector, without also reviving
        // every decorative animation that happens to rotate.
        spinner: {
          to: { transform: "rotate(360deg)" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
        shake: "shake 0.4s cubic-bezier(0.36, 0.07, 0.19, 0.97) both",
        // Just under a second. Faster and the arc smears into a solid ring at
        // the 16px most of these are drawn at; slower and a save that takes
        // half a second never completes a visible turn.
        spinner: "spinner 900ms linear infinite",
      },
    },
  },
  plugins: [animate],
};
