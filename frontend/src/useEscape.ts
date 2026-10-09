import { useEffect, useRef } from "react";

/**
 * Close a dialog with Esc. Attach the returned ref to the dialog's backdrop: only the top-most open dialog closes, and
 * an Esc that something inside already handled (an inline edit calling preventDefault) is ignored.
 */
export function useEscape<T extends HTMLElement = HTMLDivElement>(onClose: () => void, enabled = true) {
  const ref = useRef<T>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!enabled) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      const backdrops = document.querySelectorAll(".modal-backdrop");
      if (ref.current && backdrops[backdrops.length - 1] !== ref.current) return; // a dialog opened on top of this one
      close.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [enabled]);
  return ref;
}
