import type { Metadata } from "next";

import { SessionProvider } from "@/lib/auth-context";

import "./globals.css";

export const metadata: Metadata = {
  title: "VoiceDesk",
  description: "AI voice agents for Indian businesses",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-full antialiased">
        <SessionProvider>{children}</SessionProvider>
      </body>
    </html>
  );
}
