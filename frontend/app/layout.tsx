import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "PR Review Agent",
  description: "Read-only, auditable pull request reviews.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
