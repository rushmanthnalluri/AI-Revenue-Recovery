import type { Config } from "tailwindcss";

/**
 * PulseRecover design tokens — binding spec: docs/ui-design-system.md.
 * Flat amber #D9A63F on deep near-black-green #0B0D0C; hairline 1px borders
 * instead of shadows (shadows only for floating layers); Inter UI + IBM Plex
 * Mono data; 150ms cubic-bezier(0.32,0.72,0,1) transitions.
 *
 * Legacy semantic aliases (background/foreground/card/muted/primary/input/ring)
 * map onto the new ramp so view/page files that were not restyled keep
 * resolving to the same system.
 */
const config: Config = {
  darkMode: "class",
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        /* surfaces — elevation by brightness step, not shadow */
        bg: "#0B0D0C",
        surface: { DEFAULT: "#141715", hi: "#181D1A" },
        raised: "#1A1E1B",
        elevated: "#202521", // floating layers only

        /* hairlines */
        border: "#262B27",
        "border-strong": "#3A403B",

        /* text ramp */
        text: { DEFAULT: "#EFF0EB", 2: "#A2A8A0", 3: "#8A9189" },

        /* amber accent — THE signature */
        accent: {
          DEFAULT: "#D9A63F",
          hover: "#E3B254",
          ink: "#16130A",
          dim: "rgba(217, 166, 63, 0.14)",
          wash: "rgba(217, 166, 63, 0.05)",
          border: "rgba(217, 166, 63, 0.4)",
        },

        /* status */
        success: { DEFAULT: "#6FBF8C", dim: "rgba(111, 191, 140, 0.1)" },
        warning: { DEFAULT: "#D9A63F", dim: "rgba(217, 166, 63, 0.14)" },
        danger: { DEFAULT: "#D36B62", dim: "rgba(211, 107, 98, 0.1)", ink: "#21100E" },
        info: { DEFAULT: "#6E8FA0", dim: "rgba(110, 143, 160, 0.12)" },

        /* legacy semantic aliases → new ramp */
        background: "#0B0D0C",
        foreground: "#EFF0EB",
        card: { DEFAULT: "#141715", foreground: "#EFF0EB" },
        muted: { DEFAULT: "#1A1E1B", foreground: "#A2A8A0" },
        primary: { DEFAULT: "#D9A63F", foreground: "#16130A" },
        input: "#3A403B",
        ring: "#D9A63F",
      },
      borderRadius: {
        sm: "4px",
        md: "6px",
        lg: "8px",
      },
      boxShadow: {
        /* floating layers ONLY (dropdown, toast, tooltip, modal) */
        pop: "0 8px 32px rgba(0, 0, 0, 0.45)",
        float: "0 4px 20px rgba(0, 0, 0, 0.5), 0 1px 3px rgba(0, 0, 0, 0.4)",
      },
      fontFamily: {
        sans: ["var(--font-inter)", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["var(--font-plex-mono)", "ui-monospace", "SF Mono", "monospace"],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "0.875rem" }],
      },
      transitionTimingFunction: {
        apple: "cubic-bezier(0.32, 0.72, 0, 1)",
      },
      keyframes: {
        "status-pulse": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.35" },
        },
      },
      animation: {
        /* alert dots only — 1.4s ease-in-out infinite */
        "status-pulse": "status-pulse 1.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};

export default config;
