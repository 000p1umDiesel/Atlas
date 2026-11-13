export function Placeholder({ title }: { title: string }) {
  return (
    <section className="mx-auto max-w-2xl py-16 text-center">
      <h1 className="text-3xl font-semibold">{title}</h1>
      <p className="mt-3 rounded-[14px] bg-card p-6 text-muted shadow-card">
        Появится на следующем этапе
      </p>
    </section>
  );
}
