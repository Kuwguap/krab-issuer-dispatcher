import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({ subsets: ["latin"] });

export const metadata = {
  title: "ClientsPhoneNumber",
  description:
    "Passphrase-protected links for sensitive client phone numbers — privacy-first, OneTimeSecret-compatible API.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body className={inter.className}>{children}</body>
    </html>
  );
}
