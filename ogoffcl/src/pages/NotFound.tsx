import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <div className="max-w-2xl mx-auto px-4 py-32 text-center">
      <p className="display-xl text-[8rem] leading-none text-stroke">404</p>
      <p className="text-bone/50 uppercase tracking-widest text-sm mt-6">This page ain't official.</p>
      <Link to="/" className="btn-og bg-acid text-ink px-8 py-4 text-sm mt-10 inline-flex hover:bg-bone">
        Back home →
      </Link>
    </div>
  );
}
