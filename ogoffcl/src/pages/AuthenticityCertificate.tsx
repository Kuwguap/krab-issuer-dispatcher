import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

interface Product { name: string; image: string | null; size: string | null; qty: number; }
interface Result { found: boolean; orderNumber?: string; purchasedOn?: string; products?: Product[]; paid?: boolean; }

/**
 * Printable A4 Certificate of Authenticity. Admin opens it with ?code=OGA-...
 * It shows ONLY product + code + QR (no PII) so the printed sheet is safe to
 * ship inside the parcel. QR → /authentic-check?code=… for public verification.
 */
export default function AuthenticityCertificate() {
  const [params] = useSearchParams();
  const code = (params.get("code") || "").trim().toUpperCase();
  const autoPrint = params.get("print") === "1";
  const [res, setRes] = useState<Result | null>(null);
  const [loading, setLoading] = useState(true);

  const verifyUrl = `https://ogoffcl.store/authentic-check?code=${encodeURIComponent(code)}`;
  const qr = `https://api.qrserver.com/v1/create-qr-code/?size=260x260&margin=0&ecc=M&data=${encodeURIComponent(verifyUrl)}`;

  useEffect(() => {
    if (!code) { setLoading(false); return; }
    fetch(`/api/order/cert?code=${encodeURIComponent(code)}`)
      .then((r) => (r.ok ? r.json() : { found: false }))
      .then((j) => setRes(j))
      .catch(() => setRes({ found: false }))
      .finally(() => setLoading(false));
  }, [code]);

  // auto-open the print dialog once the sheet + QR have had a moment to render
  useEffect(() => {
    if (!autoPrint || loading || !res || !res.found) return;
    const t = setTimeout(() => window.print(), 900);
    return () => clearTimeout(t);
  }, [autoPrint, loading, res]);

  const S = {
    body: { background: "#3a3a3a", minHeight: "100vh", display: "flex", flexDirection: "column" as const, alignItems: "center", padding: "24px", fontFamily: '"Space Grotesk",sans-serif' },
    bar: { width: "210mm", maxWidth: "100%", display: "flex", justifyContent: "space-between", marginBottom: "14px" },
    btn: { fontFamily: '"Space Grotesk",sans-serif', fontWeight: 700, fontSize: "13px", letterSpacing: ".06em", textTransform: "uppercase" as const, padding: "12px 22px", background: "#C8FF00", color: "#0A0A0A", border: "none", cursor: "pointer" },
  };

  if (loading) return <div style={S.body as any}><p style={{ color: "#F5F2EA", fontFamily: '"Space Grotesk"' }}>Loading…</p></div>;

  return (
    <>
      <style>{`
        @page{ size:A4; margin:0 }
        @media print{ .noprint{ display:none !important } body{ background:#fff } .sheet{ box-shadow:none !important } }
        html,body{ -webkit-print-color-adjust:exact; print-color-adjust:exact }
        @import url('https://fonts.googleapis.com/css2?family=Archivo+Black&family=Space+Grotesk:wght@400;500;600;700&display=swap');
      `}</style>
      <div style={S.body as any}>
        <div className="noprint" style={S.bar as any}>
          <a href="/admin/orders" style={{ color: "#C8FF00", fontFamily: '"Space Grotesk"', fontWeight: 600, fontSize: "13px", textDecoration: "none" }}>← Back to orders</a>
          <button style={S.btn as any} onClick={() => window.print()}>⎙ Print / Save PDF (A4)</button>
        </div>

        <div className="sheet" style={{
          width: "210mm", minHeight: "297mm", background: "#F5F2EA", color: "#0A0A0A",
          padding: "20mm 18mm", position: "relative", boxShadow: "0 30px 80px rgba(0,0,0,.5)",
          backgroundImage: "linear-gradient(#0a0a0a08 1px,transparent 1px),linear-gradient(90deg,#0a0a0a08 1px,transparent 1px)",
          backgroundSize: "26px 26px",
        }}>
          {/* header */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", borderBottom: "3px solid #0A0A0A", paddingBottom: "18px" }}>
            <div style={{ fontFamily: '"Archivo Black"', fontSize: "34px", letterSpacing: "-1px", lineHeight: 1 }}>OG<span style={{ color: "#7a7a00" }}>.</span>OFFCL</div>
            <div style={{ textAlign: "right", fontSize: "11px", fontWeight: 600, letterSpacing: ".18em", textTransform: "uppercase", color: "#555", lineHeight: 1.7 }}>
              Original Gangster Official<br />Accra, Ghana
            </div>
          </div>

          {!res || !res.found ? (
            <div style={{ marginTop: "60px", textAlign: "center" }}>
              <p style={{ fontFamily: '"Archivo Black"', fontSize: "40px", color: "#c0392b" }}>NO RECORD FOUND</p>
              <p style={{ color: "#555", marginTop: "14px", fontSize: "15px" }}>Code <b>{code || "—"}</b> did not match a paid order.</p>
            </div>
          ) : (
            <>
              <div style={{ marginTop: "38px" }}>
                <p style={{ fontSize: "12px", fontWeight: 700, letterSpacing: ".35em", textTransform: "uppercase", color: "#7a7a00" }}>Certificate of</p>
                <h1 style={{ fontFamily: '"Archivo Black"', fontSize: "72px", lineHeight: .92, textTransform: "uppercase", letterSpacing: "-1px", marginTop: "6px" }}>Authenticity</h1>
                <p style={{ color: "#3a3a3a", fontSize: "15px", marginTop: "18px", maxWidth: "62ch", lineHeight: 1.6 }}>
                  This certifies that the item(s) below are <b>genuine OG OFFCL pieces</b>, produced and sold by Original Gangster Official. Verify this certificate any time by scanning the code or entering the authenticity number at <b>ogoffcl.store/authentic-check</b>.
                </p>
              </div>

              {/* body: products + QR */}
              <div style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: "30px", marginTop: "34px", alignItems: "start" }}>
                <div>
                  <p style={{ fontSize: "11px", fontWeight: 700, letterSpacing: ".2em", textTransform: "uppercase", color: "#7a7a00", marginBottom: "10px" }}>The piece(s)</p>
                  <div style={{ border: "1px solid #d8d4c8" }}>
                    {(res.products || []).map((p, i) => (
                      <div key={i} style={{ display: "flex", alignItems: "center", gap: "14px", padding: "12px 14px", borderTop: i ? "1px solid #e2ded2" : "none" }}>
                        <div style={{ width: "48px", height: "56px", background: "#fff", border: "1px solid #d8d4c8", overflow: "hidden", flex: "none" }}>
                          {p.image && <img src={p.image} alt="" style={{ width: "100%", height: "100%", objectFit: "cover", mixBlendMode: "multiply" }} />}
                        </div>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <p style={{ fontFamily: '"Archivo Black"', fontSize: "14px", textTransform: "uppercase", lineHeight: 1.15 }}>{p.name}</p>
                          <p style={{ color: "#777", fontSize: "12px", marginTop: "3px" }}>{p.size ? `Size ${p.size} · ` : ""}Qty {p.qty}</p>
                        </div>
                        <span style={{ fontFamily: '"Archivo Black"', fontSize: "10px", color: "#7a7a00", textTransform: "uppercase", letterSpacing: ".1em" }}>Genuine</span>
                      </div>
                    ))}
                  </div>
                  <div style={{ marginTop: "22px", display: "flex", gap: "34px", flexWrap: "wrap" }}>
                    <div>
                      <p style={{ fontSize: "10px", fontWeight: 700, letterSpacing: ".18em", textTransform: "uppercase", color: "#999" }}>Authenticity No.</p>
                      <p style={{ fontFamily: '"Archivo Black"', fontSize: "20px", marginTop: "4px", letterSpacing: "0" }}>{code}</p>
                    </div>
                    <div>
                      <p style={{ fontSize: "10px", fontWeight: 700, letterSpacing: ".18em", textTransform: "uppercase", color: "#999" }}>Order</p>
                      <p style={{ fontFamily: '"Archivo Black"', fontSize: "20px", marginTop: "4px" }}>{res.orderNumber}</p>
                    </div>
                    <div>
                      <p style={{ fontSize: "10px", fontWeight: 700, letterSpacing: ".18em", textTransform: "uppercase", color: "#999" }}>Issued</p>
                      <p style={{ fontFamily: '"Archivo Black"', fontSize: "20px", marginTop: "4px" }}>{res.purchasedOn ? new Date(res.purchasedOn).toLocaleDateString() : "—"}</p>
                    </div>
                  </div>
                </div>

                <div style={{ textAlign: "center" }}>
                  <div style={{ border: "3px solid #0A0A0A", padding: "10px", background: "#fff" }}>
                    <img src={qr} alt="Verification QR" width={200} height={200} style={{ display: "block" }} />
                  </div>
                  <p style={{ fontSize: "10px", fontWeight: 600, letterSpacing: ".14em", textTransform: "uppercase", color: "#777", marginTop: "10px", maxWidth: "220px" }}>Scan to verify at<br />ogoffcl.store/authentic-check</p>
                </div>
              </div>
            </>
          )}

          {/* footer */}
          <div style={{ position: "absolute", left: "18mm", right: "18mm", bottom: "16mm", borderTop: "2px solid #0A0A0A", paddingTop: "14px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <p style={{ fontSize: "11px", fontWeight: 600, letterSpacing: ".14em", textTransform: "uppercase", color: "#555" }}>From big dreams to reality</p>
            <p style={{ fontFamily: '"Archivo Black"', fontSize: "13px", textTransform: "uppercase" }}>OGOFFCL.STORE</p>
          </div>
        </div>
      </div>
    </>
  );
}
