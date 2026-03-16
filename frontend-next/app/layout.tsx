import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "CloudTutor Realtime Frontend",
  description: "Next.js realtime frontend for CloudTutor",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
