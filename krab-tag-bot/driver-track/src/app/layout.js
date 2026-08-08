import { Sora, Instrument_Sans } from "next/font/google";
import "./globals.css";

const sora = Sora({
  subsets: ["latin"],
  weight: ["600", "700", "800"],
  variable: "--font-sora",
});

const instrumentSans = Instrument_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-instrument",
});

export const metadata = {
  title: "Krab Driver Tracking",
  description: "Live driver location tracking for Krab dispatch.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body
        className={`${sora.variable} ${instrumentSans.variable}`}
        style={{ background: "#0d1626", color: "#d7e3f5" }}
      >
        {children}
      </body>
    </html>
  );
}
