import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Leakproof · Recovery command",
  description: "Auditable revenue recovery scoreboard and case timeline",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
