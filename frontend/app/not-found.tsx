import Link from "next/link";

export default function NotFound() {
  return (
    <main className="mx-auto max-w-6xl space-y-2 px-4 py-12">
      <h1 className="text-xl font-semibold">Not found</h1>
      <p className="text-muted-foreground">
        That review doesn&apos;t exist, or you can&apos;t see it.
      </p>
      <Link href="/">Back to reviews</Link>
    </main>
  );
}
