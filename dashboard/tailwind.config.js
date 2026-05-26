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
          base: "#0b0f17",
          panel: "#111827",
          elev:  "#1a2233",
          border:"#243149",
        },
        accent: {
          DEFAULT: "#36D1E0",
          soft:    "#6EE7F2",
          deep:    "#1aa4b3",
        },
        success: "#10b981",
        warn:    "#f59e0b",
        danger:  "#ef4444",
      },
      boxShadow: {
        glow: "0 0 0 1px rgba(54, 209, 224, 0.18), 0 8px 24px -8px rgba(54, 209, 224, 0.28)",
        card: "0 4px 12px -2px rgba(0,0,0,0.35)",
      },
      borderRadius: {
        xl2: "1rem",
      },
    },
  },
  plugins: [],
};
