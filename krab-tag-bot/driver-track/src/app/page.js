export default function Home() {
  return (
    <main
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 12,
        padding: 24,
      }}
    >
      <h1 style={{ fontSize: 28, fontWeight: 800 }}>🦀 Krab Tracking</h1>
      <p style={{ color: "var(--text-muted)", margin: 0 }}>Nothing to see here.</p>
    </main>
  );
}
