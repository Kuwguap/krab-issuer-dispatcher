"use client";

import { useEffect, useState } from "react";

/** True below the given viewport width (default 640px). SSR-safe: starts false. */
export default function useIsMobile(maxWidth = 640) {
  const [mobile, setMobile] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia(`(max-width: ${maxWidth}px)`);
    const update = () => setMobile(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, [maxWidth]);

  return mobile;
}
