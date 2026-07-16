/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#0A0A0A",
        bone: "#F5F2EA",
        acid: "#C8FF00",
        blood: "#FF2A2A",
        smoke: "#141414",
        ash: "#1E1E1E",
      },
      fontFamily: {
        display: ['"Archivo Black"', "Impact", "sans-serif"],
        body: ['"Space Grotesk"', "system-ui", "sans-serif"],
      },
      keyframes: {
        marquee: {
          "0%": { transform: "translateX(0)" },
          "100%": { transform: "translateX(-50%)" },
        },
        pulseSoft: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.55" },
        },
        floaty: {
          "0%, 100%": { transform: "translateY(0px)" },
          "50%": { transform: "translateY(-10px)" },
        },
      },
      animation: {
        marquee: "marquee 22s linear infinite",
        "marquee-fast": "marquee 14s linear infinite",
        pulseSoft: "pulseSoft 2.4s ease-in-out infinite",
        floaty: "floaty 5s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
