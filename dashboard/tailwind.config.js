/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{vue,ts,tsx,js,jsx}"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: ["Vazirmatn", "system-ui", "-apple-system", "Segoe UI", "Arial", "sans-serif"],
      },
      colors: {
        // Custom palette tuned for dark dashboards. Background goes very
        // dark slate, panels lift one notch, accents are cool teal/cyan
        // matching the bot's miniapp identity for visual continuity.
        bg: {
          base: "rgb(var(--bg-base) / <alpha-value>)",
          panel: "rgb(var(--bg-panel) / <alpha-value>)",
          elev:  "rgb(var(--bg-elev) / <alpha-value>)",
          border:"rgb(var(--bg-border) / <alpha-value>)",
        },
        accent: {
          DEFAULT: "#5DD6A0",
          soft:    "#91E9BC",
          deep:    "#25AA74",
        },
        success: "#10b981",
        warn:    "#f59e0b",
        danger:  "#ef4444",
      },
      boxShadow: {
        glow: "0 1px 2px rgba(0,0,0,.12)",
        card: "0 1px 2px rgba(0,0,0,.08)",
      },
      borderRadius: {
        xl2: "1rem",
      },
    },
  },
  plugins: [],
};
