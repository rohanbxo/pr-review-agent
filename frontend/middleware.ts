// Protects everything except /signin (allowed in the `authorized` callback in auth.ts),
// Auth.js's own routes (/api/auth/*) and static assets (excluded by the matcher).
export { auth as middleware } from "@/auth";

export const config = {
  matcher: [
    "/((?!api/auth|_next/static|_next/image|favicon\\.ico|robots\\.txt|.*\\.(?:png|jpg|jpeg|gif|svg|ico|webp|css|js|map|txt|woff2?)$).*)",
  ],
};
