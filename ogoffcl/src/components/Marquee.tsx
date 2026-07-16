interface Props {
  text?: string;
  className?: string;
  fast?: boolean;
}

export default function Marquee({
  text = "OG OFFCL — ORIGINAL GANGSTER OFFICIAL — NEW DROP LIVE — WEAR THE CULTURE — ",
  className = "bg-acid text-ink",
  fast = false,
}: Props) {
  const chunk = Array(6).fill(text).join("");
  return (
    <div className={`overflow-hidden py-2.5 select-none ${className}`}>
      <div className={`whitespace-nowrap font-display uppercase text-sm tracking-wider will-change-transform ${fast ? "animate-marquee-fast" : "animate-marquee"}`}>
        <span>{chunk}</span>
        <span aria-hidden>{chunk}</span>
      </div>
    </div>
  );
}
