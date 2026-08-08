export default function Home() {
  return (
    <main className="min-h-screen bg-[#08142b] text-slate-100">
      <header className="border-b border-white/10 px-6 py-5">
        <div className="mx-auto flex max-w-6xl items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="grid h-10 w-10 place-items-center rounded-xl bg-[#cf421a] text-xl font-black">S</div>
            <span className="text-lg font-semibold">ClientsPhoneNumber</span>
          </div>
          <a href="/admin" className="rounded-md bg-[#cf421a] px-4 py-2 text-sm font-semibold text-white no-underline">
            Admin
          </a>
        </div>
      </header>

      <section className="mx-auto max-w-6xl px-6 py-16">
        <p className="mb-5 inline-block rounded-full bg-[#cf421a]/20 px-4 py-1 text-sm text-orange-200">
          Privacy-first architecture
        </p>
        <h1 className="text-5xl font-black leading-tight md:text-7xl">
          ClientsPhoneNumber
          <br />
          <span className="text-[#ef784e]">Signed.</span>{" "}
          <span className="text-white">Sealed.</span>{" "}
          <span className="text-slate-300">Delivered.</span>
        </h1>
        <p className="mt-6 max-w-3xl text-lg text-slate-300">
          Send passphrase-protected links for sensitive client phone numbers with a OneTimeSecret-compatible API.
        </p>
      </section>

      <section className="mx-auto max-w-6xl px-6 pb-16">
        <div className="rounded-2xl border border-white/10 bg-white/5 p-6">
          <h2 className="mb-2 text-2xl font-bold">Create Secret Link</h2>
          <p className="mb-4 text-slate-300">
            API endpoint: <code>POST /api/v1/share</code> with Basic Auth + fields <code>secret</code>,{" "}
            <code>passphrase</code>, <code>ttl</code>.
          </p>
          <p className="text-slate-400">
            Unlock links look like: <code>/secret/&lt;secret_key&gt;</code>
          </p>
        </div>
      </section>
    </main>
  );
}
