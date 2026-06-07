import { useState } from "react";
import { api } from "../api";
import { FORM_FIELD_KEYS } from "../fields";

function pickFormFields(payload) {
  if (!payload) return {};
  const out = {};
  for (const key of FORM_FIELD_KEYS) {
    const val = payload[key];
    if (val != null && String(val).trim()) out[key] = val;
  }
  return out;
}

export default function AiFillPanel({ draftId, onFilled, disabled }) {
  const [pasteText, setPasteText] = useState("");
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");

  const applyPayload = (payload, filledKeys) => {
    const picked = pickFormFields(payload);
    const count = filledKeys?.length || Object.keys(picked).length;
    if (!count) {
      setNote("Could not detect any fields. Try clearer text or a sharper photo.");
      return;
    }
    onFilled(picked);
    setNote(`✓ Filled ${count} field${count === 1 ? "" : "s"} — review and edit anything that looks wrong.`);
  };

  const parseText = async () => {
    if (!draftId || !pasteText.trim()) return;
    setBusy("text");
    setNote("");
    try {
      const res = await api(`/api/interview/draft/${draftId}/parse-text`, {
        method: "POST",
        body: JSON.stringify({ text: pasteText.trim() }),
      });
      applyPayload(res.payload, res.filledKeys);
    } catch (e) {
      setNote(e.message || "Could not parse text.");
    } finally {
      setBusy("");
    }
  };

  const parseImage = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!draftId || !file) return;
    setBusy("image");
    setNote("");
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await api(`/api/interview/draft/${draftId}/parse-image`, {
        method: "POST",
        body: fd,
      });
      applyPayload(res.payload, res.filledKeys);
    } catch (err) {
      setNote(err.message || "Could not read image.");
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="form-card ai-fill-panel">
      <label className="field-label" htmlFor="ai-paste">
        Paste interview text
      </label>
      <textarea
        id="ai-paste"
        className="field-textarea ai-paste"
        rows={3}
        placeholder={
          "Example:\nName: Eddie Goldman\nPhone: (201) 555-0123\nEmail: eddie@gmail.com\nTelegram: @eddie_goldman\n..."
        }
        value={pasteText}
        onChange={(e) => setPasteText(e.target.value)}
        disabled={disabled || !!busy}
      />
      <div className="ai-fill-actions">
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={parseText}
          disabled={disabled || !!busy || !pasteText.trim()}
        >
          {busy === "text" ? "Parsing…" : "Auto-fill from text"}
        </button>
        <label className="btn btn-secondary btn-sm ai-file-btn">
          {busy === "image" ? "Reading…" : "Auto-fill from screenshot"}
          <input
            type="file"
            accept="image/jpeg,image/png,image/webp,image/gif"
            hidden
            disabled={disabled || !!busy}
            onChange={parseImage}
          />
        </label>
      </div>
      {note && <p className={`verify-msg ${note.startsWith("✓") ? "ok" : "bad"}`}>{note}</p>}
    </div>
  );
}
